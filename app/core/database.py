"""Async database connection and session management.

Uses SQLAlchemy 2.0 async engine with asyncpg driver.
Provides base declarative model and session factory.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.common.ids import uuid7
from app.core.config import settings

logger = logging.getLogger("autofix.database")

# Async engine - echo only in development
_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_DEBUG,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
)

# Session factory
AsyncSessionFactory = async_sessionmaker[
    AsyncSession
](
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    """Base declarative model."""

    __abstract__ = True


class TimestampedBase(Base):
    """Base model with UUID primary key and timestamps."""

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
        onupdate=text("now()"),
    )

    @staticmethod
    async def update_timestamp(
        session: AsyncSession, model: type[Base], model_id: Any
    ) -> None:
        """Update the updated_at timestamp for a record."""
        await session.execute(
            text(
                f"UPDATE {model.__tablename__} SET updated_at = now() WHERE id = :id"
            ),
            {"id": model_id},
        )


async def init_db() -> None:
    """Initialize database connection (verify connectivity on startup)."""
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection established")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        raise


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    async with AsyncSessionFactory() as session:
        yield session


async def close_engine() -> None:
    """Close database connections on shutdown."""
    await _engine.dispose()
    logger.info("Database connections closed")
