"""
VaaniBank AI — Database & Cache Layer
PSBs Hackathon 2026 | Team Vectora

Provides:
  - Async SQLAlchemy engine + session factory
  - Declarative Base for all ORM models
  - get_db()    → FastAPI dependency for DB sessions
  - redis_client → module-level Redis instance
  - get_redis()  → FastAPI dependency for Redis
  - test_connections() → called on app startup
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from config import settings

logger = logging.getLogger("vaanibank.database")


# SQLAlchemy async engine
# psycopg2 sync URL → asyncpg async URL
_async_db_url: str = settings.DATABASE_URL.replace(
    "postgresql://", "postgresql+asyncpg://"
).replace(
    "postgresql+psycopg2://", "postgresql+asyncpg://"
)

logger.info("DB Engine Init URL: %s", _async_db_url.split("@")[-1] if "@" in _async_db_url else _async_db_url)

engine = create_async_engine(
    _async_db_url,
    echo=settings.is_development,       # SQL logging in dev only
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,                 # Detect stale connections
    pool_recycle=1800,                  # Recycle connections every 30 min
)

# Session factory
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,             # Avoid lazy-load errors after commit
    autocommit=False,
    autoflush=False,
)


# Declarative base
class Base(DeclarativeBase):
    """
    All ORM models inherit from this base.
    Import Base in models.py and alembic env.py.
    """
    pass


# FastAPI DB dependency
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async DB session per request, then close it.

    Usage in router:
        async def endpoint(db: AsyncSession = Depends(get_db)):
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Redis client
redis_client: aioredis.Redis = aioredis.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True,
    health_check_interval=30,
)


# Redis client accessor (non-dependency)
async def get_redis_client() -> aioredis.Redis:
    """
    Return the module-level Redis client directly.

    Use this when you need the Redis client outside of a FastAPI dependency
    injection context (e.g. from ai_service.py singleton).
    """
    return redis_client


# FastAPI Redis dependency
async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """
    Yield the shared Redis client.

    Usage in router:
        async def endpoint(cache: aioredis.Redis = Depends(get_redis)):
    """
    try:
        yield redis_client
    finally:
        pass  # Shared client — do not close per-request


# Startup health checks
async def test_connections() -> None:
    """
    Verify PostgreSQL and Redis connectivity at application startup.
    Raises RuntimeError if either connection fails so Uvicorn exits cleanly.
    """
    # PostgreSQL
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("✅ PostgreSQL connection OK  →  %s", _async_db_url.split("@")[-1])
    except Exception as exc:
        logger.critical("❌ PostgreSQL connection FAILED: %s", exc)
        raise RuntimeError(f"Cannot connect to PostgreSQL: {exc}") from exc

    # Redis
    try:
        pong = await redis_client.ping()
        if not pong:
            raise ConnectionError("PING returned falsy")
        logger.info("✅ Redis connection OK  →  %s", settings.REDIS_URL)
    except Exception as exc:
        logger.critical("❌ Redis connection FAILED: %s", exc)
        raise RuntimeError(f"Cannot connect to Redis: {exc}") from exc


# Graceful shutdown
async def close_connections() -> None:
    """
    Dispose the SQLAlchemy engine and close the Redis pool.
    Called from app lifespan on shutdown.
    """
    await engine.dispose()
    await redis_client.aclose()
    logger.info("Database and Redis connections closed.")