"""
Permission encoding and route guards (FR-1.11).

Ported from ``erp-backend/src/auth/permissions.py``. Granted codes travel in the access
token's ``pb`` claim as a base64url bitmask keyed by ``PermissionCode`` declaration
order, so a guard costs no database round-trip.

The trade-off is staleness: a permission granted or revoked mid-session only takes
effect on the next token, i.e. within ``ACCESS_TOKEN_EXPIRE_MINUTES``. Revoking access
*now* means revoking the session (``_invalidate_user_sessions``), which kills the token
with it.
"""

import base64
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import Annotated, Any

from fastapi import Depends
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.associations import RolePermissionLink, UserRoleLink
from src.auth.enums import PermissionCode
from src.auth.models import Permission
from src.auth.utils import decode_access_token, get_access_token, get_current_user
from src.common.exceptions import HTTP403
from src.users.models import User

logger = logging.getLogger(__name__)

_BITS_PER_BYTE = 8

# Bit index == PermissionCode declaration order (see the append-only contract on the enum).
_PERMISSION_INDEX: dict[str, int] = {code.value: i for i, code in enumerate(PermissionCode)}
_PERMISSION_BY_INDEX: list[str] = [code.value for code in PermissionCode]


def encode_permission_bitmask(codes: Iterable[str]) -> str:
    """Pack permission codes into a base64url bitmask for the JWT ``pb`` claim."""
    mask = 0
    for code in codes:
        index = _PERMISSION_INDEX.get(code)
        if index is not None:
            mask |= 1 << index

    width = (len(_PERMISSION_BY_INDEX) + _BITS_PER_BYTE - 1) // _BITS_PER_BYTE
    return base64.urlsafe_b64encode(mask.to_bytes(width, "big")).rstrip(b"=").decode("ascii")


def decode_permission_bitmask(encoded: str) -> set[str]:
    """Unpack a ``pb`` claim back into permission codes. Unknown bits are dropped."""
    if not encoded:
        return set()
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        mask = int.from_bytes(base64.urlsafe_b64decode(padded), "big")
    except (ValueError, TypeError):
        # A malformed claim grants nothing rather than 500-ing the request.
        logger.info("Discarding malformed permission bitmask")
        return set()

    codes = set()
    index = 0
    while mask:
        if mask & 1 and index < len(_PERMISSION_BY_INDEX):
            codes.add(_PERMISSION_BY_INDEX[index])
        mask >>= 1
        index += 1
    return codes


async def get_user_permissions(session: AsyncSession, user_id: int) -> list[str]:
    """Read the merchant's granted codes from the database. Runs at login/refresh only."""
    statement = (
        select(Permission.code)
        .join(RolePermissionLink, col(RolePermissionLink.permission_id) == Permission.id)
        .join(UserRoleLink, col(UserRoleLink.role_id) == RolePermissionLink.role_id)
        .where(UserRoleLink.user_id == user_id, Permission.is_active)
        .distinct()
    )
    return list((await session.exec(statement)).all())


async def get_current_permissions(
    token: Annotated[str, Depends(get_access_token)],
) -> set[str]:
    """Return the codes carried by the current access token's ``pb`` claim."""
    payload = decode_access_token(token)
    # A missing claim decodes to the empty set, so a token minted before a permission
    # existed is denied rather than crashing the request.
    return decode_permission_bitmask(payload.get("pb", ""))


def require_permission(code: PermissionCode) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    Build a dependency that requires one permission code.

    Usage: ``dependencies=[Depends(require_permission(PermissionCode.RISK_ORDER_REVIEW))]``
    """

    async def checker(
        user: Annotated[User, Depends(get_current_user)],  # noqa: ARG001 — validates the session
        permissions: Annotated[set[str], Depends(get_current_permissions)],
    ) -> None:
        if code not in permissions:
            raise HTTP403(detail=f"Missing permission: {code}")

    return checker


def require_any_permission(*codes: PermissionCode) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a dependency requiring at least one of the given codes."""

    async def checker(
        user: Annotated[User, Depends(get_current_user)],  # noqa: ARG001 — validates the session
        permissions: Annotated[set[str], Depends(get_current_permissions)],
    ) -> None:
        if not any(code in permissions for code in codes):
            raise HTTP403(detail=f"Missing any of: {', '.join(codes)}")

    return checker


def require_all_permissions(*codes: PermissionCode) -> Callable[..., Coroutine[Any, Any, None]]:
    """Build a dependency requiring every one of the given codes."""

    async def checker(
        user: Annotated[User, Depends(get_current_user)],  # noqa: ARG001 — validates the session
        permissions: Annotated[set[str], Depends(get_current_permissions)],
    ) -> None:
        missing = [code for code in codes if code not in permissions]
        if missing:
            raise HTTP403(detail=f"Missing permissions: {', '.join(missing)}")

    return checker
