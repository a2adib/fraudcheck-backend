"""
Provider session tokens, cached in Redis behind a distributed lock (FR-4).

Ported from ``govaly-backend/src/logistics/token_manager.py``, which already solves
the hard part: 50 concurrent requests must produce exactly *one* login, not 50. Two
things change in the port.

**Per tenant, not per platform.** Upstream logs in with one set of platform-wide
courier credentials read from the environment, and caches the token under a global
key. Here every merchant supplies their own credential (FR-2), so a manager is
constructed per ``(provider, merchant)`` and the cache key is namespaced by
``user_public_id`` (FR-4.6, AC-4.5).

**No local-disk fallback.** Upstream also writes each token to
``/tmp/courier_token_cache/*.json`` so a Redis outage cannot stop deliveries. With
per-tenant tokens that would scatter merchant session tokens across every worker's
filesystem, outside the vault and outside its lifecycle. Redis is the only store here;
if Redis is gone, the provider leg reports ``unavailable`` and the check still returns
(FR-6.4).

The waiter loop also diverges: upstream polls only for a *token*, so a worker that
dies holding the lock stalls every waiter for the full poll window. Here the waiter
re-attempts the lock on each tick, so the moment the abandoned lock expires
(``TOKEN_LOCK_TIMEOUT_MS``, AC-4.4) the next waiter takes over and logs in.
"""

import asyncio
import contextlib
import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import jwt
from pydantic import BaseModel
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from src.cache.redis_client import get_async_redis
from src.config import settings
from src.logistics.enums import ProviderEnum
from src.logistics.exceptions import ProviderAuthError, ProviderError, TokenWaitTimeout
from src.logistics.keys import token_data_key, token_lock_key
from src.logistics.schemas import DecryptedCredential

logger = logging.getLogger(__name__)

# Compare-and-delete: only the holder may release the lock, so a slow refresh that
# outlived its own lock cannot delete the lock a *different* worker now holds.
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Collapses same-worker stampedes before they ever reach Redis — 50 concurrent
# requests in one process take this lock, and only the first one talks to Redis at
# all. Keyed by cache key, so it is per (provider, tenant) like the Redis lock.
_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}

#: RedX mints a JWT and states the expiry only inside it. When the token is not a
#: decodable JWT there is nothing to read, so fall back to the twelve hours upstream
#: has been assuming in production.
REDX_DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600

#: header.payload.signature — anything else is not a JWT and has no ``exp`` to read.
_JWT_SEGMENTS = 3

#: Statuses that mean "these credentials are wrong", as opposed to "this portal is
#: having a bad day". Pathao answers a bad password with 400 as often as 401.
_CREDENTIAL_REJECTED_STATUSES = frozenset({400, 401, 403})


def _local_lock(key: str) -> asyncio.Lock:
    lock = _LOCAL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCAL_LOCKS[key] = lock
    return lock


class TokenState(StrEnum):
    VALID = "valid"
    GRACE = "grace"
    EXPIRED = "expired"


class TokenRecord(BaseModel):
    """What gets cached. ``expires_at`` is a POSIX timestamp, as the provider reports."""

    access_token: str
    expires_at: float
    token_type: str | None = None
    refreshed_at: str | None = None


