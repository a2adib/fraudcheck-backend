from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context

# Import every model module so SQLModel.metadata is fully populated before
# autogenerate runs. Add new domains here.
from src.auth.associations import RolePermissionLink, UserRoleLink  # noqa: F401
from src.auth.models import (  # noqa: F401
    Otp,
    PasswordResetToken,
    Permission,
    RetiredRefreshToken,
    Role,
    UserSession,
)
from src.checks.models import CheckRequest, ProviderResult  # noqa: F401
from src.config import settings
from src.credentials.models import CourierCredential  # noqa: F401
from src.securities.models import ActivityLog  # noqa: F401
from src.users.models import User  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

# Alembic reads DATABASE_URL from settings at runtime; the value in alembic.ini is
# a placeholder and is ignored. The async driver is swapped for its sync equivalent
# because Alembic runs synchronously.
DATABASE_URL = str(settings.DATABASE_URL)
db_driver = settings.DATABASE_URL.scheme
db_driver_parts = db_driver.split("+")
if len(db_driver_parts) > 1:
    sync_scheme = db_driver_parts[0].strip()
    DATABASE_URL = DATABASE_URL.replace(db_driver, sync_scheme)

config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL without a DBAPI connection."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
