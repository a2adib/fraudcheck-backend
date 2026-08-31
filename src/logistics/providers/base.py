"""
The adapter contract every courier is implemented behind (FR-3).

Adding a fourth courier is one new file plus one ``register()`` call — the
orchestrator, the router and the schemas never learn its name (AC-3.4).
"""

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderError, ProviderParseError, ProviderTimeout
from src.logistics.schemas import DecryptedCredential, DeliveryStats, RawResult

logger = logging.getLogger(__name__)


@runtime_checkable
class CourierAdapter(Protocol):
    """
    One courier's lookup, split into I/O and parsing.

    ``fetch`` is deliberately the only async half: keeping ``normalize`` pure means the
    response-shape tests (AC-3.2, AC-3.3) run against recorded fixtures with no event
    loop, no network and no mocking.
    """

    name: ProviderEnum
    timeout_seconds: float

    async def fetch(self, phone: str, credential: DecryptedCredential) -> RawResult: ...

    def normalize(self, raw: RawResult) -> DeliveryStats: ...


class HttpCourierAdapter:
    """
    Shared plumbing for adapters that talk HTTP: timeout enforcement and latency.

    Subclasses implement ``_call``; everything about *how long it may take* and *what a
    timeout looks like to the caller* is settled here, once (FR-3.4, AC-3.5).
    """

    name: ProviderEnum

    def __init__(self, timeout_seconds: float | None = None) -> None:
        """Build the adapter, defaulting to the configured provider timeout (FR-3.4)."""
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.PROVIDER_TIMEOUT_SECONDS
        )

    async def fetch(self, phone: str, credential: DecryptedCredential) -> RawResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            # Belt and braces: httpx's own timeout covers the request, and this covers
            # everything else in the leg — a token refresh, a retry, a slow DNS lookup —
            # so the adapter cannot outlive its declared budget by more than a tick.
            async with asyncio.timeout(self.timeout_seconds):
                payload = await self._call(phone, credential)
        except (TimeoutError, httpx.TimeoutException) as exc:
            msg = f"{self.name.value}: timed out after {self.timeout_seconds}s"
            raise ProviderTimeout(msg) from exc

        latency_ms = int((loop.time() - started) * 1000)
        return RawResult(provider=self.name, payload=payload, latency_ms=latency_ms)

    async def _call(self, phone: str, credential: DecryptedCredential) -> dict[str, Any]:
        raise NotImplementedError

    def normalize(self, raw: RawResult) -> DeliveryStats:
        raise NotImplementedError


def as_int(value: Any, field: str, provider: ProviderEnum) -> int:  # noqa: ANN401
    """
    Coerce a provider's count to ``int``, or fail with the field that broke (AC-3.3).

    ``bool`` is rejected explicitly — it is an ``int`` subclass, and a provider sending
    ``true`` where a count belongs is a shape change worth noticing, not a 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        msg = f"{provider.value}: expected a number for {field}, got {type(value).__name__}"
        raise ProviderParseError(msg)
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        msg = f"{provider.value}: {field} is not a number"
        raise ProviderParseError(msg) from exc


def derive_returned(total_orders: int, delivered: int) -> int:
    """
    FR-3 / AC-3.6. Neither known provider reports returns, so they are the remainder.

    Clamped at zero because a provider that reports more deliveries than orders is
    telling us something inconsistent, and a negative return count would flow straight
    into the risk score as a *negative* return ratio.
    """
    return max(total_orders - delivered, 0)


__all__ = [
    "CourierAdapter",
    "HttpCourierAdapter",
    "ProviderError",
    "as_int",
    "derive_returned",
]
