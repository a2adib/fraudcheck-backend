"""Shared courier-layer fixtures."""

import pytest
from pydantic import SecretStr

from src.logistics.enums import ProviderEnum
from src.logistics.schemas import DecryptedCredential

MERCHANT_URL = "https://merchant.pathao.test"
LOGIN_URL = f"{MERCHANT_URL}/api/v1/login"
BEHAVIOR_URL = f"{MERCHANT_URL}/api/v1/user/success"

# RedX logs in on one host and answers lookups on another, so the two URLs are built
# from two settings — a single base would hide the split the adapter has to get right.
REDX_API_URL = "https://redx-api.test"
REDX_PANEL_URL = "https://redx.test"
REDX_LOGIN_URL = f"{REDX_API_URL}/v4/auth/login"
REDX_BEHAVIOR_URL = f"{REDX_PANEL_URL}/api/redx_se/admin/parcel/customer-success-return-rate"

TENANT = "tenant00001"


def credential_for(
    tenant: str = TENANT, provider: ProviderEnum = ProviderEnum.PATHAO
) -> DecryptedCredential:
    return DecryptedCredential(
        provider=provider,
        user_public_id=tenant,
        username=SecretStr("portal-user"),
        password=SecretStr("portal-password"),
    )


@pytest.fixture
def credential() -> DecryptedCredential:
    return credential_for()


@pytest.fixture(autouse=True)
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real adapters in the courier layer's own tests, not the mocks."""
    from src.config import settings

    monkeypatch.setattr(settings, "MOCK_MODE", False)
