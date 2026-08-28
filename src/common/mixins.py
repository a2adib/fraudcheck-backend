from datetime import datetime

from sqlalchemy import func
from sqlmodel import Field, SQLModel

from src.common.types import TZDateTime
from src.common.utils import generate_public_id


class TimestampMixin(SQLModel):
    created_at: datetime = Field(
        sa_type=TZDateTime,
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        sa_type=TZDateTime,
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
        nullable=False,
    )


class IDMixin(SQLModel):
    id: int | None = Field(default=None, primary_key=True)
    public_id: str = Field(default_factory=generate_public_id, unique=True, max_length=11)


class CommonFieldMixin(IDMixin, TimestampMixin):
    is_active: bool = True


def require_id(instance: IDMixin) -> int:
    """
    Narrow a persisted row's ``id`` from ``int | None`` to ``int``.

    ``id`` is optional on the model because it is unset until the row is flushed;
    every caller here holds a committed row, and this makes that explicit instead of
    spreading ``# type: ignore`` over each foreign-key assignment.
    """
    if instance.id is None:
        msg = f"{type(instance).__name__} has no id — it was never committed."
        raise RuntimeError(msg)
    return instance.id
