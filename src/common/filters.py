from datetime import date

from pydantic import BaseModel, Field


class PaginationParams(BaseModel):
    """Pagination parameters"""

    page: int = Field(default=1, ge=1, description="Page number")
    size: int = Field(default=20, ge=1, le=100, description="Items per page")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PaginationResponse(PaginationParams):
    """Pagination response"""

    total: int = Field(default=0, ge=0, description="Total number of items")


class DateRangeFilters(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
