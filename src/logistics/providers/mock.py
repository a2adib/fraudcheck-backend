"""
Deterministic mock courier (FR-11).

The point of mock mode is that a reviewer can clone the repo and run the whole service
with no courier credentials at all (AC-11.1). That only works if the mocks are honest
about the *shape* of the thing they replace: the mock builds the same raw payload the
real Pathao adapter parses, then normalises it through the same code path.

Everything is derived from a hash of the phone number, so the same input always yields
the same result (FR-11.2, AC-11.4) — no ``random``, no seeds to thread around, nothing
that changes between processes.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderError, ProviderParseError, ProviderTimeout
from src.logistics.providers.base import HttpCourierAdapter, as_int, derive_returned
from src.logistics.schemas import DecryptedCredential, DeliveryStats, RawResult

logger = logging.getLogger(__name__)

_MIN_LATENCY_MS = 200
_MAX_LATENCY_MS = 3000

_HIGH_RATING_MAX_RETURN_RATIO = 0.1
_MEDIUM_RATING_MAX_RETURN_RATIO = 0.4


@dataclass(frozen=True)
class MockScenario:
    """One provider's answer for one phone number."""

    total_orders: int = 0
    delivered: int = 0
    fails: bool = False
    times_out: bool = False


_CLEAN = MockScenario(total_orders=20, delivered=20)
_RISKY = MockScenario(total_orders=20, delivered=2)
_EMPTY = MockScenario()
_BEHAVIOR_CLEAN = MockScenario(total_orders=12, delivered=12)

#: FR-11.4. The reserved numbers, and what each one is *for*. Numbers 7-10 exercise the
#: behaviour detectors (FR-16) rather than the courier layer, so their courier history
#: is deliberately clean — an anomaly flag must be able to fire on its own.
RESERVED_SCENARIOS: dict[str, dict[ProviderEnum, MockScenario]] = {
    # Clean history, low risk
    "01700000001": dict.fromkeys(ProviderEnum, _CLEAN),
    # High return rate, high risk
    "01700000002": dict.fromkeys(ProviderEnum, _RISKY),
    # No records anywhere
    "01700000003": dict.fromkeys(ProviderEnum, _EMPTY),
    # Exactly one provider times out, the rest answer (AC-11.3)
    "01700000004": {
        ProviderEnum.PATHAO: MockScenario(times_out=True),
        ProviderEnum.STEADFAST: _CLEAN,
        ProviderEnum.REDX: _CLEAN,
    },
    # Every provider fails — the check must still return (AC-6.5)
    "01700000005": dict.fromkeys(ProviderEnum, MockScenario(fails=True)),
    # Providers disagree materially, so the score is flagged uncertain (AC-7.6)
    "01700000006": {
        ProviderEnum.PATHAO: MockScenario(total_orders=10, delivered=1),
        ProviderEnum.STEADFAST: MockScenario(total_orders=10, delivered=5),
        ProviderEnum.REDX: MockScenario(total_orders=10, delivered=9),
    },
    "01700000007": dict.fromkeys(ProviderEnum, _BEHAVIOR_CLEAN),
    "01700000008": dict.fromkeys(ProviderEnum, _BEHAVIOR_CLEAN),
    "01700000009": dict.fromkeys(ProviderEnum, _BEHAVIOR_CLEAN),
    "01700000010": dict.fromkeys(ProviderEnum, _BEHAVIOR_CLEAN),
}


def mock_credential(provider: ProviderEnum, user_public_id: str) -> DecryptedCredential:
    """
    Build a stand-in credential for mock mode (AC-11.1).

    Mock mode has to work for a merchant who has connected nothing at all, so the
    orchestrator hands the mock this instead of reporting ``no_credential``. It is never
    sent anywhere: ``MockAdapter`` ignores it.
    """
    return DecryptedCredential(
        provider=provider,
        user_public_id=user_public_id,
        username=SecretStr("mock"),
        password=SecretStr("mock"),
    )


def _digest(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode()).digest()[:8], "big")


