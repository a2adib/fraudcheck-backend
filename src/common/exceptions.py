from typing import Any

from fastapi import HTTPException, status


class HTTP400(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 400 exception"""
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


class HTTP401(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 401 exception"""
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class HTTP403(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 403 exception"""
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class HTTP404(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 404 exception"""
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class HTTP409(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 409 exception"""
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class HTTP410(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 410 exception"""
        super().__init__(status_code=status.HTTP_410_GONE, detail=detail)


class HTTP422(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 422 exception"""
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


class HTTP423(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 423 exception"""
        super().__init__(status_code=status.HTTP_423_LOCKED, detail=detail)


class HTTP429(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 429 exception"""
        super().__init__(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


class HTTP500(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 500 exception"""
        super().__init__(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


class HTTP501(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 501 exception"""
        super().__init__(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=detail)


class HTTP502(HTTPException):
    def __init__(self, detail: str) -> None:
        """Raise HTTP 502 exception"""
        super().__init__(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


class AuthAPIError(Exception):
    """
    Structured auth error rendered as ``{"error", "message", **extra}``.

    Used only by the auth endpoints so the frontend gets machine-readable error
    codes alongside human-readable messages, plus extra fields like
    ``retry_after_seconds`` or ``locked_until``. Other modules keep FastAPI's
    default ``{"detail": ...}`` shape via the ``HTTP4xx`` helpers.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        detail: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Build a structured auth error with a status, code, message and extras."""
        self.status_code = status_code
        self.error_code = error_code
        self.message = detail
        self.extra = extra or {}
        super().__init__(detail)
