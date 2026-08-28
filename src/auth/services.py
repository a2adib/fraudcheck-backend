"""
Auth services.

Ported from ``erp-backend/src/auth/services.py``: login, refresh rotation, reuse
detection, logout, OTP password reset (FR-1.10) and RBAC (FR-1.11).

Differences from upstream, all deliberate:

* A failed lookup still runs the KDF against :func:`dummy_password_hash`, so a wrong
  password and an unknown account cost the same (AC-1.5). Upstream returns early.
* Refresh secrets are hashed with SHA-256 rather than Argon2 — see ``models.py``.
* Nothing logs an email, a mobile or a token; log lines carry ``public_id`` only
  (FR-14.5).
* The OTP reaches the merchant by email only — this project has no SMS gateway, so a
  mobile identifier resolves the account but the code is sent to the address on file.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.orm import selectinload
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.auth.associations import RolePermissionLink, UserRoleLink
from src.auth.emails import send_otp_email
from src.auth.enums import Channel, PermissionCode, TokenDelivery
from src.auth.models import (
    Otp,
    PasswordResetToken,
    Permission,
    RetiredRefreshToken,
    Role,
    UserSession,
)
from src.auth.permissions import encode_permission_bitmask, get_user_permissions
from src.auth.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    PasswordResetRequest,
    PermissionOut,
    RegisterRequest,
    ResendOtpRequest,
    RoleCreate,
    RoleDetailOut,
    RoleOut,
    RoleUpdate,
    VerifyOtpRequest,
)
from src.auth.types import LoginResult, RefreshResult
from src.auth.utils import (
    create_access_token,
    decode_access_token,
    detect_channel,
    dummy_password_hash,
    get_password_hash,
    needs_rehash,
    verify_password,
)
from src.common.exceptions import HTTP400, HTTP404, AuthAPIError
from src.common.filters import PaginationParams
from src.common.mixins import require_id
from src.common.queries import get_count, get_ids_from_public_ids, get_instance_or_404
from src.common.utils import (
    compute_device_fingerprint,
    generate_opaque_token,
    generate_otp_token,
    generate_public_id,
    sha256_hex,
    split_prefixed_token,
)
from src.config import settings
from src.users.enums import UserStatus
from src.users.models import User

logger = logging.getLogger(__name__)


class AuthenticationService:
    def __init__(self, session: AsyncSession) -> None:
        """Service for registration, login, refresh rotation and logout (FR-1)."""
        self.session = session

    # ── Registration ────────────────────────────────────────────────────────

    async def register(self, payload: RegisterRequest) -> User:
        """
        Create a merchant account (FR-1.1).

        Collisions on either identifier raise the *same* 409 with the same message, so
        the response cannot be used to enumerate which emails or numbers are taken
        (AC-1.2).
        """
        email = payload.email.strip().lower()
        mobile = None
        if payload.mobile:
            channel, mobile = detect_channel(payload.mobile)
            if channel is not Channel.MOBILE:
                raise AuthAPIError(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_mobile_format",
                    "Invalid Bangladesh mobile number format.",
                )

        if await self._identifier_taken(email, mobile):
            raise AuthAPIError(
                status.HTTP_409_CONFLICT,
                "account_exists",
                "An account with these details already exists.",
            )

        user = User(
            email=email,
            mobile=mobile,
            password_hash=get_password_hash(payload.password),
            full_name=payload.full_name.strip(),
            locale=payload.locale,
        )
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)

        # A merchant owns their own tenant, so they hold every permission in it.
        await AuthorizationService(self.session).grant_default_role(user)

        logger.info("Merchant registered: public_id=%s", user.public_id)
        return user

    async def _identifier_taken(self, email: str, mobile: str | None) -> bool:
        query = select(User).where(User.email == email)
        if (await self.session.exec(query)).first():
            return True
        if mobile:
            taken = (await self.session.exec(select(User).where(User.mobile == mobile))).first()
            if taken:
                return True
        return False

    # ── Lookups ─────────────────────────────────────────────────────────────

    async def _get_user(self, channel: Channel, normalized: str) -> User | None:
        """
        Find a merchant by normalized identifier, regardless of active status.

        Status is checked explicitly in :meth:`login`, so a deactivated account gets a
        403 rather than a misleading credentials error.
        """
        if channel is Channel.EMAIL:
            query = select(User).where(User.email == normalized)
        else:
            query = select(User).where(User.mobile == normalized)
        return (await self.session.exec(query)).first()

    # ── Login ───────────────────────────────────────────────────────────────

    async def login(
        self,
        payload: LoginRequest,
        ip_address: str | None,
        user_agent: str | None,
    ) -> LoginResult:
        if not payload.identifier or not payload.password:
            raise AuthAPIError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_request",
                "Identifier and password are required.",
            )

        channel, normalized = detect_channel(payload.identifier)
        user = await self._get_user(channel, normalized)
        now = datetime.now(UTC)

        if user and (not user.is_active or user.status != UserStatus.ACTIVE):
            raise AuthAPIError(
                status.HTTP_403_FORBIDDEN,
                "account_deactivated",
                "Your account has been deactivated. Please contact support.",
            )

        # Lockout is checked before the password so a locked account stays locked even
        # when the correct password finally arrives (FR-1.8, AC-1.10).
        if user and user.locked_until and user.locked_until > now:
            retry_after = int((user.locked_until - now).total_seconds())
            raise AuthAPIError(
                status.HTTP_423_LOCKED,
                "account_locked",
                "Account temporarily locked. Try again later, or reset your password.",
                {
                    "locked_until": user.locked_until.isoformat(),
                    "retry_after_seconds": retry_after,
                },
            )

        # The KDF runs either way — see the module docstring (AC-1.5).
        password_matches = verify_password(
            payload.password, user.password_hash if user else dummy_password_hash()
        )
        if not user or not password_matches:
            if user:
                await self._register_failed_attempt(user)
            raise AuthAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "invalid_credentials",
                "Invalid email/mobile or password.",
            )

        # A bcrypt hash carried over from an erp-backend export is upgraded to Argon2id
        # here, the one moment the plaintext is available (FR-1 port note).
        if needs_rehash(user.password_hash):
            user.password_hash = get_password_hash(payload.password)

        user.failed_login_attempts = 0
        user.locked_until = None
        self.session.add(user)

        fingerprint = compute_device_fingerprint(user_agent)
        new_device = await self._is_new_device(require_id(user), ip_address, fingerprint)

        db_session, refresh_token = await self._create_session(
            user, ip_address, user_agent, fingerprint, payload.token_delivery
        )
        access_token = await self._mint_access_token(user, db_session)
        logger.info("Merchant authenticated: public_id=%s", user.public_id)
        return LoginResult(user, db_session, access_token, refresh_token, new_device)

    async def _mint_access_token(self, user: User, db_session: UserSession) -> str:
        """
        Mint the access token, embedding the merchant's permissions as the ``pb`` claim.

        Permissions are read once here rather than on every guarded request — see
        ``permissions.py`` for the staleness that buys.
        """
        codes = await get_user_permissions(self.session, require_id(user))
        return create_access_token(
            data={
                "sub": user.public_id,
                "sid": db_session.public_id,
                "pb": encode_permission_bitmask(codes),
            }
        )

    async def _register_failed_attempt(self, user: User) -> None:
        """Count the failure and lock the account once the threshold is reached."""
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
            user.locked_until = datetime.now(UTC) + timedelta(
                minutes=settings.ACCOUNT_LOCKOUT_MINUTES
            )
            user.failed_login_attempts = 0
        self.session.add(user)
        await self.session.commit()

    async def _is_new_device(self, user_id: int, ip_address: str | None, fingerprint: str) -> bool:
        """
        Whether this device/IP pair has been seen before.

        The very first login is not a "new device" — there is nothing to compare it
        against, and flagging it would make every signup look suspicious.
        """
        prior = (
            await self.session.exec(select(UserSession).where(UserSession.user_id == user_id))
        ).all()
        if not prior:
            return False
        return not any(
            existing.device_fingerprint == fingerprint
            or (ip_address and existing.ip_address == ip_address)
            for existing in prior
        )

    async def _create_session(
        self,
        user: User,
        ip_address: str | None,
        user_agent: str | None,
        fingerprint: str,
        delivery_mode: TokenDelivery,
    ) -> tuple[UserSession, str]:
        session_public_id = generate_public_id()
        secret = generate_opaque_token()
        db_session = UserSession(
            public_id=session_public_id,
            user_id=require_id(user),
            refresh_token_hash=sha256_hex(secret),
            ip_address=ip_address,
            user_agent=user_agent,
            device_fingerprint=fingerprint,
            delivery_mode=delivery_mode,
            expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
        self.session.add(db_session)
        await self.session.commit()
        await self.session.refresh(db_session)
        return db_session, f"{session_public_id}.{secret}"

    # ── Refresh ─────────────────────────────────────────────────────────────

    async def refresh(self, raw_token: str | None, ip_address: str | None) -> RefreshResult:
        """Rotate the refresh token and mint a fresh access token (FR-1.4)."""
        if not raw_token:
            raise AuthAPIError(
                status.HTTP_400_BAD_REQUEST, "missing_token", "Refresh token is required."
            )

        parsed = split_prefixed_token(raw_token)
        if not parsed:
            raise self._invalid_token()
        session_public_id, secret = parsed
        secret_hash = sha256_hex(secret)

        db_session = (
            await self.session.exec(
                select(UserSession).where(UserSession.public_id == session_public_id)
            )
        ).first()
        if not db_session:
            raise self._invalid_token()

        # Reuse detection (FR-1.6, AC-1.8): a secret that matches a retired hash was
        # already rotated once, so either it or its successor is in someone else's
        # hands. Revoke everything the merchant has.
        retired = (
            await self.session.exec(
                select(RetiredRefreshToken).where(
                    RetiredRefreshToken.session_id == db_session.id,
                    RetiredRefreshToken.token_hash == secret_hash,
                )
            )
        ).first()
        if retired:
            await self._invalidate_user_sessions(db_session.user_id)
            logger.warning(
                "Refresh token reuse detected; revoked all sessions for user_id=%s",
                db_session.user_id,
            )
            raise AuthAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "token_reuse_detected",
                "Refresh token reuse detected. All sessions have been revoked.",
            )

        if not db_session.is_active or db_session.is_expired():
            raise AuthAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "invalid_token",
                "Refresh token is expired or inactive.",
            )
        if db_session.refresh_token_hash != secret_hash:
            raise self._invalid_token()

        user = (await self.session.exec(select(User).where(User.id == db_session.user_id))).first()
        if not user or not user.is_active or user.status != UserStatus.ACTIVE:
            raise self._invalid_token()

        self.session.add(
            RetiredRefreshToken(session_id=db_session.id, token_hash=db_session.refresh_token_hash)
        )
        new_secret = generate_opaque_token()
        db_session.refresh_token_hash = sha256_hex(new_secret)
        db_session.last_active_at = datetime.now(UTC)
        if ip_address:
            db_session.ip_address = ip_address
        self.session.add(db_session)
        await self.session.commit()
        await self.session.refresh(db_session)

        access_token = await self._mint_access_token(user, db_session)
        return RefreshResult(
            require_id(user), db_session, access_token, f"{db_session.public_id}.{new_secret}"
        )

    @staticmethod
    def _invalid_token() -> AuthAPIError:
        """One message for every way a refresh token can be wrong — no oracle."""
        return AuthAPIError(status.HTTP_401_UNAUTHORIZED, "invalid_token", "Invalid refresh token.")

    async def _invalidate_user_sessions(
        self, user_id: int, exclude_session_id: int | None = None
    ) -> None:
        sessions = (
            await self.session.exec(
                select(UserSession).where(UserSession.user_id == user_id, UserSession.is_active)
            )
        ).all()
        for db_session in sessions:
            if exclude_session_id is not None and db_session.id == exclude_session_id:
                continue
            db_session.is_active = False
            self.session.add(db_session)
        await self.session.commit()

    # ── Logout ──────────────────────────────────────────────────────────────

    async def logout(self, raw_refresh: str | None, access_token: str | None = None) -> int | None:
        """
        Deactivate the session behind the presented token (FR-1.5).

        JSON clients send the refresh token. Cookie clients cannot: their refresh
        cookie is scoped to the refresh path and is not sent here, so the ``sid`` claim
        on the access token is the fallback.
        """
        if not raw_refresh and not access_token:
            raise AuthAPIError(
                status.HTTP_400_BAD_REQUEST,
                "missing_token",
                "A refresh or access token is required.",
            )

        db_session = await self._session_from_refresh(raw_refresh)
        if db_session is None and access_token:
            db_session = await self._session_from_access(access_token)
        if not db_session:
            return None

        db_session.is_active = False
        self.session.add(db_session)
        await self.session.commit()
        return db_session.user_id

    async def _session_from_refresh(self, raw_refresh: str | None) -> UserSession | None:
        if not raw_refresh:
            return None
        parsed = split_prefixed_token(raw_refresh)
        if not parsed:
            return None
        session_public_id, secret = parsed
        db_session = (
            await self.session.exec(
                select(UserSession).where(UserSession.public_id == session_public_id)
            )
        ).first()
        if db_session and db_session.refresh_token_hash == sha256_hex(secret):
            return db_session
        return None

    async def _session_from_access(self, access_token: str) -> UserSession | None:
        try:
            payload = decode_access_token(access_token)
        except HTTPException:
            return None
        session_public_id = payload.get("sid")
        if not session_public_id:
            return None
        return (
            await self.session.exec(
                select(UserSession).where(UserSession.public_id == session_public_id)
            )
        ).first()

    # ── Password change ─────────────────────────────────────────────────────

    async def change_password(self, user: User, payload: ChangePasswordRequest) -> None:
        """
        Change the password and revoke every session (FR-1.10 clause, AC-1.12).

        Every previously issued refresh token dies with the sessions, so a stolen one
        stops working the moment the merchant reacts to the theft.
        """
        if not verify_password(payload.current_password, user.password_hash):
            raise AuthAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "invalid_credentials",
                "Current password is incorrect.",
            )

        user.password_hash = get_password_hash(payload.new_password)
        self.session.add(user)
        await self.session.commit()
        await self._invalidate_user_sessions(require_id(user))
        logger.info("Password changed and sessions revoked: public_id=%s", user.public_id)

    # ── Password reset by OTP (FR-1.10) ─────────────────────────────────────

    async def forgot_password(self, payload: ForgotPasswordRequest) -> tuple[str, int | None]:
        """
        Issue an OTP and return ``(reset_token, user_id | None)``.

        A ``reset_token`` comes back whether or not the account exists, and whether or
        not it has an email address to send to: the caller must not be able to tell
        (AC-1.2's no-enumeration rule, applied to the reset path).
        """
        channel, normalized = detect_channel(payload.identifier)
        user = await self._get_user(channel, normalized)
        reset_token = generate_opaque_token(24)

        if not user or not user.is_active:
            return reset_token, None

        existing = await self._latest_otp(require_id(user))
        if existing and existing.is_in_cooldown():
            raise self._cooldown_error(existing)

        await self._issue_otp(user, reset_token)
        return reset_token, require_id(user)

    async def resend_otp(self, payload: ResendOtpRequest) -> int | None:
        """Re-send the OTP for an in-flight reset, honouring the 2-minute cooldown."""
        channel, normalized = detect_channel(payload.identifier)
        user = await self._get_user(channel, normalized)
        if not user or not user.is_active:
            return None

        existing = (
            await self.session.exec(
                select(Otp)
                .where(Otp.user_id == user.id, Otp.reset_token == payload.reset_token)
                .order_by(col(Otp.created_at).desc())
            )
        ).first()
        if not existing:
            # An unknown correlator is not an error the caller gets to distinguish.
            return None
        if existing.is_in_cooldown():
            raise self._cooldown_error(existing)

        await self._issue_otp(user, payload.reset_token)
        return require_id(user)

    async def _issue_otp(self, user: User, reset_token: str) -> None:
        """Void any outstanding OTP, mint a new one, and email it."""
        await self.session.exec(
            update(Otp).where(col(Otp.user_id) == user.id).values(is_active=False)
        )
        otp_code = generate_otp_token()
        self.session.add(
            Otp(
                user_id=require_id(user),
                token_hash=get_password_hash(otp_code),
                reset_token=reset_token,
            )
        )
        await self.session.commit()

        if not user.email:
            # Mobile-only account: nothing to send to. The caller still gets the generic
            # response, so this stays invisible from outside.
            logger.warning(
                "Reset requested for an account with no email: public_id=%s", user.public_id
            )
            return
        await send_otp_email(user.email, otp_code)

    async def _latest_otp(self, user_id: int) -> Otp | None:
        return (
            await self.session.exec(
                select(Otp)
                .where(Otp.user_id == user_id, Otp.is_active)
                .order_by(col(Otp.created_at).desc())
            )
        ).first()

    @staticmethod
    def _cooldown_error(otp: Otp) -> AuthAPIError:
        ready_at = otp.last_sent_at + timedelta(minutes=settings.OTP_RETRY_DELAY_MINUTES)
        remaining = max(int((ready_at - datetime.now(UTC)).total_seconds()), 0)
        return AuthAPIError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "otp_cooldown",
            "Please wait before requesting another OTP.",
            {"retry_after_seconds": remaining},
        )

    async def verify_otp(self, payload: VerifyOtpRequest) -> tuple[str, int]:
        """Exchange a correct OTP for a single-use password-reset token."""
        channel, normalized = detect_channel(payload.identifier)
        user = await self._get_user(channel, normalized)
        if not user:
            raise self._invalid_otp()

        db_otp = (
            await self.session.exec(
                select(Otp)
                .where(
                    Otp.user_id == user.id,
                    Otp.reset_token == payload.reset_token,
                    Otp.is_active,
                )
                .order_by(col(Otp.created_at).desc())
            )
        ).first()
        if not db_otp:
            raise self._invalid_otp()

        if db_otp.is_expired():
            await self._void_otp(db_otp)
            raise AuthAPIError(
                status.HTTP_410_GONE,
                "otp_expired",
                "OTP has expired. Please request a new one.",
            )

        if not verify_password(payload.otp, db_otp.token_hash):
            db_otp.retries += 1
            self.session.add(db_otp)
            await self.session.commit()
            if db_otp.retries >= settings.OTP_RETRY_LIMIT:
                await self._void_otp(db_otp)
                raise self._attempts_exceeded()
            raise self._invalid_otp()

        secret = generate_opaque_token()
        reset_token_row = PasswordResetToken(
            user_id=require_id(user),
            token_hash=sha256_hex(secret),
            expires_at=datetime.now(UTC)
            + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES),
        )
        self.session.add(reset_token_row)
        await self._void_otp(db_otp)
        await self.session.refresh(reset_token_row)
        return f"{reset_token_row.public_id}.{secret}", require_id(user)

    async def _void_otp(self, otp: Otp) -> None:
        otp.is_active = False
        self.session.add(otp)
        await self.session.commit()

    @staticmethod
    def _invalid_otp() -> AuthAPIError:
        """One message for a wrong code, a wrong correlator and an unknown account."""
        return AuthAPIError(status.HTTP_401_UNAUTHORIZED, "invalid_otp", "Incorrect OTP.")

    @staticmethod
    def _attempts_exceeded() -> AuthAPIError:
        return AuthAPIError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "otp_attempts_exceeded",
            "Too many failed attempts. Please restart the reset process.",
        )

    async def reset_password(self, payload: PasswordResetRequest) -> int:
        """
        Set a new password from a verified reset token and revoke every session.

        The reset is unauthenticated, so there is no "current" session worth keeping —
        and a merchant resetting a password is usually reacting to a compromise
        (FR-1.10, AC-1.12).
        """
        parsed = split_prefixed_token(payload.password_reset_token)
        if not parsed:
            raise self._invalid_reset_token()
        token_public_id, secret = parsed

        reset_token_row = (
            await self.session.exec(
                select(PasswordResetToken).where(PasswordResetToken.public_id == token_public_id)
            )
        ).first()
        if (
            not reset_token_row
            or reset_token_row.used
            or reset_token_row.is_expired()
            or reset_token_row.token_hash != sha256_hex(secret)
        ):
            raise self._invalid_reset_token()

        user = (
            await self.session.exec(select(User).where(User.id == reset_token_row.user_id))
        ).first()
        if not user:
            raise self._invalid_reset_token()

        user.password_hash = get_password_hash(payload.new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        reset_token_row.used = True
        self.session.add(user)
        self.session.add(reset_token_row)
        await self.session.commit()

        await self._invalidate_user_sessions(require_id(user))
        logger.info("Password reset and sessions revoked: public_id=%s", user.public_id)
        return require_id(user)

    @staticmethod
    def _invalid_reset_token() -> AuthAPIError:
        return AuthAPIError(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_reset_token",
            "Invalid or expired reset token.",
        )


#: Every merchant gets this role at registration — they own their own tenant, so they
#: hold every permission inside it. Created by ``sync_permission_catalogue()``.
DEFAULT_ROLE_NAME = "Owner"


def _humanize(code: str) -> str:
    """``risk.order.review`` -> ``Risk order review`` — a default label for the catalogue."""
    return code.replace(".", " ").replace("_", " ").capitalize()


class AuthorizationService:
    def __init__(self, session: AsyncSession) -> None:
        """Service for roles, permissions and grants (FR-1.11)."""
        self.session = session

    # ── Catalogue ───────────────────────────────────────────────────────────

    async def sync_permission_catalogue(self) -> list[Permission]:
        """
        Mint one ``Permission`` row per :class:`PermissionCode`, and the owner role.

        Idempotent: existing rows are reactivated rather than duplicated, and codes that
        have been retired from the enum are deactivated rather than deleted (their bit
        index must stay reserved). Run by ``just permissions`` and by the seeder.
        """
        existing = {
            permission.code: permission
            for permission in (await self.session.exec(select(Permission))).all()
        }

        for code in PermissionCode:
            permission = existing.get(code.value)
            if permission is None:
                self.session.add(
                    Permission(code=code.value, label=_humanize(code.value), is_active=True)
                )
            elif not permission.is_active:
                permission.is_active = True
                self.session.add(permission)

        live_codes = {code.value for code in PermissionCode}
        for stored_code, permission in existing.items():
            if stored_code not in live_codes and permission.is_active:
                permission.is_active = False
                self.session.add(permission)

        await self.session.commit()
        await self._sync_owner_role()

        return list((await self.session.exec(select(Permission).where(Permission.is_active))).all())

    async def _sync_owner_role(self) -> Role:
        """Create or refresh the owner role so it holds every live permission."""
        role = (await self.session.exec(select(Role).where(Role.name == DEFAULT_ROLE_NAME))).first()
        if role is None:
            role = Role(
                name=DEFAULT_ROLE_NAME,
                description="Full access to the merchant's own tenant.",
            )
            self.session.add(role)
            await self.session.commit()
            await self.session.refresh(role)
        elif not role.is_active:
            role.is_active = True
            self.session.add(role)
            await self.session.commit()

        permission_ids = (
            await self.session.exec(select(Permission.id).where(Permission.is_active))
        ).all()
        linked = set(
            (
                await self.session.exec(
                    select(RolePermissionLink.permission_id).where(
                        RolePermissionLink.role_id == role.id
                    )
                )
            ).all()
        )
        for permission_id in permission_ids:
            if permission_id not in linked:
                self.session.add(
                    RolePermissionLink(role_id=require_id(role), permission_id=permission_id)
                )
        await self.session.commit()
        return role

    async def grant_default_role(self, user: User, granted_by: User | None = None) -> None:
        """
        Give a freshly registered merchant the owner role.

        A deployment that has never run ``just permissions`` has no role to grant; that
        is logged and tolerated, because failing registration over a missing catalogue
        would be worse than a merchant who has to be granted a role afterwards.
        """
        role = (
            await self.session.exec(
                select(Role).where(Role.name == DEFAULT_ROLE_NAME, Role.is_active)
            )
        ).first()
        if role is None:
            logger.warning(
                "No %s role found — run `just permissions`. Registered merchant has no "
                "permissions: public_id=%s",
                DEFAULT_ROLE_NAME,
                user.public_id,
            )
            return

        self.session.add(
            UserRoleLink(
                user_id=require_id(user),
                role_id=require_id(role),
                created_by_id=require_id(granted_by or user),
            )
        )
        await self.session.commit()

    # ── Roles ───────────────────────────────────────────────────────────────

    async def create_role(self, payload: RoleCreate) -> Role:
        if not await self._name_is_free(payload.name):
            raise HTTP400(detail="A role already exists with that name")

        role = Role(name=payload.name, description=payload.description)
        self.session.add(role)
        await self.session.commit()
        await self.session.refresh(role)

        if payload.permission_public_ids:
            await self.assign_permissions(payload.permission_public_ids, role)
        return role

    async def update_role(self, payload: RoleUpdate, public_id: str) -> Role:
        role = await get_instance_or_404(session=self.session, model=Role, public_id=public_id)

        if payload.name and payload.name != role.name:
            if not await self._name_is_free(payload.name):
                raise HTTP400(detail="A role already exists with that name")
            role.name = payload.name
        if payload.description is not None:
            role.description = payload.description

        self.session.add(role)
        await self.session.commit()

        if payload.permission_public_ids is not None:
            await self.assign_permissions(payload.permission_public_ids, role)
        return role

    async def delete_role(self, public_id: str) -> None:
        """Soft delete a role and drop every grant of it."""
        role = await get_instance_or_404(session=self.session, model=Role, public_id=public_id)

        await self.session.exec(delete(UserRoleLink).where(col(UserRoleLink.role_id) == role.id))
        role.is_active = False
        self.session.add(role)
        await self.session.commit()

    async def _name_is_free(self, name: str) -> bool:
        taken = await self.session.scalar(
            select(Role.id).where(Role.is_active, func.lower(Role.name) == func.lower(name))
        )
        return taken is None

    async def get_roles(
        self, pagination: PaginationParams | None = None, search: str | None = None
    ) -> tuple[list[RoleOut], int]:
        query = select(Role).where(Role.is_active)
        if search:
            query = query.where(col(Role.name).ilike(f"%{search}%"))

        total = await get_count(session=self.session, query=query)
        if pagination:
            query = query.offset(pagination.offset).limit(pagination.size)

        roles = (await self.session.exec(query)).all()
        return [RoleOut.model_validate(role, from_attributes=True) for role in roles], total

    async def get_role(self, public_id: str) -> RoleDetailOut:
        role = (
            await self.session.exec(
                select(Role)
                .where(Role.public_id == public_id, Role.is_active)
                .options(selectinload(Role.permissions))  # type: ignore[arg-type]
            )
        ).first()
        if not role:
            raise HTTP404(detail="Role not found")
        return RoleDetailOut.model_validate(role, from_attributes=True)

    # ── Grants ──────────────────────────────────────────────────────────────

    async def assign_permissions(self, permission_public_ids: list[str], role: Role) -> None:
        """Replace the role's permissions with exactly the ones given."""
        permission_ids = await get_ids_from_public_ids(
            session=self.session, model=Permission, public_ids=permission_public_ids
        )

        await self.session.exec(
            delete(RolePermissionLink).where(col(RolePermissionLink.role_id) == role.id)
        )
        for permission_id in permission_ids:
            self.session.add(
                RolePermissionLink(role_id=require_id(role), permission_id=permission_id)
            )
        await self.session.commit()

    async def assign_roles(
        self, assigned_by: User, role_public_ids: list[str], user_public_id: str
    ) -> User:
        """
        Replace a merchant's roles with exactly the ones given.

        Replacement, not addition: the payload is the complete new set, so revoking is
        the same call as granting. The change reaches the merchant's guards when their
        next access token is minted (see ``permissions.py``).
        """
        target = await get_instance_or_404(
            session=self.session, model=User, public_id=user_public_id
        )
        role_ids = await get_ids_from_public_ids(
            session=self.session, model=Role, public_ids=role_public_ids
        )

        await self.session.exec(delete(UserRoleLink).where(col(UserRoleLink.user_id) == target.id))
        for role_id in role_ids:
            self.session.add(
                UserRoleLink(
                    user_id=require_id(target),
                    role_id=role_id,
                    created_by_id=require_id(assigned_by),
                )
            )
        await self.session.commit()
        return target

    # ── Reads ───────────────────────────────────────────────────────────────

    async def get_permissions(self) -> list[PermissionOut]:
        permissions = (
            await self.session.exec(
                select(Permission).where(Permission.is_active).order_by(col(Permission.id))
            )
        ).all()
        return [
            PermissionOut.model_validate(permission, from_attributes=True)
            for permission in permissions
        ]

    async def get_user_permissions_detail(self, user_id: int) -> list[PermissionOut]:
        statement = (
            select(Permission)
            .join(RolePermissionLink, col(RolePermissionLink.permission_id) == Permission.id)
            .join(UserRoleLink, col(UserRoleLink.role_id) == RolePermissionLink.role_id)
            .where(UserRoleLink.user_id == user_id, Permission.is_active)
            .distinct()
            .order_by(col(Permission.id))
        )
        permissions = (await self.session.exec(statement)).all()
        return [
            PermissionOut.model_validate(permission, from_attributes=True)
            for permission in permissions
        ]
