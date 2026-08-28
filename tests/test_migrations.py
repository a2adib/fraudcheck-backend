"""
Migration/model drift (no FR — this guards every FR that adds a table).

The test suite builds its schema with ``SQLModel.metadata.create_all`` while
production builds it with Alembic. Nothing keeps those two in step, so a model
change with no accompanying migration passes the whole suite and fails on deploy.
This test closes that gap: it runs the real migration chain against a scratch
database and asserts autogenerate finds nothing left to do.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlmodel import SQLModel

# Import every model module so SQLModel.metadata is complete — mirrors alembic/env.py.
import src.auth.associations
import src.auth.models
import src.securities.models
from alembic import command
from src.config import settings

import src.users.models  # noqa: F401  isort:skip


def _scratch_url() -> str:
    """Build a per-worker scratch database URL on the test database's server."""
    url = make_url(str(settings.DATABASE_URL).replace("postgresql+asyncpg", "postgresql+psycopg2"))
    return url.set(database=f"{url.database}_migrations").render_as_string(hide_password=False)


def _admin_url(url_string: str) -> URL:
    url = make_url(url_string)
    return URL.create(
        drivername="postgresql+psycopg2",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database="postgres",
    )


@pytest.fixture
def scratch_database() -> str:
    """Create an empty database, hand back its URL, and drop it afterwards."""
    scratch_url = _scratch_url()
    database = make_url(scratch_url).database
    admin_engine = create_engine(_admin_url(scratch_url), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{database}"'))
        yield scratch_url
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()


def test_migrations_match_the_models(scratch_database: str, monkeypatch: pytest.MonkeyPatch):
    """`alembic upgrade head` must produce exactly what the models declare."""
    # alembic/env.py reads settings.DATABASE_URL when Alembic execs it, so pointing
    # settings at the scratch database is enough to redirect the whole chain.
    monkeypatch.setattr(settings, "DATABASE_URL", type(settings.DATABASE_URL)(scratch_database))

    command.upgrade(Config("alembic.ini"), "head")

    engine = create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diff = compare_metadata(context, SQLModel.metadata)
    finally:
        engine.dispose()

    assert diff == [], (
        'Models and migrations have diverged. Run `just mm "<description>"` and '
        f"commit the generated migration. Outstanding changes: {diff}"
    )
