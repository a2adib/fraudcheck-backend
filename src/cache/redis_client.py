import logging

import redis
import redis.asyncio as aioredis

from src.config import settings

logger = logging.getLogger(__name__)

logger.info("Connecting to Redis at %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)

redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    decode_responses=True,
)

async_redis_client = aioredis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    decode_responses=True,
)


async def get_redis_health() -> dict[str, str]:
    """Report Redis reachability for ``/health`` (FR-14.3)."""
    try:
        await async_redis_client.ping()
    except Exception:  # noqa: BLE001
        return {"redis": "down"}
    return {"redis": "up"}


def get_async_redis() -> aioredis.Redis:
    """
    Resolve the shared async client at call time.

    Everything that touches Redis goes through this rather than importing the client
    object, so a single monkeypatch here swaps the client for the whole process — which
    is what lets each test bind a client to its own event loop.
    """
    return async_redis_client