class CourierTokenManager(ABC):
    provider: ClassVar[ProviderEnum]

    def __init__(self, credential: DecryptedCredential, redis: AsyncRedis | None = None) -> None:
        """Manage one merchant's session token for one provider."""
        self.credential = credential
        self.redis = redis if redis is not None else get_async_redis()
        self._instance_id = str(uuid4())

    # ── Keys ────────────────────────────────────────────────────────────────

    @property
    def _data_key(self) -> str:
        return token_data_key(self.provider, self.credential.user_public_id)

    @property
    def _lock_key(self) -> str:
        return token_lock_key(self.provider, self.credential.user_public_id)

    # ── Provider specifics ──────────────────────────────────────────────────

    @abstractmethod
    def _login_request(self) -> tuple[str, dict[str, Any]]:
        """Return the login URL and JSON body for this courier."""

    @abstractmethod
    def _parse_login_response(self, data: dict[str, Any]) -> TokenRecord:
        """Turn a login response into the cached record, or raise ``ProviderAuthError``."""

    # ── Public API ──────────────────────────────────────────────────────────

    async def get_token(self) -> str:
        async with _local_lock(self._data_key):
            record = await self._read()
            if record:
                state = self._validate(record)
                if state is TokenState.VALID:
                    return record.access_token
                if state is TokenState.GRACE:
                    return await self._refresh_or_stale(record)
            return await self._acquire_and_refresh(None)

    async def invalidate(self) -> None:
        """Drop the cached token — called when the provider rejects it mid-flight."""
        with contextlib.suppress(RedisError):
            await self.redis.delete(self._data_key)

    # ── Cache ───────────────────────────────────────────────────────────────

    async def _read(self) -> TokenRecord | None:
        try:
            raw = await self.redis.get(self._data_key)
        except RedisError:
            logger.warning("%s: Redis unreachable, cannot read cached token", self.provider.value)
            return None
        if not raw:
            return None
        try:
            return TokenRecord.model_validate(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("%s: discarding unreadable cached token", self.provider.value)
            with contextlib.suppress(RedisError):
                await self.redis.delete(self._data_key)
            return None

    async def _write(self, record: TokenRecord) -> None:
        # Kept past its own expiry by the grace period, so FR-4.7 can still serve it
        # stale when a refresh fails.
        ttl = int(record.expires_at - datetime.now(UTC).timestamp())
        ttl += settings.TOKEN_GRACE_PERIOD_SECONDS
        try:
            await self.redis.set(self._data_key, record.model_dump_json(), ex=max(ttl, 1))
        except RedisError:
            logger.warning(
                "%s: Redis unreachable, token not cached — the next request will log in again",
                self.provider.value,
            )

    def _validate(self, record: TokenRecord) -> TokenState:
        """FR-4.2 / FR-4.7 — valid, inside the grace window, or gone."""
        now = datetime.now(UTC).timestamp()
        if now < record.expires_at - settings.TOKEN_SAFETY_MARGIN_SECONDS:
            return TokenState.VALID
        if now < record.expires_at + settings.TOKEN_GRACE_PERIOD_SECONDS:
            return TokenState.GRACE
        return TokenState.EXPIRED

    # ── Refresh ─────────────────────────────────────────────────────────────

    async def _refresh_or_stale(self, record: TokenRecord) -> str:
        """AC-4.7. A failed refresh inside the grace window serves the old token."""
        try:
            return await self._acquire_and_refresh(record)
        except ProviderError:
            logger.warning(
                "%s: refresh failed, serving a token inside its grace period",
                self.provider.value,
            )
            return record.access_token

    async def _acquire_and_refresh(self, existing: TokenRecord | None) -> str:
        if await self._try_acquire_lock():
            try:
                return await self._do_refresh()
            finally:
                await self._release_lock()

        token = await self._wait_for_token()
        if token:
            return token
        if existing:
            logger.warning(
                "%s: could not acquire the login lock, serving what we have", self.provider.value
            )
            return existing.access_token
        msg = f"{self.provider.value}: timed out waiting for a login"
        raise TokenWaitTimeout(msg)

    async def _try_acquire_lock(self) -> bool:
        """FR-4.3 / FR-4.5. ``SET NX PX`` — a crashed holder cannot deadlock the system."""
        try:
            acquired = await self.redis.set(
                self._lock_key, self._instance_id, nx=True, px=settings.TOKEN_LOCK_TIMEOUT_MS
            )
        except RedisError:
            # No Redis means no coordination is possible. Logging in unilaterally is
            # worse than a stampede only in call volume, whereas refusing to log in
            # fails the check outright.
            logger.warning(
                "%s: Redis unreachable, logging in without the lock", self.provider.value
            )
            return True
        return bool(acquired)

    async def _release_lock(self) -> None:
        with contextlib.suppress(RedisError):
            await self.redis.eval(_RELEASE_LOCK_SCRIPT, 1, self._lock_key, self._instance_id)

    async def _wait_for_token(self) -> str | None:
        """
        FR-4.4. Wait for the lock holder's token, and take over if it never comes.

        Each tick looks for a usable token first, then re-attempts the lock. That
        second half is what makes AC-4.4 terminate quickly: once an abandoned lock
        expires, the waiter becomes the refresher instead of running out the clock.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.TOKEN_LOCK_MAX_POLL_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(settings.TOKEN_LOCK_POLL_INTERVAL)

            record = await self._read()
            if record and self._validate(record) in (TokenState.VALID, TokenState.GRACE):
                return record.access_token

            if await self._try_acquire_lock():
                try:
                    return await self._do_refresh()
                finally:
                    await self._release_lock()
        return None

    async def _do_refresh(self) -> str:
        # Re-read under the lock: whoever held it before us may have just written a
        # perfectly good token, and a second login would be pure waste.
        existing = await self._read()
        if existing and self._validate(existing) is TokenState.VALID:
            return existing.access_token

        url, payload = self._login_request()
        async with httpx.AsyncClient(timeout=settings.TOKEN_LOGIN_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    url, headers={"Content-Type": "application/json"}, json=payload
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                # Status code only. The response body of a failed courier login has
                # been observed to echo the submitted username (FR-2.5, AC-2.6).
                logger.warning(
                    "%s login rejected with status %s",
                    self.provider.value,
                    exc.response.status_code,
                )
                # A rejected *credential* and an unwell *portal* are different facts,
                # and upstream conflates them: govaly raises its auth error for both,
                # so a courier outage would mark every merchant's password invalid.
                if exc.response.status_code in _CREDENTIAL_REJECTED_STATUSES:
                    msg = f"{self.provider.value} rejected the credential"
                    raise ProviderAuthError(msg) from exc
                msg = f"{self.provider.value} login is unavailable"
                raise ProviderError(msg) from exc
            except Exception as exc:
                logger.warning("%s login failed: %s", self.provider.value, type(exc).__name__)
                msg = f"{self.provider.value} login is unavailable"
                raise ProviderError(msg) from exc

        record = self._parse_login_response(data)
        await self._write(record)
        logger.info(
            "%s token refreshed for tenant %s", self.provider.value, self.credential.user_public_id
        )
        return record.access_token


class PathaoTokenManager(CourierTokenManager):
    """
    Pathao merchant-panel login.

    Upstream also manages the Aladdin API token (order creation); this service only
    ever reads customer history, which lives behind the merchant panel, so the API
    token has no reason to exist here.
    """

    provider: ClassVar[ProviderEnum] = ProviderEnum.PATHAO

    def _login_request(self) -> tuple[str, dict[str, Any]]:
        return (
            f"{settings.PATHAO_MERCHANT_URL}/api/v1/login",
            {
                "username": self.credential.username.get_secret_value(),
                "password": self.credential.password.get_secret_value(),
            },
        )

    def _parse_login_response(self, data: dict[str, Any]) -> TokenRecord:
        now = datetime.now(UTC)
        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not access_token or not isinstance(expires_in, (int, float)):
            raise ProviderAuthError("Pathao login response carried no usable token")
        return TokenRecord(
            access_token=str(access_token),
            expires_at=now.timestamp() + float(expires_in),
            token_type=data.get("token_type"),
            refreshed_at=now.isoformat(),
        )


def _jwt_expiry(token: str) -> float | None:
    """
    Read ``exp`` off a JWT without verifying it — only the timestamp is wanted.

    Ported from ``govaly-backend/src/logistics/token_manager.py``. Verification would
    need RedX's signing key, which we do not have and do not need: this token is a
    bearer we hand straight back to the issuer, and reading the expiry wrong costs one
    superfluous login, not a security property.
    """
    if token.count(".") != _JWT_SEGMENTS - 1:  # not header.payload.signature, so not a JWT
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except jwt.PyJWTError:
        logger.warning("RedX token is not a decodable JWT; falling back to the default TTL")
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


class RedxTokenManager(CourierTokenManager):
    """
    RedX merchant login.

    Two things separate this from Pathao. RedX logs in on a *different host* to the one
    the lookup is made against (``REDX_API_BASE_URL`` vs ``REDX_PANEL_BASE_URL``), and it
    reports a rejected credential in the *body* — ``isError: true`` at HTTP 200 — so
    ``raise_for_status()`` never fires and the auth error is raised from
    ``_parse_login_response`` instead. The base class calls that method outside its own
    ``try``, so the error reaches the caller as-is rather than being rewrapped as an
    outage.
    """

    provider: ClassVar[ProviderEnum] = ProviderEnum.REDX

    def _login_request(self) -> tuple[str, dict[str, Any]]:
        """
        RedX identifies a merchant by phone, not username.

        The vault stores one ``username`` column per credential (FR-2); for RedX that
        column holds the login phone. This is the only place that mapping exists.
        """
        return (
            f"{settings.REDX_API_BASE_URL}/v4/auth/login",
            {
                "phone": self.credential.username.get_secret_value(),
                "password": self.credential.password.get_secret_value(),
            },
        )

    def _parse_login_response(self, data: dict[str, Any]) -> TokenRecord:
        if data.get("isError"):
            # Upstream interpolates ``data["message"]`` into the error. That message has
            # been observed to echo the submitted phone, which FR-2.5 / AC-2.6 forbid
            # anywhere near a log line — so the reason is dropped, not quoted.
            msg = "RedX rejected the credential"
            raise ProviderAuthError(msg)

        payload = data.get("data")
        access_token = payload.get("accessToken") if isinstance(payload, dict) else None
        if not access_token:
            msg = "RedX login response carried no accessToken"
            raise ProviderAuthError(msg)

        now = datetime.now(UTC)
        access_token = str(access_token)
        expires_at = _jwt_expiry(access_token) or now.timestamp() + REDX_DEFAULT_TOKEN_TTL_SECONDS
        return TokenRecord(
            access_token=access_token,
            expires_at=expires_at,
            token_type="Bearer",  # noqa: S106 — the auth scheme, not a secret
            refreshed_at=now.isoformat(),
        )


_MANAGERS: dict[ProviderEnum, type[CourierTokenManager]] = {
    ProviderEnum.PATHAO: PathaoTokenManager,
    ProviderEnum.REDX: RedxTokenManager,
}


def has_token_manager(provider: ProviderEnum) -> bool:
    """Whether this provider can be logged into yet — Steadfast's contract is still open."""
    return provider in _MANAGERS


def token_manager_for(
    credential: DecryptedCredential, redis: AsyncRedis | None = None
) -> CourierTokenManager:
    """Build the manager for a credential's provider. Raises for providers with no login."""
    manager_class = _MANAGERS.get(credential.provider)
    if manager_class is None:
        msg = f"No token manager for provider {credential.provider.value}"
        raise ProviderAuthError(msg)
    return manager_class(credential, redis)
