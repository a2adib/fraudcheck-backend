"""
Pytest configuration and shared fixtures.

Integration tests run against a real Postgres (Docker) — no DB mocking.

Isolation strategy, carried over from ``erp-backend/tests/conftest.py``:
- Session-scoped: create the test database if missing, create all tables once.
- Function-scoped: every test runs inside a transaction with
  ``join_transaction_mode="create_savepoint"``, so a ``session.commit()`` in
  application code becomes a SAVEPOINT and the whole thing is rolled back after.
- ``get_session`` is overridden to inject the test session.

Under ``pytest-xdist`` each worker gets its own database (``fraudcheck_test_gw0``, …)
because each runs the session-scoped ``create_all``.
"""

import os
from collections.abc import AsyncGenerator, Generator

import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

# Import every model module so SQLModel.metadata is populated before create_all.
import src.auth.associations
import src.auth.models
import src.cache.redis_client
import src.checks.models
import src.credentials.models
import src.securities.models
import src.users.models
from src.auth.schemas import RegisterRequest
from src.config import settings
from src.database import get_session
from src.main import app
from src.users.models import User


def _worker_database_url(base_url: str) -> str:
    """
    Give each pytest-xdist worker its own database.

    Workers share one Postgres server but must not share tables, since each runs the
    session-scoped create_all. Without xdist the base database name is used unchanged.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return base_url
    url = make_url(base_url)
    return url.set(database=f"{url.database}_{worker}").render_as_string(hide_password=False)


ASYNC_DATABASE_URL = _worker_database_url(str(settings.DATABASE_URL))
SYNC_DATABASE_URL = ASYNC_DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")


def _ensure_test_database_exists() -> None:
    """Create the test database if it does not already exist."""
    from sqlalchemy.engine import URL

    url = make_url(SYNC_DATABASE_URL)
    admin_url = URL.create(
        drivername="postgresql+psycopg2",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database="postgres",
    )
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", echo=False)
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"),
                {"db": url.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def cheap_password_hashing() -> None:
    """
    Downgrade Argon2id to its cheapest legal parameters for the whole session.

    Production cost is ~100ms per hash; M1 alone will hash hundreds of times. The
    scheme is unchanged, so verification behaviour is still exercised.
    """
    from src.auth.utils import pwd_context

    pwd_context.update(argon2__time_cost=1, argon2__memory_cost=8, argon2__parallelism=1)


@pytest.fixture
async def permission_catalogue(async_session: AsyncSession) -> list[str]:
    """
    Mint the FR-1.11 catalogue and the owner role inside the test's transaction.

    Function-scoped on purpose: the savepoint rolls it back, so a test that never asks
    for permissions still sees an empty catalogue.
    """
    from src.auth.services import AuthorizationService

    permissions = await AuthorizationService(async_session).sync_permission_catalogue()
    return [permission.code for permission in permissions]


@pytest.fixture(scope="session")
def test_engine() -> Generator[Engine]:
    _ensure_test_database_exists()
    engine = create_engine(SYNC_DATABASE_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def async_test_engine() -> AsyncEngine:
    # Deliberately a sync fixture so it lives at session scope without binding to one
    # event loop. NullPool means connections are created lazily inside each test's own
    # loop, keeping the engine object loop-agnostic.
    return create_async_engine(ASYNC_DATABASE_URL, echo=False, poolclass=NullPool)


@pytest.fixture(scope="session", autouse=True)
def create_tables(test_engine: Engine) -> None:
    """Drop and recreate every table once per test session."""
    SQLModel.metadata.drop_all(test_engine)
    SQLModel.metadata.create_all(test_engine)


@pytest.fixture
async def async_session(async_test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    async with async_test_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()


@pytest.fixture
async def test_app(async_session: AsyncSession) -> AsyncGenerator[FastAPI]:
    """The FastAPI app with ``get_session`` overridden to the test session."""

    async def override_get_session() -> AsyncGenerator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as async_client:
        yield async_client


# ── Redis ─────────────────────────────────────────────────────────────────────


async def _flush_prefix(client: aioredis.Redis) -> None:
    keys = await client.keys(f"{settings.REDIS_KEY_PREFIX}*")
    if keys:
        await client.delete(*keys)


@pytest.fixture(autouse=True)
async def redis_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[aioredis.Redis]:
    """
    Bind a Redis client to *this* test's event loop and key namespace.

    Two problems get solved here. The client is per test because a redis-py async client
    caches connections against the loop that opened them, and pytest-asyncio gives every
    test a fresh loop. The key prefix is per xdist worker because the workers share one
    Redis server, and a ``flushdb`` in one would delete another's tokens mid-test.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    monkeypatch.setattr(settings, "REDIS_KEY_PREFIX", f"test:{worker}:")

    client = aioredis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT, decode_responses=True
    )
    monkeypatch.setattr(src.cache.redis_client, "async_redis_client", client)

    await _flush_prefix(client)
    try:
        yield client
    finally:
        await _flush_prefix(client)
        await client.aclose()


# ── Merchants ─────────────────────────────────────────────────────────────────

MERCHANT_EMAIL = "merchant@example.com"
OTHER_MERCHANT_EMAIL = "other@example.com"
MERCHANT_PASSWORD = "correct-horse-battery"


async def register_merchant(
    session: AsyncSession,
    email: str = MERCHANT_EMAIL,
    password: str = MERCHANT_PASSWORD,
) -> User:
    """
    Register through the real service, so the merchant holds the Owner role.

    Building a ``User`` row by hand would skip ``grant_default_role`` and every
    permission-guarded route would then 403 for reasons that have nothing to do with
    the behaviour under test.
    """
    from src.auth.services import AuthenticationService

    return await AuthenticationService(session).register(
        RegisterRequest(email=email, password=password, full_name="Test Merchant")
    )


@pytest.fixture
async def merchant(async_session: AsyncSession, permission_catalogue: list[str]) -> User:
    return await register_merchant(async_session)


@pytest.fixture
async def other_merchant(async_session: AsyncSession, permission_catalogue: list[str]) -> User:
    """Register a second tenant, for the isolation suite."""
    return await register_merchant(async_session, email=OTHER_MERCHANT_EMAIL)


async def bearer_headers(
    client: AsyncClient,
    email: str = MERCHANT_EMAIL,
    password: str = MERCHANT_PASSWORD,
) -> dict[str, str]:
    response = await client.post("/auth/login", json={"identifier": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
async def auth_client(client: AsyncClient, merchant: User) -> AsyncClient:
    """Authenticate the shared client as ``merchant``."""
    client.headers.update(await bearer_headers(client))
    return client
