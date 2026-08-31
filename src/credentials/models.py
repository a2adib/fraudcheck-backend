"""Credential vault storage (FR-2, spec §4 ``CourierCredential``)."""

from datetime import datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from src.common.mixins import CommonFieldMixin
from src.common.types import TZDateTime
from src.credentials.enums import CredentialStatus
from src.logistics.enums import ProviderEnum


class CourierCredential(CommonFieldMixin, SQLModel, table=True):
    """
    One merchant's login for one courier portal.

    The uniqueness rule (FR-2.1, AC-2.3) is enforced by a *partial* index over active
    rows only. A plain ``UNIQUE (user_id, provider)`` would make the soft delete this
    codebase uses (``is_active = False``) a one-way door: a merchant who removed their
    Pathao credential could never add another one, because the tombstone still holds
    the slot.
    """

    __table_args__ = (
        sa.Index(
            "uq_courier_credential_active_user_provider",
            "user_id",
            "provider",
            unique=True,
            postgresql_where=sa.text("is_active"),
        ),
    )

    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    provider: ProviderEnum = Field(
        sa_column=sa.Column(
            sa.Enum(ProviderEnum, values_callable=lambda enum: [member.value for member in enum]),
            nullable=False,
        )
    )

    # Ciphertext, never plaintext — AC-2.2 inspects these columns directly in Postgres.
    username_encrypted: str = Field(nullable=False)
    password_encrypted: str = Field(nullable=False)
    key_version: int = Field(nullable=False)

    status: CredentialStatus = Field(
        default=CredentialStatus.UNTESTED,
        sa_column=sa.Column(
            sa.Enum(
                CredentialStatus,
                values_callable=lambda enum: [member.value for member in enum],
            ),
            nullable=False,
            server_default=CredentialStatus.UNTESTED.value,
        ),
    )
    last_verified_at: datetime | None = Field(sa_type=TZDateTime, default=None, nullable=True)
