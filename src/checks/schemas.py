"""Check request/response and SSE payload schemas (FR-6)."""

from datetime import datetime

from pydantic import BaseModel, Field

from src.checks.enums import CheckSource, RiskBand
from src.checks.scoring import RiskScore
from src.logistics.enums import ProviderEnum, ProviderErrorCode, ProviderStatus


class CheckCreate(BaseModel):
    """Anything a merchant might paste; normalised server-side (FR-6.9, AC-6.6)."""

    phone: str = Field(min_length=1)
    source: CheckSource = CheckSource.WEB


class CheckCreatedOut(BaseModel):
    """AC-6.1. The client gets an id and where to listen, not a result."""

    check_id: str
    stream_url: str
    phone_masked: str


class ProviderResultOut(BaseModel):
    """FR-6.6. One provider's leg, ok or not."""

    provider: ProviderEnum
    status: ProviderStatus
    error_code: ProviderErrorCode | None = None
    total_orders: int | None = None
    delivered: int | None = None
    returned: int | None = None
    cancelled: int | None = None
    customer_rating: str | None = None
    latency_ms: int = 0


class CheckStartedOut(BaseModel):
    check_id: str
    phone_masked: str
    providers: list[ProviderEnum]


class CheckDoneOut(BaseModel):
    check_id: str
    risk_score: float | None = None
    risk_band: RiskBand | None = None


class CachedCheck(BaseModel):
    """
    What gets cached for FR-6.8, and nothing more.

    Note what is absent: the phone number. It is already in the key, and a cache value
    that also carried it would be a second place to leak it from.
    """

    providers: list[ProviderResultOut]
    score: RiskScore


class CheckResultOut(BaseModel):
    """
    A completed check, in one response — what a dashboard renders.

    Carries the same numbers the stream emits across four events, plus the score
    breakdown that only lives in the ``score`` event. ``cached`` says whether the
    couriers were actually called, so a dashboard can show "as of a moment ago" honestly.
    """

    check_id: str
    phone_masked: str
    source: CheckSource
    created_at: datetime
    risk_score: float
    risk_band: RiskBand
    cached: bool
    score: RiskScore
    providers: list[ProviderResultOut]


class CheckDetailOut(BaseModel):
    """FR-8.4 in miniature — the stored result, for a client that missed the stream."""

    check_id: str
    phone_masked: str
    source: CheckSource
    created_at: datetime
    risk_score: float | None
    risk_band: RiskBand | None
    providers: list[ProviderResultOut]
