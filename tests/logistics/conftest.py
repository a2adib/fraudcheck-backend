"""Shared courier-layer fixtures."""

import pytest
from pydantic import SecretStr

from src.logistics.enums import ProviderEnum
from src.logistics.schemas import DecryptedCredential

MERCHANT_URL = "https://merchant.pathao.test"
LOGIN_URL = f"{MERCHANT_URL}/api/v1/login"
BEHAVIOR_URL = f"{MERCHANT_URL}/api/v1/user/success"
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
