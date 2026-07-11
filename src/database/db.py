"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models.schemas import Base

settings = get_settings()

# Ensure data directory exists
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_async_engine(
    settings.database_url,
    echo=settings.env == "development",
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables and apply lightweight column migrations (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_add_columns(conn)


async def _migrate_add_columns(conn) -> None:
    """Add columns that were introduced after the initial schema was deployed.

    Uses ADD COLUMN IF NOT EXISTS on Postgres and a try/except on SQLite
    (SQLite doesn't support IF NOT EXISTS for ALTER TABLE).
    Safe to run on every startup — existing columns are silently skipped.
    """
    from sqlalchemy import text

    migrations = [
        # (table, column, definition)
        ("workflow_runs", "max_pages", "INTEGER"),
        ("workflow_runs", "celery_task_id", "VARCHAR(255)"),
    ]

    is_sqlite = settings.database_url.startswith("sqlite")

    for table, column, col_def in migrations:
        if is_sqlite:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"))
            except Exception:
                pass  # column already exists
        else:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_def}")
            )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
