"""Auth request/response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

from src.auth.enums import TokenDelivery
from src.auth.utils import MIN_PASSWORD_LENGTH
from src.common.utils import OTP_LENGTH
from src.users.enums import Locale, UserStatus


class RegisterRequest(BaseModel):
    """FR-1.1. Email and password are required; mobile is optional."""

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    full_name: str = Field(min_length=1)
    mobile: str | None = None
    locale: Locale = Locale.EN


class UserOut(BaseModel):
    """The merchant record as the API exposes it — never carries the hash (AC-1.1)."""

    public_id: str
    email: EmailStr | None
    mobile: str | None
    full_name: str
    locale: Locale
    status: UserStatus
    last_activity_at: datetime | None
    created_at: datetime


class LoginRequest(BaseModel):
    identifier: str
    password: str
    token_delivery: TokenDelivery = TokenDelivery.JSON


class TokenOut(BaseModel):
    """
    The token pair, as the body carries it.

    Both fields are optional because cookie delivery returns neither (AC-1.11) — in
    that mode the body must contain no token string at all.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: int
    token_delivery: TokenDelivery = TokenDelivery.JSON
    new_device: bool = False


class RefreshRequest(BaseModel):
    # Optional: cookie mode reads the refresh token from its scoped cookie instead.
    refresh_token: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    confirm_password: str

    @model_validator(mode="after")
    def check_passwords_match(self) -> "ChangePasswordRequest":
        if self.new_password != self.confirm_password:
            msg = "Password and confirmation password do not match"
            raise ValueError(msg)
        return self


# ── Password reset by OTP (FR-1.10) ──────────────────────────────────────────


class ForgotPasswordRequest(BaseModel):
    identifier: str


class ForgotPasswordOut(BaseModel):
    """
    The correlator for this reset attempt.

    Returned even when no account matched, so the response cannot be used to test
    whether an email or mobile is registered.
    """

    reset_token: str


class VerifyOtpRequest(BaseModel):
    identifier: str
    otp: str = Field(min_length=OTP_LENGTH, max_length=OTP_LENGTH, pattern=r"^\d+$")
    reset_token: str


class VerifyOtpOut(BaseModel):
    password_reset_token: str


class ResendOtpRequest(BaseModel):
    identifier: str
    reset_token: str


class PasswordResetRequest(BaseModel):
    password_reset_token: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)
    confirm_password: str

    @model_validator(mode="after")
    def check_passwords_match(self) -> "PasswordResetRequest":
        if self.new_password != self.confirm_password:
            msg = "Password and confirmation password do not match"
            raise ValueError(msg)
        return self


# ── RBAC (FR-1.11) ───────────────────────────────────────────────────────────


class PermissionOut(BaseModel):
    public_id: str
    code: str
    label: str
    description: str | None


class RoleCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    permission_public_ids: list[str] = []


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    permission_public_ids: list[str] | None = None


class RoleOut(BaseModel):
    public_id: str
    name: str
    description: str | None


class RoleDetailOut(RoleOut):
    permissions: list[PermissionOut]


class AssignRoleRequest(BaseModel):
    """Replaces the user's roles wholesale — the list is the new complete set."""

    role_public_ids: list[str]
