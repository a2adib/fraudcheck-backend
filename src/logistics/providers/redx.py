"""
RedX merchant-panel customer lookup (FR-3).

Ported from ``govaly-backend/src/logistics/services.py:217``, where it runs against
``GET /api/redx_se/admin/parcel/customer-success-return-rate``. Four things change.

**It is async.** FR-3.6 names this call specifically: upstream runs it on a synchronous
client, and the requirement is a rewrite rather than an ``asyncio.to_thread`` wrapper.

**Failures are failures.** Upstream wraps the whole call in ``except Exception`` and
returns ``None``, so a 500, a timeout and a shape change are indistinguishable from a
customer with no history. Here each becomes its own ``ProviderError`` subclass and the
orchestrator reports the reason (FR-6.4).

**Two hosts.** The token is minted at ``REDX_API_BASE_URL`` and spent here against
``REDX_PANEL_BASE_URL``.

**The phone rides in the query string**, unlike Pathao's JSON body. Nothing in this
module may log the URL or the params — the masking rule is not optional and there is a
test that greps the captured log output.
"""

import logging
from typing import Any

import httpx
from starlette import status

from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderAuthError, ProviderError, ProviderParseError
from src.logistics.providers.base import HttpCourierAdapter, as_int, derive_returned
from src.logistics.schemas import DecryptedCredential, DeliveryStats, RawResult
from src.logistics.token_manager import token_manager_for

logger = logging.getLogger(__name__)

BEHAVIOR_PATH = "/api/redx_se/admin/parcel/customer-success-return-rate"
_AUTH_STATUSES = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
_MAX_ATTEMPTS = 2


def _no_history_payload() -> dict[str, Any]:
    """
    Build the payload for a phone RedX has never carried a parcel for.

    Not an error, and not a *low-risk* answer either: ``score_from`` treats a zero total
    as ``insufficient_data`` at the neutral score, so an unknown customer stays unknown
    rather than scoring clean. Built fresh per call so nothing downstream can mutate a
    shared constant.
    """
    return {"data": {"totalParcels": 0, "deliveredParcels": 0, "customerSegment": None}}


class RedxAdapter(HttpCourierAdapter):
    name = ProviderEnum.REDX

    async def _call(self, phone: str, credential: DecryptedCredential) -> dict[str, Any]:
        manager = token_manager_for(credential)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                token = await manager.get_token()
                response = await client.get(
                    f"{settings.REDX_PANEL_BASE_URL}{BEHAVIOR_PATH}",
                    params={"phoneNumber": phone},
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                )

                if response.status_code in _AUTH_STATUSES:
                    # The cached token may simply have been revoked at the portal. Drop
                    # it and let one retry decide whether the *credential* is bad.
                    await manager.invalidate()
                    if attempt < _MAX_ATTEMPTS:
                        logger.info("RedX rejected a cached token, retrying with a fresh login")
                        continue
                    msg = "RedX rejected the merchant credential"
                    raise ProviderAuthError(msg)

                if response.status_code == status.HTTP_404_NOT_FOUND:
                    return _no_history_payload()

                if response.is_error:
                    msg = f"RedX returned status {response.status_code}"
                    raise ProviderError(msg)

                try:
                    payload = response.json()
                except ValueError as exc:
                    msg = "RedX returned a non-JSON body"
                    raise ProviderParseError(msg) from exc

                if not isinstance(payload, dict):
                    msg = "RedX returned a JSON body that is not an object"
                    raise ProviderParseError(msg)
                return payload

        # Unreachable: the loop either returns or raises. Present so the type checker
        # does not have to take that on trust.
        msg = "RedX lookup exhausted its attempts"  # pragma: no cover
        raise ProviderError(msg)  # pragma: no cover

    def normalize(self, raw: RawResult) -> DeliveryStats:
        """``data.{totalParcels,deliveredParcels,customerSegment}`` — all one level deep."""
        data = raw.payload.get("data")
        if not isinstance(data, dict):
            msg = "RedX response carried no 'data' object"
            raise ProviderParseError(msg)

        total_orders = as_int(data.get("totalParcels"), "totalParcels", self.name)
        delivered = as_int(data.get("deliveredParcels"), "deliveredParcels", self.name)

        # Upstream defaults the segment to ``""``. An empty string would reach the API
        # as a rating a merchant could read something into, so it stays ``None``.
        segment = data.get("customerSegment")
        return DeliveryStats(
            total_orders=total_orders,
            delivered=delivered,
            returned=derive_returned(total_orders, delivered),
            cancelled=0,
            customer_rating=str(segment) if segment not in (None, "") else None,
        )
