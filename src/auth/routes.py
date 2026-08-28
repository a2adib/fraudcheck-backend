"""Auth routes (FR-1)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.enums import PermissionCode, TokenDelivery
from src.auth.permissions import require_permission
from src.auth.schemas import (
    AssignRoleRequest,
    ChangePasswordRequest,
    ForgotPasswordOut,
    ForgotPasswordRequest,
    LoginRequest,
    PasswordResetRequest,
    PermissionOut,
    RefreshRequest,
    RegisterRequest,
    ResendOtpRequest,
    RoleCreate,
    RoleDetailOut,
    RoleOut,
    RoleUpdate,
    TokenOut,
    UserOut,
    VerifyOtpOut,
    VerifyOtpRequest,
)
from src.auth.services import AuthenticationService, AuthorizationService
from src.auth.utils import (
    access_max_age,
    clear_auth_cookies,
    extract_access_token,
    extract_refresh_token,
    get_client_ip,
    get_current_user,
    set_auth_cookies,
)
from src.common.filters import PaginationParams, PaginationResponse
from src.common.mixins import require_id
from src.common.response import MESSAGE_201, StandardResponse, create_response
from src.database import get_session
from src.securities.services import create_activity_log
from src.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PaginationDep = Annotated[PaginationParams, Depends(PaginationParams)]
ClientIPDep = Annotated[str, Depends(get_client_ip)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]


def _token_out(
    response: Response,
    access_token: str,
    refresh_token: str,
    delivery: TokenDelivery,
    *,
    new_device: bool = False,
) -> TokenOut:
    """
    Deliver the token pair the way the session asked for (FR-1.9).

    In cookie mode the body carries no token string at all — not even the access
    token — so nothing reachable by JavaScript ever holds one (AC-1.11).
    """
    if delivery is TokenDelivery.COOKIE:
        set_auth_cookies(response, access_token, refresh_token)
        return TokenOut(
            expires_in=access_max_age(),
            token_delivery=TokenDelivery.COOKIE,
            new_device=new_device,
        )
    return TokenOut(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=access_max_age(),
        token_delivery=TokenDelivery.JSON,
        new_device=new_device,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: RegisterRequest,
) -> StandardResponse[UserOut]:
    """Register a merchant account (FR-1.1)."""
    auth_service = AuthenticationService(session)
    user = await auth_service.register(payload)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Merchant account registered",
        data={"user_public_id": user.public_id},
    )

    return create_response(UserOut.model_validate(user, from_attributes=True), MESSAGE_201)


@router.post("/login")
async def login_user(
    request: Request,
    response: Response,
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: LoginRequest,
) -> StandardResponse[TokenOut]:
    """Authenticate and issue an access + refresh token pair (FR-1.3)."""
    auth_service = AuthenticationService(session)
    result = await auth_service.login(
        payload, ip_address=client_ip, user_agent=request.headers.get("User-Agent")
    )

    await create_activity_log(
        session,
        user_id=require_id(result.user),
        ip_address=client_ip,
        description=(
            "Merchant logged in from a new device"
            if result.new_device
            else "Merchant logged in successfully"
        ),
        data={"session_public_id": result.session.public_id, "new_device": result.new_device},
    )

    token_out = _token_out(
        response,
        result.access_token,
        result.refresh_token,
        payload.token_delivery,
        new_device=result.new_device,
    )
    return create_response(token_out, message="Logged in successfully")


@router.post("/token/refresh")
async def refresh_token(
    request: Request,
    response: Response,
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: RefreshRequest,
) -> StandardResponse[TokenOut]:
    """Rotate the refresh token and mint a new access token (FR-1.4)."""
    auth_service = AuthenticationService(session)
    raw_token = extract_refresh_token(request, payload.refresh_token)
    result = await auth_service.refresh(raw_token, ip_address=client_ip)

    await create_activity_log(
        session,
        user_id=result.user_id,
        ip_address=client_ip,
        description="Access token refreshed",
        data={"session_public_id": result.session.public_id},
    )

    token_out = _token_out(
        response, result.access_token, result.refresh_token, result.session.delivery_mode
    )
    return create_response(token_out, message="Token refreshed successfully")


@router.post("/logout")
async def logout_user(
    request: Request,
    response: Response,
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: RefreshRequest,
) -> StandardResponse[None]:
    """Revoke the current session (FR-1.5)."""
    auth_service = AuthenticationService(session)
    user_id = await auth_service.logout(
        extract_refresh_token(request, payload.refresh_token),
        extract_access_token(request),
    )

    if user_id:
        await create_activity_log(
            session,
            user_id=user_id,
            ip_address=client_ip,
            description="Merchant logged out",
            data={},
        )

    clear_auth_cookies(response)
    return create_response(None, message="Logged out successfully")


@router.get("/me")
async def read_current_user(user: CurrentUserDep) -> StandardResponse[UserOut]:
    """Return the authenticated merchant's own record (AC-1.7 guards this route)."""
    return create_response(UserOut.model_validate(user, from_attributes=True))


