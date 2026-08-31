"""
Courier risk scoring (FR-7).

Deterministic and explainable, by design. Every number in the output can be traced to
provider counts the merchant could verify by hand, which is the whole reason this — and
not the ML layer (FR-17) — is what a decision is allowed to rest on.

The interesting part is ``confidence``. A customer with two orders and one return has a
50% return rate, and treating that as a 50-risk-point signal would be arithmetic
pretending to be evidence. Instead the raw ratio is blended toward a neutral 50 in
proportion to how much history exists, so a thin file scores "we don't know" rather than
"suspicious" (FR-7.3).
"""

from collections.abc import Mapping

from pydantic import BaseModel

from src.checks.enums import RiskBand
from src.logistics.enums import ProviderEnum
from src.logistics.schemas import DeliveryStats

NEUTRAL_SCORE = 50.0

#: Full confidence at ten orders. Below that the score is pulled toward neutral.
CONFIDENCE_SATURATION_ORDERS = 10

#: A provider needs at least this much history before its opinion counts toward the
#: disagreement check — two providers each holding one order will "disagree" by 100
#: points and mean nothing by it.
MIN_ORDERS_FOR_DISAGREEMENT = 3

#: FR-7.5. Points of spread between providers before the score is flagged uncertain.
DISAGREEMENT_THRESHOLD = 30.0

BAND_LOW_MAX = 25.0
BAND_MEDIUM_MAX = 60.0


class ProviderContribution(BaseModel):
    """One provider's share of the score, so the breakdown adds up in public."""

    provider: ProviderEnum
    total: int
    delivered: int
    returned: int
    base_risk: float


class RiskScore(BaseModel):
    """FR-7.4. The score and everything needed to argue with it."""

    score: float
    band: RiskBand
    delivered: int
    returned: int
    total: int
    return_ratio: float
    confidence: float
    insufficient_data: bool
    uncertain: bool
    contributing_providers: list[ProviderEnum]
    breakdown: list[ProviderContribution]


def band_for(score: float) -> RiskBand:
    """FR-7.2 — low 0-25, medium 26-60, high 61-100."""
    if score <= BAND_LOW_MAX:
        return RiskBand.LOW
    if score <= BAND_MEDIUM_MAX:
        return RiskBand.MEDIUM
    return RiskBand.HIGH


def score_courier_history(stats_by_provider: Mapping[ProviderEnum, DeliveryStats]) -> RiskScore:
    """
    Score the courier dimension from the providers that answered.

    Callers pass only providers whose leg returned ``ok``; a provider that timed out
    contributes nothing rather than contributing zeros, which would read as a spotless
    delivery history (AC-7.7).
    """
    delivered = sum(stats.delivered for stats in stats_by_provider.values())
    returned = sum(stats.returned + stats.cancelled for stats in stats_by_provider.values())
    total = delivered + returned

    breakdown = _breakdown(stats_by_provider)
    contributing = sorted(stats_by_provider, key=lambda provider: provider.value)

    if total == 0:
        # No history at all — including the case where every provider answered and each
        # said "never seen this number". Neutral, and flagged so the caller can say why.
        return RiskScore(
            score=NEUTRAL_SCORE,
            band=band_for(NEUTRAL_SCORE),
            delivered=0,
            returned=0,
            total=0,
            return_ratio=0.0,
            confidence=0.0,
            insufficient_data=True,
            uncertain=False,
            contributing_providers=contributing,
            breakdown=breakdown,
        )

    return_ratio = returned / total
    base_risk = return_ratio * 100
    confidence = min(1.0, total / CONFIDENCE_SATURATION_ORDERS)
    score = base_risk * confidence + NEUTRAL_SCORE * (1 - confidence)

    return RiskScore(
        # Rounded so the same inputs serialise to the same bytes every time (AC-7.8);
        # float arithmetic is deterministic, its decimal expansion is just noisy.
        score=round(score, 2),
        band=band_for(score),
        delivered=delivered,
        returned=returned,
        total=total,
        return_ratio=round(return_ratio, 4),
        confidence=round(confidence, 4),
        insufficient_data=False,
        uncertain=_is_uncertain(breakdown),
        contributing_providers=contributing,
        breakdown=breakdown,
    )


def _breakdown(
    stats_by_provider: Mapping[ProviderEnum, DeliveryStats],
) -> list[ProviderContribution]:
    contributions = []
    for provider in sorted(stats_by_provider, key=lambda member: member.value):
        stats = stats_by_provider[provider]
        provider_returned = stats.returned + stats.cancelled
        provider_total = stats.delivered + provider_returned
        base_risk = (provider_returned / provider_total * 100) if provider_total else 0.0
        contributions.append(
            ProviderContribution(
                provider=provider,
                total=provider_total,
                delivered=stats.delivered,
                returned=provider_returned,
                base_risk=round(base_risk, 2),
            )
        )
    return contributions


def _is_uncertain(breakdown: list[ProviderContribution]) -> bool:
    """FR-7.5. Two providers telling materially different stories is worth surfacing."""
    opinions = [
        contribution.base_risk
        for contribution in breakdown
        if contribution.total >= MIN_ORDERS_FOR_DISAGREEMENT
    ]
    if len(opinions) < 2:  # noqa: PLR2004 — one opinion cannot disagree with itself
        return False
    return max(opinions) - min(opinions) > DISAGREEMENT_THRESHOLD
