"""FR-6 — check creation, concurrent fan-out, SSE streaming, caching, persistence."""

import asyncio
import json
import time

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.checks.enums import RiskBand
from src.checks.keys import check_cache_key
from src.checks.models import CheckRequest, ProviderResult
from src.checks.schemas import CheckCreate
from src.checks.services import CheckService, wait_for_running_checks
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderError
from src.logistics.schemas import DeliveryStats, RawResult
from src.users.models import User
from tests.checks.conftest import consume_stream, create_check, events_named

PHONE = "01712345678"


class StubAdapter:
    """A courier with a stopwatch: exactly the latency and outcome the test asked for."""

    def __init__(
        self,
        provider: ProviderEnum,
        *,
        delay: float = 0.0,
        delivered: int = 9,
        total: int = 10,
        error: Exception | None = None,
    ) -> None:
        """Build a stub courier with a fixed latency and outcome."""
        self.name = provider
        self.timeout_seconds = 5.0
        self.delay = delay
        self.delivered = delivered
        self.total = total
        self.error = error
        self.calls = 0

    async def fetch(self, phone: str, credential: object) -> RawResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return RawResult(provider=self.name, payload={"total": self.total}, latency_ms=1)

    def normalize(self, raw: RawResult) -> DeliveryStats:
        return DeliveryStats(
            total_orders=self.total,
            delivered=self.delivered,
            returned=self.total - self.delivered,
            cancelled=0,
        )


class TestCreation:
    async def test_ac_6_1_a_check_is_accepted_with_an_id_and_a_stream_url(
        self, auth_client: AsyncClient
    ):
        """AC-6.1 — 202, because nothing has been checked yet."""
        response = await create_check(auth_client)

        assert response.status_code == 202
        data = response.json()["data"]
        assert data["check_id"]
        assert data["stream_url"] == f"/checks/{data['check_id']}/stream"
        assert data["phone_masked"] == "017*****678"
        assert PHONE not in response.text

    @pytest.mark.parametrize(
        "raw_phone", ["+8801712345678", "8801712345678", "01712345678", "017-1234-5678"]
    )
    async def test_ac_6_6_every_accepted_format_normalises_to_one_number(
        self, auth_client: AsyncClient, async_session: AsyncSession, raw_phone: str
    ):
        """AC-6.6 — four spellings, one customer, one cache key."""
        response = await create_check(auth_client, phone=raw_phone)

        check = (
            await async_session.exec(
                select(CheckRequest).where(
                    CheckRequest.public_id == response.json()["data"]["check_id"]
                )
            )
        ).one()
        assert check.phone_normalized == PHONE

    async def test_ac_6_7_an_invalid_operator_prefix_is_a_422(self, auth_client: AsyncClient):
        """AC-6.7 — 012 is not a Bangladesh mobile prefix."""
        assert (await create_check(auth_client, phone="01212345678")).status_code == 422

    async def test_an_unauthenticated_check_is_rejected(self, client: AsyncClient):
        assert (await create_check(client)).status_code == 401


