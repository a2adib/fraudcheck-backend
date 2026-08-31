"""FR-3 — the adapter layer: protocol conformance, normalisation, timeouts."""

import asyncio
import inspect
import time

import httpx
import pytest
import respx

from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderParseError,
    ProviderTimeout,
)
from src.logistics.providers.base import CourierAdapter
from src.logistics.providers.mock import MockAdapter
from src.logistics.providers.pathao import PathaoAdapter
from src.logistics.providers.registry import get_adapters, register, unregister
from src.logistics.schemas import DecryptedCredential, DeliveryStats, RawResult
from tests.logistics.conftest import BEHAVIOR_URL, LOGIN_URL

TOKEN_RESPONSE = {"access_token": "portal-token", "expires_in": 3600, "token_type": "Bearer"}


def pathao_payload(total: int = 10, delivered: int = 7, rating: str | None = "Good") -> dict:
    return {
        "data": {
            "customer": {"total_delivery": total, "successful_delivery": delivered},
            "customer_rating": rating,
        }
    }


def raw(payload: dict) -> RawResult:
    return RawResult(provider=ProviderEnum.PATHAO, payload=payload, latency_ms=12)


class TestRegistry:
    def test_ac_3_1_every_registered_adapter_satisfies_the_protocol(self):
        """AC-3.1 — runtime_checkable, so the registry cannot hold something unusable."""
        for adapter in get_adapters().values():
            assert isinstance(adapter, CourierAdapter)

    def test_ac_3_7_every_adapter_fetch_is_a_coroutine_function(self):
        """AC-3.7 — FR-3.6 forbids the synchronous upstream style; assert it over the registry."""
        for adapter in get_adapters().values():
            assert inspect.iscoroutinefunction(adapter.fetch)

    def test_mock_mode_serves_every_provider(self, monkeypatch: pytest.MonkeyPatch):
        from src.config import settings

        monkeypatch.setattr(settings, "MOCK_MODE", True)

        assert set(get_adapters()) == set(ProviderEnum)

    def test_registering_a_provider_makes_it_visible_without_touching_the_orchestrator(self):
        """The registry is the only place a new courier has to be mentioned (FR-3.3)."""

        class DummyAdapter(MockAdapter):
            pass

        try:
            register(DummyAdapter(ProviderEnum.STEADFAST))
            assert ProviderEnum.STEADFAST in get_adapters()
        finally:
            unregister(ProviderEnum.STEADFAST)


class TestPathaoNormalisation:
    def test_ac_3_2_a_real_shaped_payload_normalises_exactly(self):
        """AC-3.2 — the recorded upstream shape, mapped field for field."""
        stats = PathaoAdapter().normalize(raw(pathao_payload(total=10, delivered=7)))

        assert stats == DeliveryStats(
            total_orders=10, delivered=7, returned=3, cancelled=0, customer_rating="Good"
        )

    def test_ac_3_6_returns_are_derived_and_cancellations_default_to_zero(self):
        """AC-3.6 — neither known provider reports returns; the remainder is the assumption."""
        stats = PathaoAdapter().normalize(raw(pathao_payload(total=10, delivered=7, rating=None)))

        assert (stats.returned, stats.cancelled) == (3, 0)
        assert stats.customer_rating is None

    def test_more_deliveries_than_orders_cannot_produce_negative_returns(self):
        stats = PathaoAdapter().normalize(raw(pathao_payload(total=3, delivered=5)))

        assert stats.returned == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"data": None},
            {"data": {}},
            {"data": {"customer": None}},
            {"data": {"customer": {}}},
            {"data": {"customer": {"total_delivery": "many", "successful_delivery": 1}}},
            {"data": {"customer": {"total_delivery": True, "successful_delivery": 1}}},
        ],
    )
    def test_ac_3_3_a_malformed_payload_raises_provider_parse_error(self, payload: dict):
        """AC-3.3 — a shape change must not normalise to zeros, which read as 'clean customer'."""
        with pytest.raises(ProviderParseError):
            PathaoAdapter().normalize(raw(payload))


class TestPathaoFetch:
    @respx.mock
    async def test_it_logs_in_then_asks_for_the_customer(self, credential: DecryptedCredential):
        login = respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        lookup = respx.post(BEHAVIOR_URL).mock(
            return_value=httpx.Response(200, json=pathao_payload())
        )

        result = await PathaoAdapter().fetch("01712345678", credential)

        assert login.called
        assert lookup.called
        assert lookup.calls.last.request.headers["Authorization"] == "Bearer portal-token"
        assert result.payload == pathao_payload()

    @respx.mock
    async def test_a_revoked_token_is_dropped_and_the_call_retried_once(
        self, credential: DecryptedCredential
    ):
        """A cached token can be revoked at the portal; that is not a bad credential."""
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        lookup = respx.post(BEHAVIOR_URL).mock(
            side_effect=[
                httpx.Response(401, json={"message": "expired"}),
                httpx.Response(200, json=pathao_payload()),
            ]
        )

        result = await PathaoAdapter().fetch("01712345678", credential)

        assert lookup.call_count == 2
        assert result.payload == pathao_payload()

    @respx.mock
    async def test_a_credential_the_portal_keeps_rejecting_raises_auth_error(
        self, credential: DecryptedCredential
    ):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        respx.post(BEHAVIOR_URL).mock(return_value=httpx.Response(403, json={"message": "no"}))

        with pytest.raises(ProviderAuthError):
            await PathaoAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_a_server_error_is_a_provider_error(self, credential: DecryptedCredential):
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
        respx.post(BEHAVIOR_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(ProviderError):
            await PathaoAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_ac_3_5_a_slow_provider_times_out_within_its_budget(
        self, credential: DecryptedCredential
    ):
        """AC-3.5 — ProviderTimeout, raised within the declared timeout + 500ms."""
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))

        async def never_answers(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json=pathao_payload())

        respx.post(BEHAVIOR_URL).mock(side_effect=never_answers)

        started = time.monotonic()
        with pytest.raises(ProviderTimeout):
            await PathaoAdapter(timeout_seconds=0.2).fetch("01712345678", credential)
        elapsed = time.monotonic() - started

        assert elapsed < 0.7
