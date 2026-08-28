from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint
from sqlmodel import Field, SQLModel

from src.common.mixins import CommonFieldMixin
from src.common.types import TZDateTime
from src.users.enums import Locale, UserStatus


class User(CommonFieldMixin, SQLModel, table=True):
    """
    A merchant account.

    The merchant **is** the tenant — there is no separate organisation table. Every
    domain row hangs off ``user_id`` and every query filters on it (see AGENTS.md,
    "Multi-tenancy"). This differs from ``erp-backend``, whose ``User`` belongs to a
    ``Company``.
    """

    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR mobile IS NOT NULL",
            name="email_or_mobile_required",
        ),
    )

    email: str | None = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True, index=True),
    )
    mobile: str | None = Field(
        default=None,
        sa_column=sa.Column(sa.String, unique=True, nullable=True, index=True),
    )
    password_hash: str
    full_name: str
    # values_callable so Postgres stores the lowercase locale codes ("en"/"bn") rather
    # than SQLAlchemy's default of the member *names* ("EN"/"BN"). The frontend and the
    # Accept-Language header both speak lowercase.
    locale: Locale = Field(
        default=Locale.EN,
        sa_column=sa.Column(
            sa.Enum(Locale, values_callable=lambda enum: [member.value for member in enum]),
            nullable=False,
            server_default=Locale.EN.value,
        ),
    )
    status: UserStatus = Field(
        default=UserStatus.ACTIVE,
        sa_column=sa.Column(
            sa.Enum(UserStatus), nullable=False, server_default=UserStatus.ACTIVE.value
        ),
    )

    failed_login_attempts: int = Field(default=0, nullable=False)
    locked_until: datetime | None = Field(
        sa_type=TZDateTime,
        default=None,
        nullable=True,
    )
    last_activity_at: datetime | None = Field(
        sa_type=TZDateTime,
        default=None,
        nullable=True,
    )
