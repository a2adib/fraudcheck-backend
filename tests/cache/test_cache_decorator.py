"""
The ported Redis cache decorator.

Ported from ``govaly-backend`` in M0 and untested until now, which is the worst state
for a caching layer to be in: a broken cache does not raise, it just quietly returns the
wrong customer's numbers.
"""

import redis.asyncio as aioredis

from src.cache.cache_decorator import async_cache_result, async_invalidate_tag
from src.config import settings


def prefix() -> str:
    """Keep every key inside this test's namespace, so the fixture cleans it up."""
    return f"{settings.REDIS_KEY_PREFIX}cache"


class TestCaching:
    async def test_a_second_call_with_the_same_arguments_does_not_run_the_function(
        self, redis_client: aioredis.Redis
    ):
        calls = {"count": 0}

        @async_cache_result(ttl=60, key_prefix=prefix(), client=redis_client)
        async def expensive(value: int) -> dict[str, int]:
            calls["count"] += 1
            return {"value": value}

        assert await expensive(1) == {"value": 1}
        assert await expensive(1) == {"value": 1}
        assert calls["count"] == 1

    async def test_different_arguments_are_different_entries(self, redis_client: aioredis.Redis):
        """The tenant-safety property the docstring warns about, asserted."""
        calls = {"count": 0}

        @async_cache_result(ttl=60, key_prefix=prefix(), client=redis_client)
        async def per_tenant(tenant: str) -> str:
            calls["count"] += 1
            return tenant

        assert await per_tenant("tenant-a") == "tenant-a"
        assert await per_tenant("tenant-b") == "tenant-b"
        assert calls["count"] == 2

    async def test_a_result_can_be_refused_the_cache(self, redis_client: aioredis.Redis):
        calls = {"count": 0}

        @async_cache_result(
            ttl=60,
            key_prefix=prefix(),
            client=redis_client,
            skip_cache_if=lambda result: not result,
        )
        async def sometimes_empty() -> list[int]:
            calls["count"] += 1
            return []

        await sometimes_empty()
        await sometimes_empty()

        assert calls["count"] == 2

    async def test_an_entry_can_be_invalidated_by_its_arguments(self, redis_client: aioredis.Redis):
        calls = {"count": 0}

        @async_cache_result(ttl=60, key_prefix=prefix(), client=redis_client)
        async def lookup(value: int) -> int:
            calls["count"] += 1
            return value

        await lookup(7)
        await lookup.invalidate(7)
        await lookup(7)

        assert calls["count"] == 2


class TestTags:
    async def test_tagged_entries_are_invalidated_together(self, redis_client: aioredis.Redis):
        tag = f"{settings.REDIS_KEY_PREFIX}courier"
        calls = {"count": 0}

        @async_cache_result(ttl=60, key_prefix=prefix(), client=redis_client, tags=[tag])
        async def lookup(value: int) -> int:
            calls["count"] += 1
            return value

        await lookup(1)
        await lookup(2)

        try:
            assert await async_invalidate_tag(tag, client=redis_client) == 2
            await lookup(1)
            await lookup(2)
            assert calls["count"] == 4
        finally:
            await redis_client.delete(f"cache_tag:{tag}")

    async def test_invalidating_an_unknown_tag_removes_nothing(self, redis_client: aioredis.Redis):
        assert await async_invalidate_tag("no-such-tag", client=redis_client) == 0
