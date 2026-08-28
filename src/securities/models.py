"""
Audit models.

``ActivityLog`` is the append-only record of every side-effecting request (FR-1.12,
FR-14.4). Ported from ``erp-backend/src/securities/models.py`` without its
``permission_mode`` / ``required_permissions`` columns — those describe the RBAC check
that authorised the call, and RBAC (FR-1.11) has not landed yet.

``data`` must never carry a credential, a token, a full phone number, an order amount
or an address (AGENTS.md, "Things that will bite you"). Callers pass public ids.
"""

from typing import Any

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from src.common.mixins import CommonFieldMixin


class ActivityLog(CommonFieldMixin, SQLModel, table=True):
    ip_address: str = Field(nullable=False)
    description: str = Field(nullable=False)
    data: dict[str, Any] = Field(default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False))

    # The acting merchant. Named ``created_by_id`` rather than ``user_id`` because the
    # row records *who acted*, which for admin-initiated actions need not be the tenant
    # the row is about.
    created_by_id: int = Field(foreign_key="user.id", nullable=False, index=True)
