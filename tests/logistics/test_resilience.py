"""
What the courier layer does when its own dependencies fail.

These paths are the reason the layer exists — a service that only works when Redis, the
portal and the payload are all healthy has not solved anything — and NFR-3 puts the
breaker and the lock at 100% coverage for exactly that reason.
"""

from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
import respx
from redis.exceptions import ConnectionError as RedisConnectionError

from src.logistics.breaker import BreakerDecision, CircuitBreaker
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderAuthError, ProviderError, ProviderParseError
from src.logistics.keys import token_data_key, token_lock_key
from src.logistics.providers.base import HttpCourierAdapter
from src.logistics.providers.pathao import PathaoAdapter
from src.logistics.schemas import DecryptedCredential, RawResult
from src.logistics.token_manager import PathaoTokenManager, token_manager_for
from tests.logistics.conftest import BEHAVIOR_URL, LOGIN_URL, TENANT, credential_for

FRESH_TOKEN = {"access_token": "fresh-token", "expires_in": 3600}


class BrokenRedis:
    """A Redis that is up enough to be called and down enough to be useless."""

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError

    async def eval(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError

    async def hget(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError


def broken() -> Any:
    return BrokenRedis()


class TestTokenManagerWithoutRedis:
    @respx.mock
    async def test_a_login_still_happens_when_redis_is_unreachable(
        self, credential: DecryptedCredential
    ):
        """Degraded, not broken: no cache, no lock, but the merchant's check still runs."""
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))

        token = await PathaoTokenManager(credential, broken()).get_token()

        assert token == "fresh-token"
        assert login.call_count == 1

    @respx.mock
    async def test_invalidating_a_token_without_redis_does_not_raise(
        self, credential: DecryptedCredential
    ):
        await PathaoTokenManager(credential, broken()).invalidate()

    @respx.mock
    async def test_an_unreadable_cached_token_is_discarded(
        self, credential: DecryptedCredential, redis_client: aioredis.Redis
    ):
        """Corrupt cache entries are dropped and replaced, not parsed hopefully."""
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        await redis_client.set(token_data_key(ProviderEnum.PATHAO, TENANT), "{not json")

        assert await PathaoTokenManager(credential).get_token() == "fresh-token"


class TestTokenManagerEdges:
    @respx.mock
    async def test_a_login_response_with_no_token_is_an_auth_error(
        self, credential: DecryptedCredential
    ):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

        with pytest.raises(ProviderAuthError):
            await PathaoTokenManager(credential).get_token()

    async def test_a_provider_with_no_login_has_no_token_manager(self):
        """Steadfast has no contract yet; asking for its token must fail loudly."""
        with pytest.raises(ProviderAuthError):
            token_manager_for(credential_for(provider=ProviderEnum.STEADFAST))

    @respx.mock
    async def test_a_held_lock_falls_back_to_the_token_we_already_have(
        self,
        credential: DecryptedCredential,
        redis_client: aioredis.Redis,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """FR-4.7 again, from the other side: refreshing is optional, answering is not."""
        from src.config import settings

        monkeypatch.setattr(settings, "TOKEN_LOCK_MAX_POLL_SECONDS", 0.2)
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))

        manager = PathaoTokenManager(credential)
        await manager._write(  # noqa: SLF001 — seeding the cache in the exact state under test
            manager._parse_login_response(  # noqa: SLF001
                {
                    "access_token": "stale-token",
                    "expires_in": settings.TOKEN_SAFETY_MARGIN_SECONDS - 30,
                }
            )
        )
        await redis_client.set(
            token_lock_key(ProviderEnum.PATHAO, TENANT), "another-worker", px=30_000
        )

        assert await manager.get_token() == "stale-token"


