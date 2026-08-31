"""
Per-provider circuit breaker, shared across API instances through Redis (FR-5).

A courier portal that is down must fail *fast*. Without a breaker every request pays
the full provider timeout before giving up, and a single sick provider drags the whole
check latency budget (NFR-1) down with it.

Two deliberate choices:

**State transitions run as Lua.** ``allow()`` reads the state and may move it to
half-open while claiming the single probe slot; doing that as three round-trips would
let two instances both believe they hold the probe. One script, one atomic step.

**"Now" is passed in, never read inside Redis.** Every deadline compares against a
timestamp the caller supplies, so the whole breaker can be driven by a frozen clock in
tests (NFR: no test may depend on wall-clock time) instead of by key TTLs.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from src.cache.redis_client import get_async_redis
from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import CircuitOpenError
from src.logistics.keys import breaker_key

logger = logging.getLogger(__name__)

# The state hash is garbage-collected rather than kept forever: a provider nobody has
# called in an hour has no interesting failure history.
_STATE_TTL_SECONDS = 3600

_ALLOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local open_seconds = tonumber(ARGV[2])
local state = redis.call('hget', key, 'state')

if state == 'open' then
    local opened_at = tonumber(redis.call('hget', key, 'opened_at')) or 0
    if now - opened_at < open_seconds then
        return 'open'
    end
    redis.call('hset', key, 'state', 'half_open', 'probe', '1')
    return 'probe'
end

if state == 'half_open' then
    if redis.call('hsetnx', key, 'probe', '1') == 1 then
        return 'probe'
    end
    return 'open'
end

return 'closed'
"""

_FAILURE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local function trip()
    redis.call('hset', key, 'state', 'open', 'opened_at', now, 'failures', threshold)
    redis.call('hdel', key, 'probe')
    redis.call('expire', key, ttl)
    return 'open'
end

if redis.call('hget', key, 'state') == 'half_open' then
    return trip()
end

local first = tonumber(redis.call('hget', key, 'first_failure_at'))
local failures = tonumber(redis.call('hget', key, 'failures')) or 0
if first == nil or now - first > window then
    first = now
    failures = 0
end
failures = failures + 1
redis.call('hset', key, 'failures', failures, 'first_failure_at', first)

if failures >= threshold then
    return trip()
end

redis.call('hset', key, 'state', 'closed')
redis.call('expire', key, ttl)
return 'closed'
"""


class BreakerDecision(StrEnum):
    CLOSED = "closed"
    PROBE = "probe"
    OPEN = "open"


class CircuitBreaker:
    def __init__(
        self,
        provider: ProviderEnum,
        user_public_id: str,
        redis: AsyncRedis | None = None,
    ) -> None:
        """Breaker for one merchant's use of one provider (see :func:`breaker_key`)."""
        self.provider = provider
        self.user_public_id = user_public_id
        self.redis = redis if redis is not None else get_async_redis()
        self.key = breaker_key(provider, user_public_id)

    async def allow(self) -> BreakerDecision:
        """
        Decide whether this call may go out, claiming the half-open probe if there is one.

        Raises:
            CircuitOpenError: when the breaker is open, so no HTTP call is made (AC-5.1).

        """
        now = datetime.now(UTC).timestamp()
        try:
            raw = await self.redis.eval(
                _ALLOW_SCRIPT, 1, self.key, now, settings.BREAKER_OPEN_SECONDS
            )
        except RedisError:
            # A breaker that cannot be read must not become an outage of its own: let
            # the call through and rely on the provider timeout.
            logger.warning(
                "Breaker state unreadable for %s, allowing the call", self.provider.value
            )
            return BreakerDecision.CLOSED

        decision = BreakerDecision(_decode(raw))
        if decision is BreakerDecision.OPEN:
            msg = f"{self.provider.value}: circuit open"
            raise CircuitOpenError(msg)
        return decision

    async def record_success(self) -> None:
        """AC-5.3 / AC-5.5. Success closes the breaker and zeroes the failure count."""
        try:
            await self.redis.delete(self.key)
        except RedisError:
            logger.warning("Could not reset breaker state for %s", self.provider.value)

    async def record_failure(self) -> None:
        """FR-5.2 / FR-5.6. Counts auth failures too — a 401 is still a broken provider."""
        now = datetime.now(UTC).timestamp()
        try:
            raw = await self.redis.eval(
                _FAILURE_SCRIPT,
                1,
                self.key,
                now,
                settings.BREAKER_WINDOW_SECONDS,
                settings.BREAKER_FAILURE_THRESHOLD,
                _STATE_TTL_SECONDS,
            )
        except RedisError:
            logger.warning("Could not record breaker failure for %s", self.provider.value)
            return

        if _decode(raw) == BreakerDecision.OPEN:
            logger.warning(
                "Circuit opened for %s (tenant %s) for %ss",
                self.provider.value,
                self.user_public_id,
                settings.BREAKER_OPEN_SECONDS,
            )

    async def state(self) -> str:
        """Read the stored state, for tests and the observability endpoint."""
        try:
            stored = await self.redis.hget(self.key, "state")
        except RedisError:
            return BreakerDecision.CLOSED.value
        return _decode(stored) if stored else BreakerDecision.CLOSED.value


def _decode(value: object) -> str:
    """Redis returns ``bytes`` unless the client decodes; accept either."""
    return value.decode() if isinstance(value, bytes) else str(value)
