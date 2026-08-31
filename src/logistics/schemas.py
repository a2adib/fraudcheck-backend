"""Shapes exchanged between the vault, the adapters and the check orchestrator."""

from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from src.logistics.enums import ProviderEnum


class DecryptedCredential(BaseModel):
    """
    A merchant's courier login, decrypted in memory for exactly one call.

    Both secrets are ``SecretStr`` so that a stray ``repr()``, an exception rendering
    its arguments, or a structlog event dict cannot spill them — FR-2.5 forbids
    plaintext anywhere but the outbound request body, and AC-2.6 greps the log output
    to prove it.

    ``user_public_id`` rides along because every token cache key and breaker key is
    namespaced by tenant (FR-4.6); an adapter must never be handed a credential
    without knowing whose it is.
    """

    model_config = ConfigDict(frozen=True)

    provider: ProviderEnum
    user_public_id: str
    username: SecretStr
    password: SecretStr


class RawResult(BaseModel):
    """A provider's unparsed answer, plus how long it took to arrive."""

    provider: ProviderEnum
    payload: dict[str, Any]
    latency_ms: int


class DeliveryStats(BaseModel):
    """
    The normalized courier history for one phone number, from one provider.

    ``returned`` is derived as ``total_orders - delivered`` for providers that report
    only those two figures, and ``cancelled`` defaults to 0 — a documented assumption
    from FR-3, asserted by AC-3.6.
    """

    total_orders: int
    delivered: int
    returned: int
    cancelled: int
    customer_rating: str | None = None
