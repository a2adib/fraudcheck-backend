from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from src.common.filters import PaginationResponse

T = TypeVar("T")

MESSAGE_200 = "Returned successfully"
MESSAGE_201 = "Created successfully"


class StandardResponse(BaseModel, Generic[T]):
    meta: dict[str, Any] | None = None
    pagination: PaginationResponse | None = None
    detail: str
    data: T


def create_response(
    # Sequence, not list: list is invariant, so a list[RoleOut] would not satisfy
    # list[BaseModel] and every caller with a concrete model list would need a cast.
    data: Sequence[dict[str, Any]] | dict[str, Any] | Sequence[BaseModel] | BaseModel | None = None,
    message: str = MESSAGE_200,
    pagination: PaginationResponse | None = None,
    meta: dict[str, Any] | None = None,
) -> StandardResponse[Any]:
    """Create a standardized response"""
    return StandardResponse(detail=message, meta=meta, data=data, pagination=pagination)
