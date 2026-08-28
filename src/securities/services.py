"""Audit services."""

import logging
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.securities.models import ActivityLog
from src.users.models import User

logger = logging.getLogger(__name__)


async def create_activity_log(
    session: AsyncSession,
    user_id: int,
    ip_address: str,
    description: str,
    data: dict[str, Any] | None = None,
) -> ActivityLog:
    """
    Record a side-effecting action and bump the actor's ``last_activity_at`` (FR-1.12).

    Call this *after* the business logic has succeeded, so a rolled-back operation
    leaves no audit trail claiming it happened.

    ``data`` is stored verbatim as JSON — pass public ids and flags only, never
    credentials, tokens, full phone numbers, amounts or addresses (FR-14.5).
    """
    activity_log = ActivityLog(
        ip_address=ip_address,
        description=description,
        data=data or {},
        created_by_id=user_id,
    )
    session.add(activity_log)
    await session.commit()
    await session.refresh(activity_log)

    db_user = (await session.exec(select(User).where(User.id == user_id))).first()
    if db_user:
        db_user.last_activity_at = activity_log.created_at
        session.add(db_user)
        await session.commit()

    logger.info("Activity log created for user_id=%s: %s", user_id, description)
    return activity_log
