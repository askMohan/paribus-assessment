"""Shared test resources; all persistence uses temporary files."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from app.db.database import Database
from app.db.repository import BatchRepository


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> AsyncIterator[BatchRepository]:
    """Keep one isolated database open for the duration of a test."""
    async with Database(tmp_path / "data" / "hospitals.db") as database:
        yield BatchRepository(database)