class TestStreaming:
    async def test_ac_6_2_the_event_sequence_is_started_results_score_done(
        self, auth_client: AsyncClient
    ):
        """AC-6.2 — three healthy providers produce exactly six events, in order."""
        created = (await create_check(auth_client)).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        assert [name for name, _ in events] == [
            "started",
            "provider_result",
            "provider_result",
            "provider_result",
            "score",
            "done",
        ]

    async def test_ac_6_3_results_are_not_batched(
        self,
        async_session: AsyncSession,
        merchant: User,
        stub_registry,
    ):
        """
        AC-6.3 — with providers at 200ms, 1s and 3s, the first result must not wait.

        Driven through the service rather than the HTTP client on purpose: httpx's
        ASGI transport collects the whole response body before handing it back, so a
        client-side stopwatch here would measure the transport, not the fan-out.
        """
        stub_registry(
            {
                ProviderEnum.PATHAO: StubAdapter(ProviderEnum.PATHAO, delay=0.2),
                ProviderEnum.REDX: StubAdapter(ProviderEnum.REDX, delay=1.0),
                ProviderEnum.STEADFAST: StubAdapter(ProviderEnum.STEADFAST, delay=3.0),
            }
        )
        service = CheckService(async_session)
        check = await service.create(merchant, CheckCreate(phone=PHONE))

        started = time.monotonic()
        first_result_at = None
        first_provider = None
        async for event in service.event_stream(merchant, check):
            if event.event == "provider_result":
                first_result_at = time.monotonic() - started
                first_provider = json.loads(str(event.data))["provider"]
                break

        assert first_provider == "pathao"
        assert first_result_at is not None
        assert first_result_at < 0.4
        await wait_for_running_checks()

    async def test_ac_6_4_one_failing_provider_does_not_fail_the_check(
        self, auth_client: AsyncClient, stub_registry
    ):
        """AC-6.4 — the other two answer, and ``done`` is still emitted."""
        stub_registry(
            {
                ProviderEnum.PATHAO: StubAdapter(
                    ProviderEnum.PATHAO, error=ProviderError("portal on fire")
                ),
                ProviderEnum.REDX: StubAdapter(ProviderEnum.REDX),
                ProviderEnum.STEADFAST: StubAdapter(ProviderEnum.STEADFAST),
            }
        )
        created = (await create_check(auth_client)).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        results = {
            item["provider"]: item["status"] for item in events_named(events, "provider_result")
        }
        assert results == {"pathao": "unavailable", "redx": "ok", "steadfast": "ok"}
        assert events_named(events, "done")

    async def test_ac_6_5_every_provider_failing_still_produces_a_score(
        self, auth_client: AsyncClient
    ):
        """AC-6.5 — three ``unavailable`` legs, ``insufficient_data``, and an HTTP 200."""
        created = (await create_check(auth_client, phone="01700000005")).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        assert {item["status"] for item in events_named(events, "provider_result")} == {
            "unavailable"
        }
        score = events_named(events, "score")[0]
        assert score["insufficient_data"] is True
        assert score["band"] == RiskBand.MEDIUM.value

    async def test_ac_6_7_everything_is_persisted_before_done(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        """AC-6.7 / FR-6.7 — the stored record is complete by the time the stream closes."""
        created = (await create_check(auth_client)).json()["data"]

        await consume_stream(auth_client, created["stream_url"])

        detail = (await auth_client.get(f"/checks/{created['check_id']}")).json()["data"]
        assert len(detail["providers"]) == 3
        assert detail["risk_score"] is not None
        assert detail["phone_masked"] == "017*****678"

    async def test_ac_6_10_a_client_hanging_up_does_not_lose_the_result(
        self, auth_client: AsyncClient, async_session: AsyncSession, stub_registry
    ):
        """AC-6.10 — the fan-out outlives the response; the merchant's history is intact."""
        stub_registry(
            {
                ProviderEnum.PATHAO: StubAdapter(ProviderEnum.PATHAO),
                ProviderEnum.REDX: StubAdapter(ProviderEnum.REDX, delay=0.3),
                ProviderEnum.STEADFAST: StubAdapter(ProviderEnum.STEADFAST, delay=0.5),
            }
        )
        created = (await create_check(auth_client)).json()["data"]

        await consume_stream(auth_client, created["stream_url"], stop_after="provider_result")
        await wait_for_running_checks()

        check = (
            await async_session.exec(
                select(CheckRequest).where(CheckRequest.public_id == created["check_id"])
            )
        ).one()
        await async_session.refresh(check)
        results = (
            await async_session.exec(
                select(ProviderResult).where(ProviderResult.check_request_id == check.id)
            )
        ).all()
        assert len(results) == 3
        assert check.risk_score is not None


class TestDashboardLookup:
    """``POST /checks/lookup`` — the whole result in one response."""

    async def test_a_lookup_returns_the_score_and_every_provider(self, auth_client: AsyncClient):
        response = await auth_client.post("/checks/lookup", json={"phone": PHONE})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["risk_band"] in {"low", "medium", "high"}
        assert len(data["providers"]) == 3
        assert data["score"]["breakdown"]
        assert data["cached"] is False
        assert data["phone_masked"] == "017*****678"
        assert PHONE not in response.text

    async def test_a_lookup_is_stored_like_any_other_check(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        """One history, however the check was started."""
        data = (await auth_client.post("/checks/lookup", json={"phone": PHONE})).json()["data"]

        detail = (await auth_client.get(f"/checks/{data['check_id']}")).json()["data"]
        assert detail["risk_score"] == data["risk_score"]
        assert len(detail["providers"]) == 3

    async def test_a_second_lookup_is_served_from_cache(
        self, auth_client: AsyncClient, stub_registry
    ):
        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})

        first = (await auth_client.post("/checks/lookup", json={"phone": PHONE})).json()["data"]
        second = (await auth_client.post("/checks/lookup", json={"phone": PHONE})).json()["data"]

        assert adapter.calls == 1
        assert (first["cached"], second["cached"]) == (False, True)
        assert first["risk_score"] == second["risk_score"]

    async def test_a_lookup_and_a_streamed_check_agree(self, auth_client: AsyncClient):
        """The two entry points share one code path; this is the assertion that says so."""
        streamed = (await create_check(auth_client, phone="01700000002")).json()["data"]
        events = await consume_stream(auth_client, streamed["stream_url"])

        looked_up = (
            await auth_client.post("/checks/lookup", json={"phone": "01700000002"})
        ).json()["data"]

        assert looked_up["risk_score"] == events_named(events, "score")[0]["score"]

    async def test_an_invalid_number_is_a_422(self, auth_client: AsyncClient):
        response = await auth_client.post("/checks/lookup", json={"phone": "01212345678"})

        assert response.status_code == 422

    async def test_a_lookup_needs_authentication(self, client: AsyncClient):
        assert (await client.post("/checks/lookup", json={"phone": PHONE})).status_code == 401


