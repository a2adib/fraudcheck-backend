"""
RBAC link tables (FR-1.11).

Kept in their own module, as upstream does, so ``models.py`` and ``permissions.py`` can
both import them without a cycle.
"""

from sqlmodel import Field, SQLModel, UniqueConstraint

from src.common.mixins import CommonFieldMixin


class RolePermissionLink(SQLModel, table=True):
    """Which permissions a role grants. Rewritten wholesale when a role is edited."""

    role_id: int = Field(foreign_key="role.id", nullable=False, primary_key=True)
    permission_id: int = Field(foreign_key="permission.id", nullable=False, primary_key=True)


class UserRoleLink(CommonFieldMixin, SQLModel, table=True):
    """
    Which roles a merchant holds.

    Carries the mixin (rather than being a bare composite-PK link like
    ``RolePermissionLink``) because who granted a role, and when, is audit-relevant.
    """

    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    user_id: int = Field(foreign_key="user.id", nullable=False, index=True)
    role_id: int = Field(foreign_key="role.id", nullable=False, index=True)
    created_by_id: int = Field(foreign_key="user.id", nullable=False)
