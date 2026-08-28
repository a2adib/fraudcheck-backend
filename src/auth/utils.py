"""
Auth primitives.

Ported from ``erp-backend/src/auth/utils.py`` with one deliberate change: the
``CryptContext`` leads with Argon2id per NFR-2, where upstream uses bcrypt at 12
rounds. Both schemes are registered with ``deprecated="auto"`` so any bcrypt hash
carried over from an upstream export is verified once and transparently rehashed to
Argon2id on the next successful login (FR-1 port note).

Sessions and refresh rotation land here too; OTP reset (FR-1.10) and RBAC (FR-1.11)
are still to come.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.enums import Channel
from src.auth.models import UserSession
from src.common.exceptions import AuthAPIError
from src.common.phone import InvalidPhoneNumberError, normalize_bd_mobile
from src.config import settings
from src.database import get_session
from src.users.enums import UserStatus
from src.users.models import User

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 10  # FR-1, AC-1.3. Upstream erp-backend uses 8.

ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"
#: FR-1.9 / AC-1.11 — the refresh cookie is never sent to any other endpoint.
REFRESH_COOKIE_PATH = "/auth/token/refresh"

pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")

OPTIONAL_BEARER_SCHEME = HTTPBearer(auto_error=False)

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)

_dummy_password_hash: str | None = None


def dummy_password_hash() -> str:
    """
    Return a hash of a value nobody knows, to verify against when no user matched.

    AC-1.5 requires a wrong password and a nonexistent account to take the same time;
    skipping the KDF when the lookup misses is exactly the timing oracle that leaks
    which emails are registered. Computed lazily and cached so it picks up whatever
    Argon2 parameters are configured at first use — the test suite downgrades them.
    """
    global _dummy_password_hash  # noqa: PLW0603
    if _dummy_password_hash is None:
        _dummy_password_hash = get_password_hash(secrets.token_urlsafe(32))
    return _dummy_password_hash


def get_password_hash(password: str) -> str:
    """Hash a password with Argon2id."""
    # passlib ships no type information (see [[tool.mypy.overrides]] in pyproject),
    # so each result is pinned to a concrete type on the way out.
    hashed: str = pwd_context.hash(password)
    return hashed


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash. Accepts a legacy bcrypt hash."""
    matches: bool = pwd_context.verify(plain_password, hashed_password)
    return matches


def needs_rehash(hashed_password: str) -> bool:
    """Report whether a stored hash uses a deprecated scheme and needs upgrading."""
    stale: bool = pwd_context.needs_update(hashed_password)
    return stale


# ── Identifier detection ─────────────────────────────────────────────────────


def detect_channel(identifier: str) -> tuple[Channel, str]:
    """
    Detect the channel from a login identifier and return ``(channel, normalized)``.

    Email is trimmed and lowercased; a mobile is normalized to ``01XXXXXXXXX`` so
    ``01712345678``, ``+8801712345678`` and ``880-171-2345678`` all resolve to the
    same account (FR-1.7, AC-1.9).
    """
    ident = identifier.strip()
    if "@" in ident:
        return Channel.EMAIL, ident.lower()
    try:
        return Channel.MOBILE, normalize_bd_mobile(ident)
    except InvalidPhoneNumberError as exc:
        raise AuthAPIError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_mobile_format",
            "Invalid Bangladesh mobile number format.",
        ) from exc


# ── JWT access tokens ────────────────────────────────────────────────────────


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Mint a short-lived JWT access token (FR-1.3, 30 minutes by default)."""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {**data, "exp": datetime.now(UTC) + expires_delta}
    token: str = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify an access token, returning its claims. Raises 401 otherwise."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        logger.info("Access token has expired")
        raise CREDENTIALS_EXCEPTION from None
    except jwt.PyJWTError:
        logger.info("Access token failed validation")
        raise CREDENTIALS_EXCEPTION from None
    return claims


# ── Auth cookies (FR-1.9) ────────────────────────────────────────────────────


def access_max_age() -> int:
    return settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def refresh_max_age() -> int:
    return settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Set HttpOnly access + refresh cookies, the refresh one scoped to its own path."""
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=access_max_age(),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        domain=settings.COOKIE_DOMAIN,
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=refresh_max_age(),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN,
    )


def clear_auth_cookies(response: Response) -> None:
    """Expire both auth cookies on the exact paths they were set with."""
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        domain=settings.COOKIE_DOMAIN,
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        domain=settings.COOKIE_DOMAIN,
    )


# ── Request helpers ──────────────────────────────────────────────────────────


def get_client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the proxy headers the deployment sets."""
    client_ip = request.headers.get("X-Client-IP")
    if client_ip:
        return client_ip

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip

    return request.client.host if request.client else "unknown"


def extract_access_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
) -> str | None:
    """Pull the access token from the Bearer header, falling back to the cookie."""
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return request.cookies.get(ACCESS_COOKIE_NAME)


def extract_refresh_token(request: Request, body_token: str | None) -> str | None:
    """Prefer the token the client sent in the body; fall back to the cookie."""
    return body_token or request.cookies.get(REFRESH_COOKIE_NAME)


# ── Current-user dependencies ────────────────────────────────────────────────


async def get_access_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(OPTIONAL_BEARER_SCHEME)],
) -> str:
    """Resolve the access token from header or cookie, or raise 401 (AC-1.7)."""
    token = extract_access_token(request, credentials)
    if not token:
        raise CREDENTIALS_EXCEPTION
    return token


async def resolve_user_from_token(session: AsyncSession, token: str) -> User:
    """Validate an access token and return the active merchant it belongs to."""
    payload = decode_access_token(token)
    public_id = payload.get("sub")
    session_public_id = payload.get("sid")

    if not public_id:
        raise CREDENTIALS_EXCEPTION

    user = (
        await session.exec(select(User).where(User.is_active, User.public_id == public_id))
    ).first()
    if not user or user.status != UserStatus.ACTIVE:
        raise CREDENTIALS_EXCEPTION

    # The token carries the session that minted it. A revoked or expired session means
    # the merchant logged out, reset their password, or had the device revoked — the
    # access token dies with it rather than living out its remaining minutes.
    if session_public_id:
        user_session = (
            await session.exec(
                select(UserSession).where(
                    UserSession.public_id == session_public_id,
                    UserSession.is_active,
                )
            )
        ).first()
        if not user_session or user_session.is_expired():
            raise CREDENTIALS_EXCEPTION

    return user


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Depends(get_access_token)],
) -> User:
    """Validate the access token (header or cookie) and return the current merchant."""
    return await resolve_user_from_token(session, token)


async def get_current_user_or_none(
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(OPTIONAL_BEARER_SCHEME)],
) -> User | None:
    """Resolve the current merchant like :func:`get_current_user`, or ``None``."""
    token = extract_access_token(request, credentials)
    if not token:
        return None
    try:
        return await resolve_user_from_token(session, token)
    except HTTPException:
        return None