@router.post("/change-password")
async def change_password(
    session: SessionDep,
    client_ip: ClientIPDep,
    user: CurrentUserDep,
    payload: ChangePasswordRequest,
    response: Response,
) -> StandardResponse[None]:
    """Change the password; every session — including this one — is revoked (AC-1.12)."""
    auth_service = AuthenticationService(session)
    await auth_service.change_password(user, payload)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Password changed; all sessions revoked",
        data={"user_public_id": user.public_id},
    )

    clear_auth_cookies(response)
    return create_response(None, message="Password changed successfully. Please log in again.")


# ── Password reset by OTP (FR-1.10) ──────────────────────────────────────────


@router.post("/password/forgot")
async def forgot_password(
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: ForgotPasswordRequest,
) -> StandardResponse[ForgotPasswordOut]:
    """Start a password reset: email an OTP and return its correlator."""
    auth_service = AuthenticationService(session)
    reset_token, user_id = await auth_service.forgot_password(payload)

    if user_id:
        await create_activity_log(
            session,
            user_id=user_id,
            ip_address=client_ip,
            description="Password reset OTP requested",
            data={},
        )

    # Same message and shape whether or not the account exists.
    return create_response(
        ForgotPasswordOut(reset_token=reset_token),
        message="If the account exists, an OTP has been sent.",
    )


@router.post("/otp/resend")
async def resend_otp(
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: ResendOtpRequest,
) -> StandardResponse[None]:
    """Re-send the OTP for an in-flight reset (2-minute cooldown)."""
    auth_service = AuthenticationService(session)
    user_id = await auth_service.resend_otp(payload)

    if user_id:
        await create_activity_log(
            session,
            user_id=user_id,
            ip_address=client_ip,
            description="Password reset OTP resent",
            data={},
        )

    return create_response(None, message="If the account exists, a new OTP has been sent.")


@router.post("/otp/verify")
async def verify_otp(
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: VerifyOtpRequest,
) -> StandardResponse[VerifyOtpOut]:
    """Exchange a correct OTP for a single-use password-reset token."""
    auth_service = AuthenticationService(session)
    password_reset_token, user_id = await auth_service.verify_otp(payload)

    await create_activity_log(
        session,
        user_id=user_id,
        ip_address=client_ip,
        description="Password reset OTP verified",
        data={},
    )

    return create_response(
        VerifyOtpOut(password_reset_token=password_reset_token),
        message="OTP verified successfully",
    )


@router.post("/password/reset")
async def reset_password(
    session: SessionDep,
    client_ip: ClientIPDep,
    payload: PasswordResetRequest,
) -> StandardResponse[None]:
    """Set a new password and revoke every session (AC-1.12)."""
    auth_service = AuthenticationService(session)
    user_id = await auth_service.reset_password(payload)

    await create_activity_log(
        session,
        user_id=user_id,
        ip_address=client_ip,
        description="Password reset; all sessions revoked",
        data={},
    )

    return create_response(None, message="Password reset successfully. Please log in again.")


# ── Permissions and roles (FR-1.11) ──────────────────────────────────────────


