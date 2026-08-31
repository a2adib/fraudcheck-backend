"""
Redis key namespacing.

Every key this service writes goes through :func:`namespaced`, so a deployment can
share a Redis instance with something else — and so the test suite can give each
pytest-xdist worker its own slice instead of racing on ``flushdb``.

The prefix is read from ``settings`` at call time, never captured at import, because
tests set it per session.

Key *shapes* live with the domain that owns them (``src/logistics/keys.py``,
``src/checks/keys.py``); ``cache`` depends on nothing but ``config`` (AGENTS.md).
"""

from src.config import settings


def namespaced(key: str) -> str:
    return f"{settings.REDIS_KEY_PREFIX}{key}"
