"""Redis keys owned by the check domain."""

from src.cache.keys import namespaced


def check_cache_key(user_public_id: str, phone_normalized: str) -> str:
    """
    FR-6.8. Tenant first, and never anything but tenant first.

    A cache key that started with the phone number would be one refactor away from a
    cross-tenant read — the exact failure AC-6.9 exists to catch — so the tenant is not
    a component of the key so much as the namespace the key lives in.
    """
    return namespaced(f"check:{user_public_id}:{phone_normalized}")
