"""
Alembic environment configuration — VaaniBank AI
PSBs Hackathon 2026 | Team Vectora

Async-aware env.py using asyncpg driver.
All 9 models are imported so Base.metadata is fully populated
before Alembic inspects it for autogenerate / migration execution.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── Project imports ────────────────────────────────────────────────────────────
# database.Base must be imported before models so the declarative registry exists
from database import Base  # noqa: F401

# Import ALL 9 models — this populates Base.metadata
from models import (  # noqa: F401
    AnalyticsDaily,
    AuditLog,
    BilingualSummary,
    Branch,
    Exchange,
    PIILog,
    ProcessStep,
    Session,
    SessionProcessTracking,
    StaffMember,
)

# ── Config from config.py ──────────────────────────────────────────────────────
from config import settings

# ── Alembic Config object ──────────────────────────────────────────────────────
config = context.config

# Override sqlalchemy.url with the value from our settings singleton
# Convert sync psycopg2 URL → asyncpg URL if needed
_async_url = settings.DATABASE_URL.replace(
    "postgresql://", "postgresql+asyncpg://"
).replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://"
)
config.set_main_option("sqlalchemy.url", _async_url)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate support
target_metadata = Base.metadata


# ── Offline migrations (generate SQL scripts without DB connection) ────────────

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL to stdout without a live DB connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online migrations (run against live DB) ───────────────────────────────────

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync wrapper."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No pooling for migration runs
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migration mode."""
    asyncio.run(run_async_migrations())


# ── Entry point dispatch ───────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()