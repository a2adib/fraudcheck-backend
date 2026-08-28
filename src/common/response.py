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
    data: list[dict[str, Any]] | dict[str, Any] | list[BaseModel] | BaseModel | None = None,
    message: str = MESSAGE_200,
    pagination: PaginationResponse | None = None,
    meta: dict[str, Any] | None = None,
) -> StandardResponse[Any]:
    """Create a standardized response"""
    return StandardResponse(detail=message, meta=meta, data=data, pagination=pagination)
