"""Check persistence (FR-6.7, spec §4)."""

from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlmodel import Field, SQLModel

from src.checks.enums import CheckSource, RiskBand
from src.common.mixins import CommonFieldMixin
from src.logistics.enums import ProviderEnum, ProviderErrorCode, ProviderStatus


def _enum_column(
    enum_type: type[StrEnum], *, nullable: bool = False, server_default: str | None = None
) -> sa.Column:  # type: ignore[type-arg]
    """Store the lowercase *values*, not SQLAlchemy's default of the member names."""
    return sa.Column(
        sa.Enum(enum_type, values_callable=lambda enum: [member.value for member in enum]),
        nullable=nullable,
        server_default=server_default,
    )


class CheckRequest(CommonFieldMixin, SQLModel, table=True):
    """
    One customer lookup, and the score it produced.

    ``phone_normalized`` is stored in canonical ``01XXXXXXXXX`` form (FR-6.9) so that
    history lookups, cache keys and behaviour aggregation all agree on what "the same
    customer" means.
    """

    __table_args__ = (
        sa.Index("ix_check_request_user_created_at", "user_id", sa.text("created_at DESC")),
        sa.Index("ix_check_request_user_phone", "user_id", "phone_normalized"),
    )

    user_id: int = Field(foreign_key="user.id", nullable=False)
    phone_normalized: str = Field(nullable=False, max_length=11)
    source: CheckSource = Field(
        default=CheckSource.WEB,
        sa_column=_enum_column(CheckSource, server_default=CheckSource.WEB.value),
    )

    # Null until the fan-out completes — a check that was created but never streamed
    # has no score, and must not be shown as though it scored zero.
    risk_score: float | None = Field(default=None, nullable=True)
    risk_band: RiskBand | None = Field(
        default=None, sa_column=_enum_column(RiskBand, nullable=True)
    )


class ProviderResult(CommonFieldMixin, SQLModel, table=True):
    """One provider's leg of a check, including the legs that failed."""

    check_request_id: int = Field(foreign_key="checkrequest.id", nullable=False, index=True)
    provider: ProviderEnum = Field(sa_column=_enum_column(ProviderEnum))
    status: ProviderStatus = Field(sa_column=_enum_column(ProviderStatus))

    total_orders: int | None = Field(default=None, nullable=True)
    delivered: int | None = Field(default=None, nullable=True)
    returned: int | None = Field(default=None, nullable=True)
    cancelled: int | None = Field(default=None, nullable=True)
    customer_rating: str | None = Field(default=None, nullable=True)

    latency_ms: int = Field(default=0, nullable=False)
    error_code: ProviderErrorCode | None = Field(
        default=None, sa_column=_enum_column(ProviderErrorCode, nullable=True)
    )
    raw_payload: dict[str, Any] | None = Field(
        default=None, sa_column=sa.Column(postgresql.JSONB, nullable=True)
    )
