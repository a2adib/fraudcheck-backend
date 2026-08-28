import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette import status
from starlette.middleware.cors import CORSMiddleware

from src.cache.redis_client import async_redis_client
from src.common.exceptions import AuthAPIError
from src.config import app_configs, settings
from src.database import async_engine
from src.logging_config import setup_logging

setup_logging(settings.ENVIRONMENT)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncGenerator[None]:
    if settings.MOCK_MODE:
        # AC-11.5. Loud on purpose: mock mode must never be mistaken for production.
        logger.warning(
            "MOCK_MODE is enabled — all courier adapters are mocked and no real "
            "provider will be contacted."
        )
    yield


app = FastAPI(
    **app_configs,
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=settings.CORS_ORIGINS_REGEX,
    allow_credentials=True,
    allow_methods=("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
    allow_headers=settings.CORS_HEADERS,
)


@app.exception_handler(AuthAPIError)
async def auth_api_error_handler(_request: Request, exc: AuthAPIError) -> JSONResponse:
    """Render auth errors as {"error", "message", **extra} with the right status."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message, **exc.extra},
    )


# ── Routers ───────────────────────────────────────────────────────────────────
# Registered here as each domain lands.


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health() -> JSONResponse:
    """
    Report API, Postgres and Redis status (FR-14.3).

    Each component is reported individually even when one is down, so a 503 still
    tells you *which* dependency failed (AC-14.3).
    """
    components: dict[str, str] = {"api": "up"}

    try:
        # A liveness probe wants a connection, not an ORM session: SQLModel's exec()
        # takes a Select and its execute() warns on every call for preferring exec().
        async with async_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        components["postgres"] = "up"
    except Exception:
        logger.exception("Health check: Postgres unreachable")
        components["postgres"] = "down"

    try:
        await async_redis_client.ping()
        components["redis"] = "up"
    except Exception:
        logger.exception("Health check: Redis unreachable")
        components["redis"] = "down"

    healthy = all(state == "up" for state in components.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if healthy else "degraded",
            "mode": "mock" if settings.MOCK_MODE else "live",
            **components,
        },
    )
