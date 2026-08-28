import logging
from typing import Any, TypeVar

from sqlalchemy import Select, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.common.exceptions import HTTP404
from src.common.mixins import CommonFieldMixin, IDMixin

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=CommonFieldMixin)


async def get_instance_or_404(session: AsyncSession, model: type[T], public_id: str) -> T:
    query = await session.exec(select(model).where(model.is_active, model.public_id == public_id))
    db_instance = query.one_or_none()
    if not db_instance:
        raise HTTP404(detail=f"{model.__name__} not found")
    return db_instance


async def get_tenant_instance_or_404(
    session: AsyncSession, model: type[T], public_id: str, user_id: int
) -> T:
    """
    Tenant-scoped lookup. Use this for every merchant-owned row.

    Returns 404 — never 403 — when the row belongs to another tenant, so existence is
    not leaked across tenants (AC-8.4).
    """
    query = await session.exec(
        select(model).where(
            model.is_active,
            model.public_id == public_id,
            model.user_id == user_id,  # type: ignore[attr-defined]
        )
    )
    db_instance = query.one_or_none()
    if not db_instance:
        raise HTTP404(detail=f"{model.__name__} not found")
    return db_instance


async def get_instance_by_id_or_404(session: AsyncSession, model: type[T], instance_id: int) -> T:
    query = await session.exec(select(model).where(model.is_active, model.id == instance_id))
    db_instance = query.one_or_none()
    if not db_instance:
        raise HTTP404(detail=f"{model.__name__} not found")
    return db_instance


async def get_instance_id_or_none(
    session: AsyncSession, model: type[CommonFieldMixin], public_id: str
) -> int | None:
    instance_id: int | None = await session.scalar(
        select(model.id).where(model.is_active, model.public_id == public_id)
    )
    return instance_id


async def get_instance_id_or_404(
    session: AsyncSession, model: type[CommonFieldMixin], public_id: str
) -> int:
    db_instance_id = await get_instance_id_or_none(
        session=session, model=model, public_id=public_id
    )
    if db_instance_id is None:
        raise HTTP404(detail=f"{model.__name__} not found")
    return db_instance_id


async def get_ids_from_public_ids(
    session: AsyncSession, model: type[CommonFieldMixin], public_ids: list[str]
) -> list[int]:
    """
    Resolve public ids to internal ids, preserving the caller's order.

    Raises 404 naming the ids that did not resolve, so a partially valid list fails
    loudly instead of silently linking a subset.
    """
    rows = (
        await session.exec(
            select(model.id, model.public_id).where(
                model.is_active,
                model.public_id.in_(public_ids),  # type: ignore[attr-defined]
            )
        )
    ).all()
    id_by_public_id: dict[str, int] = {
        public_id: instance_id for instance_id, public_id in rows if instance_id is not None
    }

    missing = [public_id for public_id in public_ids if public_id not in id_by_public_id]
    if missing:
        raise HTTP404(detail=f"{model.__name__} not found: {', '.join(missing)}")

    return [id_by_public_id[public_id] for public_id in public_ids]


async def delete_instance_or_404(session: AsyncSession, model: type[T], public_id: str) -> None:
    """Soft delete an instance."""
    logger.info("Soft deleting %s public_id=%s", model.__name__, public_id)
    db_instance = await get_instance_or_404(session=session, model=model, public_id=public_id)
    db_instance.is_active = False
    session.add(db_instance)
    await session.commit()


async def hard_delete_instance_or_404(
    session: AsyncSession, model: type[IDMixin], public_id: str
) -> None:
    """
    Delete an instance completely.

    Warning: only for models without an ``is_active`` field.
    """
    logger.warning("Hard deleting %s public_id=%s", model.__name__, public_id)
    db_instance = (await session.exec(select(model).where(model.public_id == public_id))).first()
    if not db_instance:
        raise HTTP404(detail=f"{model.__name__} not found")
    await session.delete(db_instance)
    await session.commit()


async def get_count(session: AsyncSession, query: Select[Any]) -> int:
    total_query = select(func.count()).select_from(query.subquery())
    return await session.scalar(total_query) or 0
