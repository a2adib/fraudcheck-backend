"""FR-2 — the credential vault API: store, mask, verify, delete."""

import logging

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.credentials.enums import CredentialStatus
from src.credentials.models import CourierCredential
from src.users.models import User

USERNAME = "merchant-account@shop.com"
PASSWORD = "pathao-portal-password"
LOGIN_URL = "https://merchant.pathao.test/api/v1/login"
REDX_LOGIN_URL = "https://redx-api.test/v4/auth/login"


async def store_credential(
    client: AsyncClient,
    provider: str = "pathao",
    username: str = USERNAME,
    password: str = PASSWORD,
):
    return await client.post(
        "/credentials",
        json={"provider": provider, "username": username, "password": password},
    )


class TestStoring:
    async def test_ac_2_1_stored_credential_returns_only_masked_details(
        self, auth_client: AsyncClient
    ):
        """AC-2.1 — 201, and the body carries provider, status and a masked username."""
        response = await store_credential(auth_client)

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["provider"] == "pathao"
        assert data["status"] == CredentialStatus.UNTESTED.value
        assert data["username_masked"] == "me***@shop.com"
        assert USERNAME not in response.text
        assert PASSWORD not in response.text

    async def test_ac_2_2_the_row_in_postgres_holds_ciphertext_only(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        """AC-2.2 — inspect the columns directly; no plaintext substring survives."""
        assert (await store_credential(auth_client)).status_code == 201

        credential = (await async_session.exec(select(CourierCredential))).one()

        assert USERNAME not in credential.username_encrypted
        assert PASSWORD not in credential.password_encrypted
        assert credential.key_version == 1
        assert credential.status is CredentialStatus.UNTESTED

    async def test_ac_2_3_a_second_credential_for_one_provider_is_a_409(
        self, auth_client: AsyncClient
    ):
        """AC-2.3 — one credential per provider per merchant."""
        assert (await store_credential(auth_client)).status_code == 201

        assert (await store_credential(auth_client)).status_code == 409

    async def test_a_second_credential_is_allowed_for_a_different_provider(
        self, auth_client: AsyncClient
    ):
        assert (await store_credential(auth_client)).status_code == 201

        assert (await store_credential(auth_client, provider="redx")).status_code == 201

    async def test_listing_returns_masked_usernames(self, auth_client: AsyncClient):
        await store_credential(auth_client)

        response = await auth_client.get("/credentials")

        assert response.status_code == 200
        assert [item["username_masked"] for item in response.json()["data"]] == ["me***@shop.com"]
        assert USERNAME not in response.text


class TestVerification:
    @pytest.fixture(autouse=True)
    def live_mode(self, monkeypatch: pytest.MonkeyPatch):
        """Verification only talks to a provider outside mock mode."""
        from src.config import settings

        monkeypatch.setattr(settings, "MOCK_MODE", False)

    @respx.mock
    async def test_ac_2_4_a_working_credential_becomes_valid(self, auth_client: AsyncClient):
        """AC-2.4 — the provider accepts the login, so status flips to valid."""
        respx.post(LOGIN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        await store_credential(auth_client)

        response = await auth_client.post("/credentials/pathao/verify")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == CredentialStatus.VALID.value
        assert data["last_verified_at"] is not None

    @respx.mock
    async def test_ac_2_5_a_rejected_password_is_a_200_saying_invalid(
        self, auth_client: AsyncClient
    ):
        """AC-2.5 — the request succeeded; its answer is that the credential does not work."""
        respx.post(LOGIN_URL).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        await store_credential(auth_client)

        response = await auth_client.post("/credentials/pathao/verify")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == CredentialStatus.INVALID.value

    @respx.mock
    async def test_an_unreachable_provider_is_a_502_and_leaves_status_alone(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        """A wrong password and an unreachable portal must not look the same."""
        respx.post(LOGIN_URL).mock(side_effect=httpx.ConnectError("boom"))
        await store_credential(auth_client)

        response = await auth_client.post("/credentials/pathao/verify")

        assert response.status_code == 502
        credential = (await async_session.exec(select(CourierCredential))).one()
        await async_session.refresh(credential)
        assert credential.status is CredentialStatus.UNTESTED

    async def test_verifying_a_provider_with_no_credential_is_a_404(self, auth_client: AsyncClient):
        assert (await auth_client.post("/credentials/pathao/verify")).status_code == 404

    async def test_a_provider_with_no_login_yet_is_a_501_not_an_invalid_credential(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        """Steadfast has no login implemented; that is our gap, not the merchant's password."""
        await store_credential(auth_client, provider="steadfast")

        response = await auth_client.post("/credentials/steadfast/verify")

        assert response.status_code == 501
        credential = (await async_session.exec(select(CourierCredential))).one()
        assert credential.status is CredentialStatus.UNTESTED

    @respx.mock
    async def test_a_redx_credential_can_be_verified(self, auth_client: AsyncClient):
        """RedX has a login now, so it is no longer part of the 501 case above."""
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json={"isError": False, "data": {"accessToken": "t"}})
        )
        await store_credential(auth_client, provider="redx")

        response = await auth_client.post("/credentials/redx/verify")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == CredentialStatus.VALID.value

    @respx.mock
    async def test_a_redx_rejection_at_http_200_is_an_invalid_credential(
        self, auth_client: AsyncClient
    ):
        """RedX says no in the body, not the status line — the merchant still sees `invalid`."""
        respx.post(REDX_LOGIN_URL).mock(
            return_value=httpx.Response(200, json={"isError": True, "message": "nope"})
        )
        await store_credential(auth_client, provider="redx")

        response = await auth_client.post("/credentials/redx/verify")

        assert response.status_code == 200
        assert response.json()["data"]["status"] == CredentialStatus.INVALID.value

    async def test_mock_mode_verifies_without_contacting_anyone(
        self, auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """FR-11.1 — the demo has to work with invented credentials."""
        from src.config import settings

        monkeypatch.setattr(settings, "MOCK_MODE", True)
        await store_credential(auth_client)

        response = await auth_client.post("/credentials/pathao/verify")

        assert response.json()["data"]["status"] == CredentialStatus.VALID.value


class TestDeletion:
    async def test_a_deleted_credential_disappears_from_the_list(self, auth_client: AsyncClient):
        await store_credential(auth_client)

        assert (await auth_client.delete("/credentials/pathao")).status_code == 200

        assert (await auth_client.get("/credentials")).json()["data"] == []

    async def test_a_provider_can_be_reconnected_after_deletion(self, auth_client: AsyncClient):
        """The soft delete must not permanently consume the merchant's Pathao slot."""
        await store_credential(auth_client)
        await auth_client.delete("/credentials/pathao")

        assert (await store_credential(auth_client)).status_code == 201


class TestUpdating:
    async def test_a_new_password_resets_the_verified_status(
        self, auth_client: AsyncClient, async_session: AsyncSession
    ):
        await store_credential(auth_client)
        credential = (await async_session.exec(select(CourierCredential))).one()
        credential.status = CredentialStatus.VALID
        async_session.add(credential)
        await async_session.commit()

        response = await auth_client.patch("/credentials/pathao", json={"password": "new-one"})

        assert response.status_code == 200
        assert response.json()["data"]["status"] == CredentialStatus.UNTESTED.value


class TestTenantIsolation:
    async def test_a_merchant_cannot_see_another_merchants_credentials(
        self,
        auth_client: AsyncClient,
        other_merchant: User,
        async_session: AsyncSession,
    ):
        await store_credential(auth_client)
        from tests.conftest import OTHER_MERCHANT_EMAIL, bearer_headers

        headers = await bearer_headers(auth_client, email=OTHER_MERCHANT_EMAIL)

        response = await auth_client.get("/credentials", headers=headers)

        assert response.json()["data"] == []


class TestSecretsNeverLeak:
    @respx.mock
    async def test_ac_2_6_no_plaintext_credential_appears_in_the_logs(
        self,
        auth_client: AsyncClient,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """AC-2.6 — asserted by grepping the captured log output, not by inspection."""
        from src.config import settings

        monkeypatch.setattr(settings, "MOCK_MODE", False)
        # A login that fails is the interesting case: the provider's error body has been
        # observed to echo the submitted username back.
        respx.post(LOGIN_URL).mock(
            return_value=httpx.Response(401, json={"error": f"no such user {USERNAME}"})
        )

        with caplog.at_level(logging.DEBUG):
            await store_credential(auth_client)
            await auth_client.get("/credentials")
            await auth_client.patch("/credentials/pathao", json={"password": PASSWORD})
            await auth_client.post("/credentials/pathao/verify")
            await auth_client.delete("/credentials/pathao")

        captured = caplog.text
        assert USERNAME not in captured
        assert PASSWORD not in captured
