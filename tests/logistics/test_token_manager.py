"""FR-4 — one login per stampede, per tenant, with a grace period."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
import redis.asyncio as aioredis
import respx

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import TokenWaitTimeout
from src.logistics.keys import token_data_key, token_lock_key
from src.logistics.token_manager import PathaoTokenManager, TokenRecord
from tests.logistics.conftest import LOGIN_URL, TENANT, credential_for

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
