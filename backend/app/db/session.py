"""Async engine / session factory with pool configuration per dialect."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

_engine_kwargs: dict = {"echo": settings.db_echo, "future": True, "pool_pre_ping": True}
if settings.is_sqlite:
    _engine_kwargs["poolclass"] = NullPool
    _engine_kwargs.pop("pool_pre_ping")
    # SQLite serialises writers; WAL plus a busy timeout lets the API and the detached
    # execution tasks write concurrently instead of failing with "database is locked".
    _engine_kwargs["connect_args"] = {"timeout": 30}
else:
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
    )

engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs)

if settings.is_sqlite:
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# Ambient session for background writers (cost ledger, tool health) so telemetry joins the
# caller's transaction instead of opening a second connection and contending for the write
# lock. Set by the execution engine for the duration of a run.
ambient_session: ContextVar[AsyncSession | None] = ContextVar("ambient_session", default=None)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager for background workers and the execution engine."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping_database() -> bool:
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose_engine() -> None:
    await engine.dispose()