class TestRefreshRaces:
    @respx.mock
    async def test_a_token_written_while_we_waited_for_the_lock_is_used_as_is(
        self, credential: DecryptedCredential
    ):
        """
        The double-check inside the lock, exercised.

        Between reading the cache and winning the lock, another worker can finish its
        own login. Without the re-read this manager would log in again for nothing.
        """
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        manager = PathaoTokenManager(credential)
        written_by_someone_else = manager._parse_login_response(  # noqa: SLF001
            {"access_token": "their-token", "expires_in": 3600}
        )
        reads = {"count": 0}

        async def read_racing_with_another_worker():
            reads["count"] += 1
            return None if reads["count"] == 1 else written_by_someone_else

        manager._read = read_racing_with_another_worker  # type: ignore[method-assign]  # noqa: SLF001

        assert await manager.get_token() == "their-token"
        assert login.call_count == 0

    @respx.mock
    async def test_a_waiter_that_gives_up_falls_back_to_the_token_it_started_with(
        self,
        credential: DecryptedCredential,
        redis_client: aioredis.Redis,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """
        Redis going flaky mid-refresh must not cost the merchant their check.

        The lock is held elsewhere and the cache stops answering, so the waiter times
        out with nothing — and still has the grace-period token it walked in with.
        """
        from src.config import settings

        monkeypatch.setattr(settings, "TOKEN_LOCK_MAX_POLL_SECONDS", 0.2)
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        manager = PathaoTokenManager(credential)
        in_grace = manager._parse_login_response(  # noqa: SLF001
            {"access_token": "stale-token", "expires_in": settings.TOKEN_SAFETY_MARGIN_SECONDS - 30}
        )
        reads = {"count": 0}

        async def read_then_stop_answering():
            reads["count"] += 1
            return in_grace if reads["count"] == 1 else None

        manager._read = read_then_stop_answering  # type: ignore[method-assign]  # noqa: SLF001
        await redis_client.set(
            token_lock_key(ProviderEnum.PATHAO, TENANT), "another-worker", px=30_000
        )

        assert await manager.get_token() == "stale-token"


class TestLoginFailures:
    @respx.mock
    async def test_a_rejected_password_is_an_auth_error(self, credential: DecryptedCredential):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(401, json={"message": "no"}))

        with pytest.raises(ProviderAuthError):
            await PathaoTokenManager(credential).get_token()

    @respx.mock
    async def test_a_portal_outage_is_not_reported_as_a_bad_password(
        self, credential: DecryptedCredential
    ):
        """The distinction upstream loses: a 500 says nothing about the credential."""
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(500))

        with pytest.raises(ProviderError) as caught:
            await PathaoTokenManager(credential).get_token()
        assert not isinstance(caught.value, ProviderAuthError)

    @respx.mock
    async def test_a_portal_that_cannot_be_reached_at_all_is_a_provider_error(
        self, credential: DecryptedCredential
    ):
        respx.post(LOGIN_URL).mock(side_effect=httpx.ConnectError("dns"))

        with pytest.raises(ProviderError) as caught:
            await PathaoTokenManager(credential).get_token()
        assert not isinstance(caught.value, ProviderAuthError)


class TestBreakerWithoutRedis:
    async def test_an_unreadable_breaker_lets_the_call_through(self):
        """A breaker that cannot be read must not become an outage of its own."""
        breaker = CircuitBreaker(ProviderEnum.PATHAO, TENANT, broken())

        assert await breaker.allow() is BreakerDecision.CLOSED

    async def test_recording_outcomes_without_redis_does_not_raise(self):
        breaker = CircuitBreaker(ProviderEnum.PATHAO, TENANT, broken())

        await breaker.record_failure()
        await breaker.record_success()

        assert await breaker.state() == "closed"

    async def test_a_fresh_breaker_reports_closed(self):
        assert await CircuitBreaker(ProviderEnum.PATHAO, TENANT).state() == "closed"


class TestAdapterEdges:
    @respx.mock
    async def test_a_non_json_body_is_a_parse_error(self, credential: DecryptedCredential):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        respx.post(BEHAVIOR_URL).mock(return_value=httpx.Response(200, text="<html>maintenance"))

        with pytest.raises(ProviderParseError):
            await PathaoAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_a_json_array_body_is_a_parse_error(self, credential: DecryptedCredential):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=FRESH_TOKEN))
        respx.post(BEHAVIOR_URL).mock(return_value=httpx.Response(200, json=[]))

        with pytest.raises(ProviderParseError):
            await PathaoAdapter().fetch("01712345678", credential)

    async def test_the_base_adapter_refuses_to_pretend_it_can_fetch(
        self, credential: DecryptedCredential
    ):
        """A subclass that forgets ``_call`` fails immediately, not with an empty result."""
        adapter = HttpCourierAdapter()
        adapter.name = ProviderEnum.PATHAO

        with pytest.raises(NotImplementedError):
            await adapter.fetch("01712345678", credential)
        with pytest.raises(NotImplementedError):
            adapter.normalize(RawResult(provider=ProviderEnum.PATHAO, payload={}, latency_ms=0))
