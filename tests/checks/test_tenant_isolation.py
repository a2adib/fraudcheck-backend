"""
The tenant-isolation suite (NFR-3, §1.3).

Every one of these would pass just as well if the isolation were removed and the tests
were written loosely — so they are written tightly: a cache entry, a credential and a
stored check each get read *by the wrong tenant*, and each must come back empty.
"""

import pytest
from httpx import AsyncClient

from src.checks.keys import check_cache_key
from src.logistics.enums import ProviderEnum
from src.logistics.keys import token_data_key
from src.users.models import User
from tests.checks.conftest import consume_stream, create_check, events_named
from tests.checks.test_checks import StubAdapter
from tests.conftest import OTHER_MERCHANT_EMAIL, bearer_headers

PHONE = "01712345678"


@pytest.fixture
async def other_headers(auth_client: AsyncClient, other_merchant: User) -> dict[str, str]:
    return await bearer_headers(auth_client, email=OTHER_MERCHANT_EMAIL)


class TestCacheIsolation:
    async def test_ac_6_9_a_cached_result_is_never_served_to_another_tenant(
        self, auth_client: AsyncClient, other_headers: dict[str, str], stub_registry
    ):
        """AC-6.9 — tenant B's request must reach the providers, not tenant A's cache."""
        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})

        first = (await create_check(auth_client)).json()["data"]
        await consume_stream(auth_client, first["stream_url"])
        assert adapter.calls == 1

        second = (await create_check(auth_client, headers=other_headers)).json()["data"]
        events = await consume_stream(auth_client, second["stream_url"], headers=other_headers)

        assert adapter.calls == 2
        assert events_named(events, "score")[0]["cached"] is False

    async def test_a_lookup_does_not_read_another_tenants_cache(
        self, auth_client: AsyncClient, other_headers: dict[str, str], stub_registry
    ):
        """The dashboard endpoint takes the same tenant-namespaced cache path."""
        adapter = StubAdapter(ProviderEnum.PATHAO)
        stub_registry({ProviderEnum.PATHAO: adapter})

        await auth_client.post("/checks/lookup", json={"phone": PHONE})
        response = await auth_client.post(
            "/checks/lookup", json={"phone": PHONE}, headers=other_headers
        )

        assert adapter.calls == 2
        assert response.json()["data"]["cached"] is False

    async def test_the_two_tenants_write_two_cache_keys(
        self, auth_client: AsyncClient, merchant: User, other_merchant: User, redis_client
    ):
        assert check_cache_key(merchant.public_id, PHONE) != check_cache_key(
            other_merchant.public_id, PHONE
        )


class TestHistoryIsolation:
    async def test_ac_8_4_another_tenants_check_is_a_404_not_a_403(
        self, auth_client: AsyncClient, other_headers: dict[str, str]
    ):
        """AC-8.4 — a 403 would confirm the check exists; a 404 says nothing at all."""
        created = (await create_check(auth_client)).json()["data"]

        response = await auth_client.get(f"/checks/{created['check_id']}", headers=other_headers)

        assert response.status_code == 404

    async def test_another_tenant_cannot_open_the_stream_either(
        self, auth_client: AsyncClient, other_headers: dict[str, str]
    ):
        created = (await create_check(auth_client)).json()["data"]

        response = await auth_client.get(created["stream_url"], headers=other_headers)

        assert response.status_code == 404


class TestCredentialIsolation:
    async def test_one_tenants_credential_does_not_authorise_anothers_check(
        self,
        auth_client: AsyncClient,
        other_headers: dict[str, str],
        stub_registry,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The merchant who connected Pathao is the only one who gets Pathao results."""
        from src.config import settings

        monkeypatch.setattr(settings, "MOCK_MODE", False)
        stub_registry({ProviderEnum.PATHAO: StubAdapter(ProviderEnum.PATHAO)})
        await auth_client.post(
            "/credentials",
            json={"provider": "pathao", "username": "a-user", "password": "a-password"},
        )

        created = (await create_check(auth_client, headers=other_headers)).json()["data"]
        events = await consume_stream(auth_client, created["stream_url"], headers=other_headers)

        assert events_named(events, "provider_result")[0]["error_code"] == "no_credential"

    async def test_provider_tokens_are_namespaced_per_tenant(
        self, merchant: User, other_merchant: User
    ):
        assert token_data_key(ProviderEnum.PATHAO, merchant.public_id) != token_data_key(
            ProviderEnum.PATHAO, other_merchant.public_id
        )
