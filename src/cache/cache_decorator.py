"""
Redis result caching, ported from ``govaly-backend/src/cache/cache_decorator.py``.

Only the async variant is carried over — every request path in this service is async.

Note for callers: a cache key derived purely from function arguments is only
tenant-safe if a tenant identifier is *among* those arguments. Anything caching
merchant-scoped data must take ``user_public_id`` as an argument or pass it via
``key_prefix`` (FR-6.9, AC-6.9).
"""

import functools
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis

from src.cache.redis_client import async_redis_client
from src.config import settings

logger = logging.getLogger(__name__)


def _build_key(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    prefix: str,
) -> str:
    key = f"{fn.__module__}.{fn.__qualname__}:{args}:{sorted(kwargs.items())}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"{prefix}:{digest}" if prefix else digest


def async_cache_result(
    ttl: int = settings.CACHE_TIME_OUT,
    key_prefix: str = "",
    client: aioredis.Redis = async_redis_client,
    skip_cache_if: Callable[[Any], bool] | None = None,
    tags: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Cache an async function's return value in Redis.

    Args:
        ttl:           Seconds before the key expires.
        key_prefix:    String prepended to the cache key — use it to namespace by tenant.
        client:        Async Redis client instance.
        skip_cache_if: Called with the return value; if it returns True the result is
                       NOT cached (e.g. skip caching a ``None`` or a failed lookup).
        tags:          Tag strings the key is registered under, so
                       :func:`async_invalidate_tag` can bust related entries at once.

    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            cache_key = _build_key(fn, args, kwargs, key_prefix)

            # redis-py types this bytes|str|None regardless of decode_responses;
            # json.loads accepts either, so let inference stand.
            cached = await client.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit: %s", cache_key)
                return json.loads(cached)

            logger.debug("Cache miss: %s", cache_key)
            result = await fn(*args, **kwargs)

            if skip_cache_if is None or not skip_cache_if(result):
                async with client.pipeline(transaction=False) as pipe:
                    # `setex` is deprecated in redis-py 6; `set(..., ex=)` is the same call.
                    pipe.set(cache_key, json.dumps(result), ex=ttl)
                    for tag in tags or []:
                        pipe.sadd(f"cache_tag:{tag}", cache_key)
                        pipe.expire(f"cache_tag:{tag}", ttl)
                    await pipe.execute()
                logger.debug("Cache set: %s (ttl=%ds, tags=%s)", cache_key, ttl, tags)
            else:
                logger.debug("Cache skip (skip_cache_if): %s", cache_key)
            return result

        async def invalidate(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            cache_key = _build_key(fn, args, kwargs, key_prefix)
            await client.delete(cache_key)
            logger.debug("Cache invalidated: %s", cache_key)

        wrapper.invalidate = invalidate  # type: ignore[attr-defined]
        return wrapper

    return decorator


async def async_invalidate_tag(tag: str, client: aioredis.Redis = async_redis_client) -> int:
    """Delete every cache key registered under ``tag``. Returns the number removed."""
    keys: set[bytes | str] = await client.smembers(f"cache_tag:{tag}")
    if not keys:
        return 0

    async with client.pipeline(transaction=False) as pipe:
        pipe.delete(*keys)
        pipe.delete(f"cache_tag:{tag}")
        await pipe.execute()
    return len(keys)
