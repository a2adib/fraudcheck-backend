"""FR-3 — the adapter layer: protocol conformance, normalisation, timeouts."""

import asyncio
import inspect
import logging
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
from src.logistics.providers.redx import RedxAdapter
from src.logistics.providers.registry import get_adapters, register, unregister
from src.logistics.schemas import DecryptedCredential, DeliveryStats, RawResult
from tests.logistics.conftest import (
    BEHAVIOR_URL,
    LOGIN_URL,
    REDX_BEHAVIOR_URL,
    REDX_LOGIN_URL,
    credential_for,
)

TOKEN_RESPONSE = {"access_token": "portal-token", "expires_in": 3600, "token_type": "Bearer"}


def pathao_payload(total: int = 10, delivered: int = 7, rating: str | None = "Good") -> dict:
    return {
        "data": {
            "customer": {"total_delivery": total, "successful_delivery": delivered},
            "customer_rating": rating,
        }
    }


REDX_TOKEN_RESPONSE = {"isError": False, "data": {"accessToken": "redx-token"}}


def redx_payload(total: int = 10, delivered: int = 7, segment: str | None = "Good") -> dict:
    return {
        "data": {
            "totalParcels": total,
            "deliveredParcels": delivered,
            "customerSegment": segment,
        }
    }


def raw(payload: dict) -> RawResult:
    return RawResult(provider=ProviderEnum.PATHAO, payload=payload, latency_ms=12)


def redx_raw(payload: dict) -> RawResult:
    return RawResult(provider=ProviderEnum.REDX, payload=payload, latency_ms=12)


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


class TestRedxNormalisation:
    def test_ac_3_2_a_real_shaped_payload_normalises_exactly(self):
        """AC-3.2 — the shape recorded in the spec's provider table, mapped field for field."""
        stats = RedxAdapter().normalize(redx_raw(redx_payload(total=10, delivered=7)))

        assert stats == DeliveryStats(
            total_orders=10, delivered=7, returned=3, cancelled=0, customer_rating="Good"
        )

    def test_ac_3_6_returns_are_derived_and_cancellations_default_to_zero(self):
        """AC-3.6 — RedX reports totals and deliveries only; the rest is the assumption."""
        stats = RedxAdapter().normalize(redx_raw(redx_payload(total=10, delivered=7, segment=None)))

        assert (stats.returned, stats.cancelled) == (3, 0)
        assert stats.customer_rating is None

    def test_more_deliveries_than_orders_cannot_produce_negative_returns(self):
        stats = RedxAdapter().normalize(redx_raw(redx_payload(total=3, delivered=5)))

        assert stats.returned == 0

    def test_an_empty_segment_is_no_rating_rather_than_an_empty_one(self):
        """Upstream defaults ``customerSegment`` to ``""``; an empty badge is not a rating."""
        stats = RedxAdapter().normalize(redx_raw(redx_payload(segment="")))

        assert stats.customer_rating is None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"data": None},
            {"data": []},
            {"data": {"deliveredParcels": 1}},
            {"data": {"totalParcels": 10}},
            {"data": {"totalParcels": "many", "deliveredParcels": 1}},
            {"data": {"totalParcels": True, "deliveredParcels": 1}},
        ],
    )
    def test_ac_3_3_a_malformed_payload_raises_provider_parse_error(self, payload: dict):
        """AC-3.3 — a shape change must not normalise to zeros, which read as 'clean customer'."""
        with pytest.raises(ProviderParseError):
            RedxAdapter().normalize(redx_raw(payload))


