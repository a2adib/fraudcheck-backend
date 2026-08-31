"""
The check pipeline outside mock mode: credentials, the registry, and the breaker.

Mock mode answers every provider unconditionally, which is exactly what hides these
behaviours — so these tests turn it off.
"""

import pytest
from httpx import AsyncClient

from src.logistics.breaker import CircuitBreaker
from src.logistics.enums import ProviderEnum
from src.logistics.providers.registry import register, unregister
from src.users.models import User
from tests.checks.conftest import consume_stream, create_check, events_named
from tests.checks.test_checks import StubAdapter


@pytest.fixture(autouse=True)
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "MOCK_MODE", False)


async def store_credential(client: AsyncClient, provider: str) -> None:
    response = await client.post(
        "/credentials",
        json={"provider": provider, "username": f"{provider}-user", "password": "portal-password"},
    )
    assert response.status_code == 201


class TestCredentialRequirement:
    async def test_ac_2_7_a_provider_with_no_credential_reports_no_credential(
        self, auth_client: AsyncClient, stub_registry
    ):
        """AC-2.7 — and the check still completes, with that provider excluded."""
        stub_registry({ProviderEnum.PATHAO: StubAdapter(ProviderEnum.PATHAO)})
        created = (await create_check(auth_client)).json()["data"]

        events = await consume_stream(auth_client, created["stream_url"])

        result = events_named(events, "provider_result")[0]
        assert (result["status"], result["error_code"]) == ("unavailable", "no_credential")
        assert events_named(events, "score")[0]["insufficient_data"] is True

    async def test_a_deleted_credential_stops_the_provider_being_queried(
        self, auth_client: AsyncClient, stub_registry
    ):
        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})
        await store_credential(auth_client, "pathao")
        await auth_client.delete("/credentials/pathao")

        created = (await create_check(auth_client)).json()["data"]
        events = await consume_stream(auth_client, created["stream_url"])

        assert adapter.calls == 0
        assert events_named(events, "provider_result")[0]["error_code"] == "no_credential"


class TestRegistry:
    async def test_ac_3_4_a_newly_registered_provider_needs_no_other_change(
        self, auth_client: AsyncClient
    ):
        """
        AC-3.4 — register an adapter, and it appears in results.

        Nothing else is touched: no orchestrator change, no router change, no schema
        change. Steadfast stands in for the fourth provider because it is the member
        with no live adapter of its own yet.
        """
        await store_credential(auth_client, "pathao")
        await store_credential(auth_client, "steadfast")
        adapter = StubAdapter(ProviderEnum.STEADFAST, total=20, delivered=20)

        try:
            register(adapter)
            created = (await create_check(auth_client)).json()["data"]
            events = await consume_stream(auth_client, created["stream_url"])
        finally:
            unregister(ProviderEnum.STEADFAST)

        results = {item["provider"]: item for item in events_named(events, "provider_result")}
        assert results["steadfast"]["status"] == "ok"
        assert results["steadfast"]["total_orders"] == 20
        assert adapter.calls == 1


class TestBreakerIntegration:
    async def test_ac_5_1_an_open_circuit_short_circuits_the_leg(
        self, auth_client: AsyncClient, merchant: User, stub_registry
    ):
        """AC-5.1 end to end — ``circuit_open``, and the provider is never called."""
        from src.config import settings

        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})
        await store_credential(auth_client, "pathao")

        breaker = CircuitBreaker(ProviderEnum.PATHAO, merchant.public_id)
        for _ in range(settings.BREAKER_FAILURE_THRESHOLD):
            await breaker.record_failure()

        created = (await create_check(auth_client)).json()["data"]
        events = await consume_stream(auth_client, created["stream_url"])

        assert adapter.calls == 0
        assert events_named(events, "provider_result")[0]["error_code"] == "circuit_open"

    async def test_a_failing_provider_trips_the_breaker_for_that_tenant_only(
        self, auth_client: AsyncClient, merchant: User, stub_registry
    ):
        """FR-5.2 — the failures a real check produces are what open the circuit."""
        from src.config import settings
        from src.logistics.exceptions import ProviderError

        stub_registry(
            {ProviderEnum.PATHAO: StubAdapter(ProviderEnum.PATHAO, error=ProviderError("down"))}
        )
        await store_credential(auth_client, "pathao")

        for index in range(settings.BREAKER_FAILURE_THRESHOLD):
            created = (await create_check(auth_client, phone=f"0171234000{index}")).json()["data"]
            await consume_stream(auth_client, created["stream_url"])

        assert await CircuitBreaker(ProviderEnum.PATHAO, merchant.public_id).state() == "open"
        assert await CircuitBreaker(ProviderEnum.PATHAO, "another-tenant").state() == "closed"
