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