def _fraction(*parts: str) -> float:
    """Derive a stable float in ``[0, 1)`` from these inputs."""
    return (_digest(*parts) % 10_000) / 10_000


def _default_scenario(phone: str, provider: ProviderEnum) -> MockScenario:
    """
    Give an unreserved number a plausible, stable history.

    Providers see *different* slices of the same customer, as they do in reality — a
    merchant does not ship every order with every courier — so the totals differ per
    provider while the customer's underlying return behaviour does not.
    """
    total_orders = _digest(phone, provider.value, "total") % 26
    return_ratio = _fraction(phone, "returns")
    delivered = total_orders - int(total_orders * return_ratio)
    return MockScenario(total_orders=total_orders, delivered=delivered)


def scenario_for(phone: str, provider: ProviderEnum) -> MockScenario:
    reserved = RESERVED_SCENARIOS.get(phone)
    if reserved:
        return reserved[provider]
    if _fraction(phone, provider.value, "failure") < settings.MOCK_FAILURE_RATE:
        return MockScenario(fails=True)
    return _default_scenario(phone, provider)


def _rating(total_orders: int, delivered: int) -> str | None:
    """Mimic Pathao's coarse word-grade rating, derived from the same numbers."""
    if total_orders == 0:
        return None
    return_ratio = (total_orders - delivered) / total_orders
    if return_ratio <= _HIGH_RATING_MAX_RETURN_RATIO:
        return "Good"
    if return_ratio <= _MEDIUM_RATING_MAX_RETURN_RATIO:
        return "Average"
    return "Poor"


class MockAdapter(HttpCourierAdapter):
    """
    Stands in for any provider. One class, because the mock's job is the *contract*.

    It emits Pathao's payload shape for every provider: the orchestrator and the
    scoring code only ever see ``DeliveryStats``, so the shape behind it is free, and
    reusing one keeps the mock from drifting into a second, fictional API to maintain.
    """

    def __init__(self, provider: ProviderEnum, timeout_seconds: float | None = None) -> None:
        """Build a mock for one provider."""
        super().__init__(timeout_seconds)
        self.name = provider

    async def _call(
        self,
        phone: str,
        credential: DecryptedCredential,  # noqa: ARG002 — mock mode never authenticates
    ) -> dict[str, Any]:
        scenario = scenario_for(phone, self.name)

        if settings.MOCK_LATENCY_ENABLED:
            span = _MAX_LATENCY_MS - _MIN_LATENCY_MS
            latency_ms = _MIN_LATENCY_MS + _digest(phone, self.name.value, "latency") % span
            await asyncio.sleep(latency_ms / 1000)

        if scenario.times_out:
            # Sleeping past the deadline rather than raising directly, so the timeout
            # is produced by the same machinery a real slow provider would trip.
            await asyncio.sleep(self.timeout_seconds + 1)
            msg = f"{self.name.value}: mock timeout"
            raise ProviderTimeout(msg)

        if scenario.fails:
            msg = f"{self.name.value}: mock provider failure"
            raise ProviderError(msg)

        return {
            "data": {
                "customer": {
                    "total_delivery": scenario.total_orders,
                    "successful_delivery": scenario.delivered,
                },
                "customer_rating": _rating(scenario.total_orders, scenario.delivered),
            }
        }

    def normalize(self, raw: RawResult) -> DeliveryStats:
        data = raw.payload.get("data")
        if not isinstance(data, dict):
            msg = f"{self.name.value}: mock response carried no 'data' object"
            raise ProviderParseError(msg)
        customer = data["customer"]
        total_orders = as_int(customer.get("total_delivery"), "total_delivery", self.name)
        delivered = as_int(customer.get("successful_delivery"), "successful_delivery", self.name)
        rating = data.get("customer_rating")
        return DeliveryStats(
            total_orders=total_orders,
            delivered=delivered,
            returned=derive_returned(total_orders, delivered),
            cancelled=0,
            customer_rating=str(rating) if rating else None,
        )