@router.get("/permissions")
async def list_permissions(
    session: SessionDep,
    user: CurrentUserDep,  # noqa: ARG001 — the catalogue is not public
) -> StandardResponse[list[PermissionOut]]:
    """Return the full permission catalogue, as seeded from ``PermissionCode``."""
    permissions = await AuthorizationService(session).get_permissions()
    return create_response(permissions, message="Permissions returned successfully")


@router.get("/permissions/me")
async def list_my_permissions(
    session: SessionDep,
    user: CurrentUserDep,
) -> StandardResponse[list[PermissionOut]]:
    """Return the permissions the current merchant actually holds."""
    permissions = await AuthorizationService(session).get_user_permissions_detail(require_id(user))
    return create_response(permissions, message="Permissions returned successfully")


@router.post(
    "/roles",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(PermissionCode.ADMIN_ROLE_CREATE))],
)
async def create_role(
    session: SessionDep,
    client_ip: ClientIPDep,
    user: CurrentUserDep,
    payload: RoleCreate,
) -> StandardResponse[RoleOut]:
    auth_service = AuthorizationService(session)
    role = await auth_service.create_role(payload)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Role created",
        data={"role_public_id": role.public_id, "name": role.name},
    )

    return create_response(RoleOut.model_validate(role, from_attributes=True), MESSAGE_201)


@router.get("/roles", dependencies=[Depends(require_permission(PermissionCode.ADMIN_ROLE_READ))])
async def list_roles(
    session: SessionDep,
    pagination: PaginationDep,
    search: str | None = None,
) -> StandardResponse[list[RoleOut]]:
    roles, total = await AuthorizationService(session).get_roles(pagination, search)
    return create_response(
        roles,
        message="Roles returned successfully",
        pagination=PaginationResponse(page=pagination.page, size=pagination.size, total=total),
    )


@router.get(
    "/roles/{public_id}",
    dependencies=[Depends(require_permission(PermissionCode.ADMIN_ROLE_READ))],
)
async def read_role(session: SessionDep, public_id: str) -> StandardResponse[RoleDetailOut]:
    role = await AuthorizationService(session).get_role(public_id)
    return create_response(role, message="Role returned successfully")


@router.put(
    "/roles/{public_id}",
    dependencies=[Depends(require_permission(PermissionCode.ADMIN_ROLE_UPDATE))],
)
async def update_role(
    session: SessionDep,
    client_ip: ClientIPDep,
    user: CurrentUserDep,
    public_id: str,
    payload: RoleUpdate,
) -> StandardResponse[RoleOut]:
    auth_service = AuthorizationService(session)
    role = await auth_service.update_role(payload, public_id)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Role updated",
        data={"role_public_id": public_id},
    )

    return create_response(
        RoleOut.model_validate(role, from_attributes=True), message="Role updated successfully"
    )


@router.delete(
    "/roles/{public_id}",
    dependencies=[Depends(require_permission(PermissionCode.ADMIN_ROLE_DELETE))],
)
async def delete_role(
    session: SessionDep,
    client_ip: ClientIPDep,
    user: CurrentUserDep,
    public_id: str,
) -> StandardResponse[None]:
    auth_service = AuthorizationService(session)
    await auth_service.delete_role(public_id)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Role deleted",
        data={"role_public_id": public_id},
    )

    return create_response(None, message="Role deleted successfully")


@router.post(
    "/users/{public_id}/roles",
    dependencies=[Depends(require_permission(PermissionCode.ADMIN_USER_UPDATE))],
)
async def assign_roles(
    session: SessionDep,
    client_ip: ClientIPDep,
    user: CurrentUserDep,
    public_id: str,
    payload: AssignRoleRequest,
) -> StandardResponse[None]:
    """Replace a merchant's roles. Takes effect on their next access token."""
    auth_service = AuthorizationService(session)
    await auth_service.assign_roles(user, payload.role_public_ids, public_id)

    await create_activity_log(
        session,
        user_id=require_id(user),
        ip_address=client_ip,
        description="Roles assigned",
        data={"user_public_id": public_id, "role_public_ids": payload.role_public_ids},
    )

    return create_response(None, message="Roles assigned successfully")
