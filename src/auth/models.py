"""
Auth models.

Ported from ``erp-backend/src/auth/models.py``.

One deliberate change from upstream: *high-entropy* token secrets are stored as SHA-256
hex, not as password hashes. A refresh or reset secret is 32 random bytes, so it has no
brute-force surface that a slow KDF would protect; hashing it cheaply and
deterministically also turns reuse detection into one indexed lookup instead of an
Argon2 verify per retired token (FR-1.6).

The OTP is the exception and keeps Argon2 — see :class:`Otp`.
"""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlmodel import Field, Relationship, SQLModel

from src.auth.associations import RolePermissionLink
from src.auth.enums import TokenDelivery
from src.common.mixins import CommonFieldMixin
from src.common.types import TZDateTime
from src.config import settings


class UserSession(CommonFieldMixin, SQLModel, table=True):
    """
    A login session backing one refresh token.

    ``public_id`` (from the mixin) is the prefix of the opaque refresh token
    (``{public_id}.{secret}``); only the hashed secret is stored. ``is_active`` is
    flipped off on logout (FR-1.5), on detected reuse (FR-1.6) and on password reset
    (FR-1.10).
    """

    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    refresh_token_hash: str = Field(nullable=False)
    ip_address: str | None = Field(default=None, nullable=True)
    user_agent: str | None = Field(default=None, nullable=True)
    device_fingerprint: str | None = Field(default=None, nullable=True, index=True)
    # values_callable so Postgres stores "json"/"cookie" rather than SQLAlchemy's
    # default of the member *names* — same reasoning as User.locale.
    delivery_mode: TokenDelivery = Field(
        default=TokenDelivery.JSON,
        sa_column=sa.Column(
            sa.Enum(TokenDelivery, values_callable=lambda enum: [m.value for m in enum]),
            nullable=False,
            server_default=TokenDelivery.JSON.value,
        ),
    )
    last_active_at: datetime = Field(
        sa_type=TZDateTime,
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )
    expires_at: datetime = Field(sa_type=TZDateTime, nullable=False)

    def is_expired(self) -> bool:
        return self.expires_at < datetime.now(UTC)


class RetiredRefreshToken(CommonFieldMixin, SQLModel, table=True):
    """
    Hashes of refresh secrets that have already been rotated out of a session.

    Presenting one is treated as theft: every session for that user is revoked
    (FR-1.6, AC-1.8). Rows are kept for the lifetime of the session they belong to —
    a token that can no longer be replayed within the refresh window is no longer
    evidence of anything.
    """

    session_id: int = Field(foreign_key="usersession.id", nullable=False, index=True)
    token_hash: str = Field(nullable=False, index=True)


class Otp(CommonFieldMixin, SQLModel, table=True):
    """
    A one-time password for the reset flow (FR-1.10).

    The plaintext code is never stored — only ``token_hash``, and unlike every other
    token in this module that hash is **Argon2**, not SHA-256: six digits is a keyspace
    of one million, small enough that a fast hash of a leaked table is trivially
    reversible. ``reset_token`` is the opaque correlator handed back by
    ``/auth/password/forgot`` and replayed by verify and resend, so an OTP can only be
    consumed by the flow that asked for it.
    """

    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    token_hash: str = Field(nullable=False)
    reset_token: str = Field(nullable=False, index=True)
    retries: int = Field(default=0, nullable=False)
    last_sent_at: datetime = Field(
        sa_type=TZDateTime,
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
    )

    def is_expired(self) -> bool:
        return self.created_at + timedelta(minutes=settings.OTP_EXPIRE_MINUTES) < datetime.now(UTC)

    def is_in_cooldown(self) -> bool:
        """Whether the resend cooldown (FR-1.10, 2 minutes) is still running."""
        cooldown = timedelta(minutes=settings.OTP_RETRY_DELAY_MINUTES)
        return self.last_sent_at + cooldown > datetime.now(UTC)


class PasswordResetToken(CommonFieldMixin, SQLModel, table=True):
    """
    Single-use token issued once the OTP is verified, scoped to setting a new password.

    Handed to the client as ``{public_id}.{secret}`` like the refresh token, and only
    the hashed secret is stored. ``used`` makes replay a 401 rather than a second reset.
    """

    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    token_hash: str = Field(nullable=False)
    expires_at: datetime = Field(sa_type=TZDateTime, nullable=False)
    used: bool = Field(default=False, nullable=False)

    def is_expired(self) -> bool:
        return self.expires_at < datetime.now(UTC)


class Role(CommonFieldMixin, SQLModel, table=True):
    """
    A named bundle of permissions (FR-1.11).

    Global, not tenant-scoped: a merchant *is* the tenant here, so roles are a catalogue
    the deployment defines rather than something each merchant authors. The unique index
    is on ``lower(name)`` and applies only to live rows, so a soft-deleted role does not
    reserve its name forever.
    """

    __table_args__ = (
        sa.Index(
            "uq_role_name_active",
            sa.func.lower(sa.column("name")),
            unique=True,
            postgresql_where="is_active",
        ),
    )

    name: str = Field(nullable=False)
    description: str | None = Field(default=None, nullable=True)

    # Only the role <-> permission side gets a relationship. Who *holds* a role is read
    # through explicit joins in permissions.py; a User relationship would need disambiguating
    # foreign_keys for UserRoleLink.created_by_id and buys nothing.
    permissions: list["Permission"] = Relationship(
        back_populates="roles", link_model=RolePermissionLink
    )


class Permission(CommonFieldMixin, SQLModel, table=True):
    """
    One grantable capability, keyed by its dotted ``code`` (FR-1.11).

    Rows are minted from :class:`~src.auth.enums.PermissionCode` by
    ``sync_permission_catalogue()`` — never by hand, and never by an API call.
    """

    code: str = Field(unique=True, index=True, nullable=False)
    label: str = Field(nullable=False)
    description: str | None = Field(default=None, nullable=True)

    roles: list["Role"] = Relationship(back_populates="permissions", link_model=RolePermissionLink)
