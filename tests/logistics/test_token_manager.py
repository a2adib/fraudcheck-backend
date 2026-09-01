"""FR-4 — one login per stampede, per tenant, with a grace period."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import jwt
import pytest
import redis.asyncio as aioredis
import respx

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderAuthError, TokenWaitTimeout
from src.logistics.keys import token_data_key, token_lock_key
from src.logistics.token_manager import (
    REDX_DEFAULT_TOKEN_TTL_SECONDS,
    PathaoTokenManager,
    RedxTokenManager,
    TokenRecord,
)
from tests.logistics.conftest import LOGIN_URL, REDX_LOGIN_URL, TENANT, credential_for

FRESH_TOKEN = {"access_token": "fresh-token", "expires_in": 3600, "token_type": "Bearer"}
CACHED_TOKEN = "cached-token"


def manager(tenant: str = TENANT) -> PathaoTokenManager:
    return PathaoTokenManager(credential_for(tenant))


async def seed_token(
    redis_client: aioredis.Redis, tenant: str, *, expires_in: float, token: str = CACHED_TOKEN
) -> None:
    record = TokenRecord(
        access_token=token,
        expires_at=datetime.now(UTC).timestamp() + expires_in,
        token_type="Bearer",
    )
    await redis_client.set(
        token_data_key(ProviderEnum.PATHAO, tenant), record.model_dump_json(), ex=3600
    )


class TestStampede:
    @respx.mock
    async def test_ac_4_1_fifty_concurrent_checks_trigger_exactly_one_login(self):
        """AC-4.1 — the whole point of the lock: 50 callers, one login, 50 successes."""
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))

        tokens = await asyncio.gather(*(manager().get_token() for _ in range(50)))

        assert login.call_count == 1
        assert tokens == ["fresh-token"] * 50

    @respx.mock
    async def test_ac_4_2_a_cached_token_makes_no_login_call(self, redis_client: aioredis.Redis):
        """AC-4.2 — a valid cached token is used as is."""
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await seed_token(redis_client, TENANT, expires_in=3600)

        assert await manager().get_token() == "cached-token"
        assert login.call_count == 0

    @respx.mock
    async def test_ac_4_3_an_expired_token_is_replaced(self, redis_client: aioredis.Redis):
        """AC-4.3 — past the grace window the cached token is worthless."""
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await seed_token(redis_client, TENANT, expires_in=-settings.TOKEN_GRACE_PERIOD_SECONDS - 60)

        assert await manager().get_token() == "fresh-token"
        assert login.call_count == 1

        cached = json.loads(await redis_client.get(token_data_key(ProviderEnum.PATHAO, TENANT)))
        assert cached["access_token"] == "fresh-token"


class TestLock:
    @respx.mock
    async def test_ac_4_4_an_abandoned_lock_expires_and_the_next_request_proceeds(
        self, redis_client: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
    ):
        """AC-4.4 — a worker that dies holding the lock cannot deadlock the system."""
        monkeypatch.setattr(settings, "TOKEN_LOCK_TIMEOUT_MS", 300)
        monkeypatch.setattr(settings, "TOKEN_LOCK_MAX_POLL_SECONDS", 3.0)
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))

        # A holder that never releases and never writes a token — i.e. it crashed.
        await redis_client.set(token_lock_key(ProviderEnum.PATHAO, TENANT), "dead-worker", px=300)

        assert await manager().get_token() == "fresh-token"
        assert login.call_count == 1

    @respx.mock
    async def test_ac_4_6_waiting_out_the_lock_with_no_token_is_a_token_wait_timeout(
        self, redis_client: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
    ):
        """AC-4.6 — the caller gets a named failure, and the check above it still completes."""
        monkeypatch.setattr(settings, "TOKEN_LOCK_MAX_POLL_SECONDS", 0.3)
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await redis_client.set(
            token_lock_key(ProviderEnum.PATHAO, TENANT), "another-worker", px=30_000
        )

        with pytest.raises(TokenWaitTimeout):
            await manager().get_token()

    @respx.mock
    async def test_the_lock_is_released_after_a_refresh(self, redis_client: aioredis.Redis):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))

        await manager().get_token()

        assert await redis_client.get(token_lock_key(ProviderEnum.PATHAO, TENANT)) is None


class TestTenantIsolation:
    @respx.mock
    async def test_ac_4_5_two_merchants_get_two_cache_keys(self, redis_client: aioredis.Redis):
        """AC-4.5 — one merchant's session token is never used for another's request."""
        respx.post(LOGIN_URL).mock(
            side_effect=[
                httpx.Response(200, json={**FRESH_TOKEN, "access_token": "token-a"}),
                httpx.Response(200, json={**FRESH_TOKEN, "access_token": "token-b"}),
            ]
        )

        first = await manager("tenant-a").get_token()
        second = await manager("tenant-b").get_token()

        assert (first, second) == ("token-a", "token-b")
        assert await redis_client.exists(token_data_key(ProviderEnum.PATHAO, "tenant-a"))
        assert await redis_client.exists(token_data_key(ProviderEnum.PATHAO, "tenant-b"))

    @respx.mock
    async def test_a_cached_token_is_not_visible_to_another_tenant(
        self, redis_client: aioredis.Redis
    ):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await seed_token(redis_client, "tenant-a", expires_in=3600, token="tenant-a-token")

        assert await manager("tenant-b").get_token() == "fresh-token"


class TestGracePeriod:
    @respx.mock
    async def test_ac_4_7_a_stale_token_is_served_when_the_refresh_fails(
        self, redis_client: aioredis.Redis
    ):
        """AC-4.7 — 30s past the safety margin, with the portal down, is not a failed check."""
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(500))
        await seed_token(redis_client, TENANT, expires_in=settings.TOKEN_SAFETY_MARGIN_SECONDS - 30)

        assert await manager().get_token() == "cached-token"
        assert login.called

    @respx.mock
    async def test_a_successful_refresh_inside_the_grace_window_replaces_the_token(
        self, redis_client: aioredis.Redis
    ):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await seed_token(redis_client, TENANT, expires_in=settings.TOKEN_SAFETY_MARGIN_SECONDS - 30)

        assert await manager().get_token() == "fresh-token"


class TestInvalidate:
    @respx.mock
    async def test_invalidating_forces_the_next_call_to_log_in(self, redis_client: aioredis.Redis):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await seed_token(redis_client, TENANT, expires_in=3600)

        await manager().invalidate()

        assert await manager().get_token() == "fresh-token"


def redx_manager(tenant: str = TENANT) -> RedxTokenManager:
    return RedxTokenManager(credential_for(tenant, ProviderEnum.REDX))


def redx_login_body(token: str) -> dict:
    return {"isError": False, "data": {"accessToken": token}}


class TestRedxLogin:
    """RedX's login differs from Pathao's in every part except the lock around it."""

    @respx.mock
    async def test_the_login_body_carries_a_phone_not_a_username(self):
        """
        FR-2 stores one ``username`` column; RedX spends it as ``phone``.

        Sending ``username`` would be accepted by nothing and rejected as a bad
        credential, which is the most misleading failure available.
        """
        login = respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json=redx_login_body("redx-token"))
        )

        await redx_manager().get_token()

        assert json.loads(login.calls.last.request.content) == {
            "phone": "portal-user",
            "password": "portal-password",
        }

    @respx.mock
    async def test_a_rejected_credential_arrives_as_is_error_at_http_200(self):
        """
        RedX answers a bad password with 200 and ``isError: true``.

        ``raise_for_status()`` never fires, so the auth error has to come out of
        ``_parse_login_response`` — and reach the caller un-rewrapped as an outage.
        """
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json={"isError": True, "message": "invalid"})
        )

        with pytest.raises(ProviderAuthError):
            await redx_manager().get_token()

    @respx.mock
    async def test_the_rejection_reason_is_not_carried_into_the_error(self):
        """FR-2.5 / AC-2.6 — RedX echoes the submitted phone in ``message``; it stays there."""
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(
                200, json={"isError": True, "message": "no merchant for 01712345678"}
            )
        )

        with pytest.raises(ProviderAuthError) as exc_info:
            await redx_manager().get_token()

        assert "01712345678" not in str(exc_info.value)

    @respx.mock
    async def test_a_login_response_with_no_access_token_is_an_auth_error(self):
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json={"isError": False, "data": {}})
        )

        with pytest.raises(ProviderAuthError):
            await redx_manager().get_token()

    @respx.mock
    async def test_the_jwt_expiry_is_honoured(self, redis_client: aioredis.Redis):
        """RedX states the expiry only inside the token, so it has to be read out."""
        expires_at = datetime.now(UTC).timestamp() + 900
        token = jwt.encode({"exp": int(expires_at)}, "x" * 32, algorithm="HS256")
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json=redx_login_body(token))
        )

        await redx_manager().get_token()

        cached = TokenRecord.model_validate_json(
            await redis_client.get(token_data_key(ProviderEnum.REDX, TENANT))
        )
        assert cached.expires_at == pytest.approx(expires_at, abs=1)

    @respx.mock
    async def test_a_token_that_is_not_a_jwt_falls_back_to_the_default_ttl(
        self, redis_client: aioredis.Redis
    ):
        """An opaque token is not an error — it just has no expiry to read."""
        before = datetime.now(UTC).timestamp()
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json=redx_login_body("an-opaque-token"))
        )

        await redx_manager().get_token()

        cached = TokenRecord.model_validate_json(
            await redis_client.get(token_data_key(ProviderEnum.REDX, TENANT))
        )
        assert cached.expires_at == pytest.approx(before + REDX_DEFAULT_TOKEN_TTL_SECONDS, abs=5)

    @respx.mock
    async def test_ac_4_5_two_merchants_get_two_redx_cache_keys(self, redis_client: aioredis.Redis):
        """AC-4.5 — the per-tenant namespacing is the base class's, but assert it holds here."""
        respx.post(REDX_LOGIN_URL).mock(
            side_effect=[
                httpx.Response(200, json=redx_login_body("token-a")),
                httpx.Response(200, json=redx_login_body("token-b")),
            ]
        )

        first = await redx_manager("tenant-a").get_token()
        second = await redx_manager("tenant-b").get_token()

        assert (first, second) == ("token-a", "token-b")
        assert await redis_client.exists(token_data_key(ProviderEnum.REDX, "tenant-a"))
        assert await redis_client.exists(token_data_key(ProviderEnum.REDX, "tenant-b"))

    @respx.mock
    async def test_a_pathao_token_is_never_served_to_a_redx_lookup(
        self, redis_client: aioredis.Redis
    ):
        """The provider is part of the key, so the two never collide."""
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json=redx_login_body("redx-token"))
        )
        await seed_token(redis_client, TENANT, expires_in=3600, token="pathao-token")

        assert await redx_manager().get_token() == "redx-token"