class TestRedxFetch:
    @respx.mock
    async def test_it_logs_in_on_one_host_then_queries_the_other(self):
        """The token is minted at the API host and spent at the panel host."""
        credential = credential_for(provider=ProviderEnum.REDX)
        login = respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE)
        )
        lookup = respx.get(REDX_BEHAVIOR_URL).mock(
            return_value=httpx.Response(200, json=redx_payload())
        )

        result = await RedxAdapter().fetch("01712345678", credential)

        assert login.called
        assert lookup.called
        assert lookup.calls.last.request.headers["Authorization"] == "Bearer redx-token"
        assert result.payload == redx_payload()

    @respx.mock
    async def test_the_phone_travels_as_the_phone_number_query_parameter(self):
        """RedX takes a GET with a query string, not Pathao's JSON body."""
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        lookup = respx.get(REDX_BEHAVIOR_URL).mock(
            return_value=httpx.Response(200, json=redx_payload())
        )

        await RedxAdapter().fetch("01712345678", credential)

        assert lookup.calls.last.request.url.params["phoneNumber"] == "01712345678"

    @respx.mock
    async def test_a_revoked_token_is_dropped_and_the_call_retried_once(self):
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        lookup = respx.get(REDX_BEHAVIOR_URL).mock(
            side_effect=[
                httpx.Response(401, json={"message": "expired"}),
                httpx.Response(200, json=redx_payload()),
            ]
        )

        result = await RedxAdapter().fetch("01712345678", credential)

        assert lookup.call_count == 2
        assert result.payload == redx_payload()

    @respx.mock
    async def test_a_credential_the_portal_keeps_rejecting_raises_auth_error(self):
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        respx.get(REDX_BEHAVIOR_URL).mock(return_value=httpx.Response(403, json={"message": "no"}))

        with pytest.raises(ProviderAuthError):
            await RedxAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_a_phone_redx_has_never_carried_is_an_empty_history_not_an_error(self):
        """
        A 404 means "no parcels for this number", which is a fact, not a failure.

        Zeroing is safe here precisely because ``score_from`` flags a zero total as
        ``insufficient_data`` at the neutral score — an unknown customer never reads as
        a clean one.
        """
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        respx.get(REDX_BEHAVIOR_URL).mock(return_value=httpx.Response(404, json={"data": None}))

        result = await RedxAdapter().fetch("01712345678", credential)
        stats = RedxAdapter().normalize(result)

        assert stats == DeliveryStats(
            total_orders=0, delivered=0, returned=0, cancelled=0, customer_rating=None
        )

    @respx.mock
    async def test_a_server_error_is_a_provider_error(self):
        """Upstream swallows this into ``None``, which reads as 'no history'. It is not."""
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        respx.get(REDX_BEHAVIOR_URL).mock(return_value=httpx.Response(503))

        with pytest.raises(ProviderError):
            await RedxAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_a_non_json_body_is_a_parse_error(self):
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))
        respx.get(REDX_BEHAVIOR_URL).mock(
            return_value=httpx.Response(200, text="<html>maintenance")
        )

        with pytest.raises(ProviderParseError):
            await RedxAdapter().fetch("01712345678", credential)

    @respx.mock
    async def test_the_phone_never_reaches_the_logs(self, caplog: pytest.LogCaptureFixture):
        """
        RedX carries the phone in the query string, so a logged URL is a logged phone.

        Pathao puts it in a body, which makes this adapter the one that can leak by
        accident. Asserted by grepping the captured output rather than by inspection.
        """
        phone = "01712345678"
        credential = credential_for(provider=ProviderEnum.REDX)
        # A rejected login is the interesting case: RedX's error body echoes the phone.
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(
                200, json={"isError": True, "message": f"no merchant for {phone}"}
            )
        )

        with caplog.at_level(logging.DEBUG), pytest.raises(ProviderAuthError):
            await RedxAdapter().fetch(phone, credential)

        assert phone not in caplog.text

    @respx.mock
    async def test_ac_3_5_a_slow_provider_times_out_within_its_budget(self):
        """AC-3.5 — ProviderTimeout, raised within the declared timeout + 500ms."""
        credential = credential_for(provider=ProviderEnum.REDX)
        respx.post(REDX_LOGIN_URL).mock(return_value=httpx.Response(200, json=REDX_TOKEN_RESPONSE))

        async def never_answers(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json=redx_payload())

        respx.get(REDX_BEHAVIOR_URL).mock(side_effect=never_answers)

        started = time.monotonic()
        with pytest.raises(ProviderTimeout):
            await RedxAdapter(timeout_seconds=0.2).fetch("01712345678", credential)
        elapsed = time.monotonic() - started

        assert elapsed < 0.7
