"""Redis keys owned by the courier layer: provider tokens and breaker state."""

from src.cache.keys import namespaced
from src.logistics.enums import ProviderEnum


def token_data_key(provider: ProviderEnum, user_public_id: str) -> str:
    """FR-4.1. Per tenant, never global: one merchant's token is never another's."""
    return namespaced(f"token:{provider.value}:{user_public_id}")


def token_lock_key(provider: ProviderEnum, user_public_id: str) -> str:
    return namespaced(f"lock:token:{provider.value}:{user_public_id}")


def breaker_key(provider: ProviderEnum, user_public_id: str) -> str:
    """
    FR-5.5. Breaker state lives in Redis so every API instance shares it.

    Keyed per *tenant* as well as per provider, which diverges from the spec's
    "per-provider" wording on purpose: FR-5.6 counts 401/403 toward the breaker, so a
    single merchant with a stale password would otherwise trip the circuit for every
    other merchant on the platform. Recorded in ADR-0002.
    """
    return namespaced(f"breaker:{provider.value}:{user_public_id}")