class TestCaching:
    async def test_ac_6_8_a_recent_check_is_served_from_cache(
        self, auth_client: AsyncClient, stub_registry
    ):
        """AC-6.8 — a cache hit still streams, and makes zero provider calls."""
        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})

        first = (await create_check(auth_client)).json()["data"]
        await consume_stream(auth_client, first["stream_url"])
        assert adapter.calls == 1

        second = (await create_check(auth_client)).json()["data"]
        events = await consume_stream(auth_client, second["stream_url"])

        assert adapter.calls == 1
        assert events_named(events, "provider_result")[0]["status"] == "ok"
        assert events_named(events, "score")[0]["cached"] is True

    async def test_ac_11_4_the_same_number_checked_twice_gives_the_same_answer(
        self, auth_client: AsyncClient, redis_client
    ):
        """AC-11.4 — with the cache cleared, mock results are still identical."""
        first = (await create_check(auth_client)).json()["data"]
        first_events = await consume_stream(auth_client, first["stream_url"])

        await redis_client.delete(
            check_cache_key((await auth_client.get("/auth/me")).json()["data"]["public_id"], PHONE)
        )
        second = (await create_check(auth_client)).json()["data"]
        second_events = await consume_stream(auth_client, second["stream_url"])

        assert (
            events_named(first_events, "score")[0]["score"]
            == (events_named(second_events, "score")[0]["score"])
        )

    async def test_a_check_where_nothing_answered_is_not_cached(
        self, auth_client: AsyncClient, redis_client, merchant: User
    ):
        """Fifteen minutes of "everyone is down" would outlive the outage that caused it."""
        created = (await create_check(auth_client, phone="01700000005")).json()["data"]

        await consume_stream(auth_client, created["stream_url"])

        assert not await redis_client.exists(check_cache_key(merchant.public_id, "01700000005"))


class TestMockScenarios:
    async def test_ac_11_2_the_reserved_risky_number_scores_high(self, auth_client: AsyncClient):
        """AC-11.2 — 01700000002 is the demo's bad customer."""
        created = (await create_check(auth_client, phone="01700000002")).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        assert events_named(events, "score")[0]["band"] == RiskBand.HIGH.value

    async def test_the_reserved_clean_number_scores_low(self, auth_client: AsyncClient):
        created = (await create_check(auth_client, phone="01700000001")).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        assert events_named(events, "score")[0]["band"] == RiskBand.LOW.value

    async def test_ac_11_3_the_reserved_number_times_out_on_exactly_one_provider(
        self, auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """AC-11.3 — one provider times out; the others answer normally."""
        from src.config import settings

        monkeypatch.setattr(settings, "PROVIDER_TIMEOUT_SECONDS", 0.2)
        created = (await create_check(auth_client, phone="01700000004")).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        statuses = {
            item["provider"]: item["status"] for item in events_named(events, "provider_result")
        }
        assert statuses == {"pathao": "timeout", "redx": "ok", "steadfast": "ok"}

    async def test_ac_7_6_the_reserved_number_where_providers_disagree(
        self, auth_client: AsyncClient
    ):
        """AC-7.6 end to end — the demo number that produces an ``uncertain`` score."""
        created = (await create_check(auth_client, phone="01700000006")).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        assert events_named(events, "score")[0]["uncertain"] is True
