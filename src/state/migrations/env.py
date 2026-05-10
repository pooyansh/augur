"""Alembic migration environment — async variant."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Alembic config object
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Build the database URL from environment variables.
# POSTGRES_PASSWORD must come from the decrypted secrets mount — never from .env.
# ---------------------------------------------------------------------------
_host = os.environ.get("POSTGRES_HOST", "localhost")
_port = os.environ.get("POSTGRES_PORT", "5432")
_db = os.environ.get("POSTGRES_DB", "bidder")
_user = os.environ.get("POSTGRES_USER", "bidder")
_password = os.environ.get("POSTGRES_PASSWORD", "")

_db_url = f"postgresql+asyncpg://{_user}:{_password}@{_host}:{_port}/{_db}"
config.set_main_option("sqlalchemy.url", _db_url)

# ---------------------------------------------------------------------------
# Import ORM metadata for autogenerate support.
# ---------------------------------------------------------------------------
from src.state.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection required).

    Useful for generating SQL scripts to inspect or run manually.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migrations against an open connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations online."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
