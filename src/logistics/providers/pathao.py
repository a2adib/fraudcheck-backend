"""
Pathao merchant-panel customer lookup (FR-3).

Ported from ``govaly-backend/src/logistics/services.py:240``, where it runs in
production today against ``POST /api/v1/user/success``. What changes here: the token
comes from the *merchant's own* credential rather than a platform-wide login, a 401
retries once against a fresh token instead of giving up, and a malformed payload
raises ``ProviderParseError`` rather than silently normalising to zeros — which
upstream does, and which would read as "this customer has no history" (a *low* risk
signal) when in fact the response shape changed.
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

BEHAVIOR_PATH = "/api/v1/user/success"
_AUTH_STATUSES = (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
_MAX_ATTEMPTS = 2


class PathaoAdapter(HttpCourierAdapter):
    name = ProviderEnum.PATHAO

    async def _call(self, phone: str, credential: DecryptedCredential) -> dict[str, Any]:
        manager = token_manager_for(credential)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                token = await manager.get_token()
                response = await client.post(
                    f"{settings.PATHAO_MERCHANT_URL}{BEHAVIOR_PATH}",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    json={"phone": phone},
                )

                if response.status_code in _AUTH_STATUSES:
                    # The cached token may simply have been revoked at the portal. Drop
                    # it and let one retry decide whether the *credential* is bad.
                    await manager.invalidate()
                    if attempt < _MAX_ATTEMPTS:
                        logger.info("Pathao rejected a cached token, retrying with a fresh login")
                        continue
                    msg = "Pathao rejected the merchant credential"
                    raise ProviderAuthError(msg)

                if response.is_error:
                    msg = f"Pathao returned status {response.status_code}"
                    raise ProviderError(msg)

                try:
                    payload = response.json()
                except ValueError as exc:
                    msg = "Pathao returned a non-JSON body"
                    raise ProviderParseError(msg) from exc

                if not isinstance(payload, dict):
                    msg = "Pathao returned a JSON body that is not an object"
                    raise ProviderParseError(msg)
                return payload

        # Unreachable: the loop either returns or raises. Present so the type checker
        # does not have to take that on trust.
        msg = "Pathao lookup exhausted its attempts"  # pragma: no cover
        raise ProviderError(msg)  # pragma: no cover

    def normalize(self, raw: RawResult) -> DeliveryStats:
        """
        ``data.customer.{total_delivery,successful_delivery}`` + ``data.customer_rating``.

        Note the asymmetry in the upstream payload: the counts sit under ``customer``
        while the rating sits one level up, beside it.
        """
        data = raw.payload.get("data")
        if not isinstance(data, dict):
            msg = "Pathao response carried no 'data' object"
            raise ProviderParseError(msg)

        customer = data.get("customer")
        if not isinstance(customer, dict):
            msg = "Pathao response carried no 'data.customer' object"
            raise ProviderParseError(msg)

        total_orders = as_int(customer.get("total_delivery"), "total_delivery", self.name)
        delivered = as_int(customer.get("successful_delivery"), "successful_delivery", self.name)

        rating = data.get("customer_rating")
        return DeliveryStats(
            total_orders=total_orders,
            delivered=delivered,
            returned=derive_returned(total_orders, delivered),
            cancelled=0,
            customer_rating=str(rating) if rating not in (None, "") else None,
        )
