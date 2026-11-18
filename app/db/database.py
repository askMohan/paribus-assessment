"""Application-owned SQLAlchemy engine and isolated async ORM sessions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

from sqlalchemy import URL, Connection, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


def _configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Configure each pooled connection before ORM operations can use it."""
    # Let the begin hook own transactions; foreign keys are per connection.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def _begin_transaction(connection: Connection) -> None:
    """Acquire write ownership before read-modify-write claims can race."""
    write = connection.get_execution_options().get("sqlite_write", False)
    connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")


class Database:
    """Own the engine while giving each operation a fresh AsyncSession."""

    def __init__(self, path: Path) -> None:
        """Prepare lifecycle state without opening SQLite or creating a file."""
        self.path = path
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        """Initialize missing ORM tables without replacing existing saved data."""
        if self._engine is not None:
            raise RuntimeError("Database is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.path.resolve())),
            connect_args={"timeout": 5},
            pool_size=1,
            max_overflow=0,
        )
        event.listen(engine.sync_engine, "connect", _configure_sqlite)
        event.listen(engine.sync_engine, "begin", _begin_transaction)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        except BaseException:
            await engine.dispose()
            raise
        self._engine = engine
        self._sessions = async_sessionmaker(
            engine, expire_on_commit=False, autoflush=False
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Wait for active work and dispose connections without suppressing errors."""
        async with self._lock:
            if self._engine is not None:
                await self._engine.dispose()
                self._sessions = None
                self._engine = None

    @asynccontextmanager
    async def transaction(self, *, write: bool = False) -> AsyncIterator[AsyncSession]:
        """Commit or roll back a fresh session; never hold it across network calls."""
        async with self._lock:
            if self._sessions is None:
                raise RuntimeError("Database is not open")
            async with self._sessions() as session:
                try:
                    await session.connection(execution_options={"sqlite_write": write})
                    yield session
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
