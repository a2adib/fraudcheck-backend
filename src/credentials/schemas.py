"""Credential vault request/response schemas (FR-2)."""

from datetime import datetime

from pydantic import BaseModel, Field, SecretStr

from src.credentials.enums import CredentialStatus
from src.logistics.enums import ProviderEnum


class CredentialCreate(BaseModel):
    """
    A new courier login.

    Secrets arrive as ``SecretStr`` so FastAPI's validation errors, which echo the
    submitted body, cannot quote them back (FR-2.5).
    """

    provider: ProviderEnum
    username: SecretStr = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


class CredentialUpdate(BaseModel):
    username: SecretStr | None = Field(default=None, min_length=1)
    password: SecretStr | None = Field(default=None, min_length=1)


class CredentialOut(BaseModel):
    """
    What the API is willing to say about a stored credential (AC-2.1).

    No ciphertext, no key version, no password field at all — only enough for a
    merchant to recognise the account and see whether it works.
    """

    public_id: str
    provider: ProviderEnum
    username_masked: str
    status: CredentialStatus
    last_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CredentialVerifyOut(BaseModel):
    """
    The outcome of testing a credential against the live provider.

    AC-2.5. A rejected password is a *result*, not an error: the request did exactly
    what it promised, and the answer is "these credentials do not work".
    """

    provider: ProviderEnum
    status: CredentialStatus
    last_verified_at: datetime | None
