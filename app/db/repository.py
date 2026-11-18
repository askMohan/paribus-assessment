"""Reusable ORM operations for the batch workflow, without raw data queries."""

import logging
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.database import Database
from app.db.models import BatchModel, HospitalRowModel, utc_datetime
from app.domain import (
    RESUMABLE_BATCH_STATES,
    Batch,
    BatchState,
    FailureKind,
    HospitalRow,
    RowState,
    can_create_hospital,
)

logger = logging.getLogger(__name__)

# Paused work releases a slot; resume must acquire capacity again atomically.
_ACTIVE_STATES = (BatchState.QUEUED, BatchState.PROCESSING, BatchState.ACTIVATING)


class BatchCapacityError(Exception):
    """Already scheduled/running batches occupy the configured admission budget."""


def _activation_ready(batch: BatchModel) -> bool:
    """Reuse the manifest barrier before entering and completing activation."""
    return (
        batch.attempt_started_at is not None
        and len(batch.rows) == batch.total_hospitals
        and all(
            row.state == RowState.CREATED and row.hospital_id is not None
            for row in batch.rows
        )
    )


def _end_attempt(
    batch: BatchModel,
    state: BatchState,
    ended_at: datetime,
    error: str | None,
) -> None:
    """Finalize timing and state once for success and unsuccessful attempts alike."""
    if batch.attempt_started_at is not None:
        batch.processing_time_seconds += max(
            0.0, (ended_at - batch.attempt_started_at).total_seconds()
        )
    batch.attempt_started_at = None
    batch.state = state
    batch.last_error = error


def _mark_creating_unknown(batch: BatchModel, error: str) -> None:
    """Preserve confirmed outcomes while abandoning unconfirmed creation intent."""
    for row in batch.rows:
        if row.state == RowState.CREATING:
            row.state = RowState.UNKNOWN
            row.failure_kind = FailureKind.UNCERTAIN
            row.last_error = error
            logger.warning(
                "Creation unconfirmed batch_id=%s row=%s",
                batch.batch_id,
                row.row_number,
            )


class BatchRepository:
    """Persist explicit business operations through shared ORM guards/helpers."""

    def __init__(self, database: Database) -> None:
        """Bind sessions without taking ownership of the application's lifecycle."""
        self.database = database

    @asynccontextmanager
    async def _batch(
        self,
        batch_id: UUID,
        *,
        write: bool = False,
        states: Collection[BatchState] | None = None,
    ) -> AsyncIterator[tuple[AsyncSession, BatchModel | None]]:
        """Reuse transaction setup, eager aggregate loading, and state filtering."""
        async with self.database.transaction(write=write) as session:
            batch = await session.get(
                BatchModel, str(batch_id), options=[selectinload(BatchModel.rows)]
            )
            if batch is not None and states is not None and batch.state not in states:
                batch = None
            yield session, batch

    async def _in_flight_count(
        self,
        session: AsyncSession,
        limit: int | None = None,
    ) -> int:
        """Share the admission query across count, registration, and resume."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("max_in_flight_batches must be a positive integer")
        count = await session.scalar(
            select(func.count())
            .select_from(BatchModel)
            .where(BatchModel.state.in_(_ACTIVE_STATES))
        )
        if limit is not None and count >= limit:
            raise BatchCapacityError("Maximum number of in-flight batches reached")
        return count

    async def register_batch(
        self,
        batch_id: UUID,
        rows: Sequence[HospitalRow],
        *,
        max_in_flight_batches: int | None = None,
    ) -> None:
        """Commit one validated batch and its full pending manifest together."""
        if not 1 <= len(rows) <= 20:
            raise ValueError("A batch must contain 1-20 rows")
        if sorted(row.row_number for row in rows) != list(range(1, len(rows) + 1)):
            raise ValueError("Input row numbers must be unique and contiguous from 1")
        if any(
            row.state != RowState.PENDING
            or row.hospital_id is not None
            or row.failure_kind is not None
            or row.last_error is not None
            for row in rows
        ):
            raise ValueError("New batch rows must be pending without previous outcomes")
        async with self.database.transaction(write=True) as session:
            await self._in_flight_count(session, max_in_flight_batches)
            session.add(
                BatchModel(
                    batch_id=str(batch_id),
                    total_hospitals=len(rows),
                    rows=[
                        HospitalRowModel(
                            row_number=row.row_number,
                            name=row.hospital.name,
                            address=row.hospital.address,
                            phone=row.hospital.phone,
                        )
                        for row in rows
                    ],
                )
            )

    async def get_batch(self, batch_id: UUID) -> Batch | None:
        """Read a consistent snapshot without leaking live ORM objects."""
        async with self._batch(batch_id) as (_, batch):
            return batch.to_domain() if batch is not None else None

    async def mark_unconfirmed(self, batch_id: UUID, error: str) -> Batch | None:
        """Checkpoint abandoned intent and return its snapshot in one transaction."""
        async with self._batch(batch_id, write=True) as (_, batch):
            if batch is not None:
                _mark_creating_unknown(batch, error)
                return batch.to_domain()
            return None

    async def count_in_flight_batches(self) -> int:
        """Count scheduled/running work without counting paused failures."""
        async with self.database.transaction() as session:
            return await self._in_flight_count(session)

    async def recover_interrupted_batches(self) -> int:
        """Pause abandoned work on startup without replaying remote operations."""
        async with self.database.transaction(write=True) as session:
            batches = list(
                await session.scalars(
                    select(BatchModel)
                    .where(BatchModel.state.in_(_ACTIVE_STATES))
                    .options(selectinload(BatchModel.rows))
                )
            )
            for batch in batches:
                batch.state = BatchState.INTERRUPTED
                batch.last_error = "Previous application stopped before completion"
                # The actual stop time is unknown. Keep checkpointed duration and
                # discard the uncheckpointed interval rather than count downtime.
                batch.attempt_started_at = None
                _mark_creating_unknown(
                    batch, "Creation was interrupted without a saved outcome"
                )
            return len(batches)

    async def start_attempt(
        self,
        batch_id: UUID,
        *,
        started_at: datetime | None = None,
    ) -> bool:
        """Claim queued work once and start its active-duration marker."""
        started = utc_datetime(started_at)
        async with self._batch(batch_id, write=True, states=(BatchState.QUEUED,)) as (
            _,
            batch,
        ):
            if batch is None or batch.attempt_started_at is not None:
                return False
            batch.state = BatchState.PROCESSING
            batch.attempt_started_at = started
            batch.last_error = None
            return True

    async def claim_for_resume(
        self,
        batch_id: UUID,
        *,
        max_in_flight_batches: int | None = None,
    ) -> bool:
        """Reserve paused work; the resume service checks row-level eligibility."""
        async with self._batch(batch_id, write=True, states=RESUMABLE_BATCH_STATES) as (
            session,
            batch,
        ):
            if (
                batch is None
                or batch.batch_activated
                or batch.attempt_started_at is not None
                or not batch.to_domain().can_resume
            ):
                return False
            await self._in_flight_count(session, max_in_flight_batches)
            batch.state, batch.last_error = BatchState.QUEUED, None
            return True

    async def start_creation(self, batch_id: UUID, row_number: int) -> bool:
        """Persist intent only for pending or established-safe retryable input."""
        async with self._batch(
            batch_id, write=True, states=(BatchState.PROCESSING,)
        ) as (_, batch):
            row = batch.row_at(row_number) if batch is not None else None
            if row is None or batch.attempt_started_at is None:
                return False
            if not can_create_hospital(row.state, row.failure_kind):
                return False
            row.state, row.failure_kind, row.last_error = RowState.CREATING, None, None
            return True

    async def record_row_outcome(
        self,
        batch_id: UUID,
        row_number: int,
        *,
        hospital_id: int | None = None,
        failure_kind: FailureKind | None = None,
        error: str | None = None,
    ) -> bool:
        """Record success, failure, or uncertainty through one guarded ORM path."""
        if hospital_id is not None:
            if isinstance(hospital_id, bool) or not isinstance(hospital_id, int):
                raise ValueError("hospital_id must be an integer")
            if failure_kind is not None or error is not None:
                raise ValueError(
                    "A successful outcome cannot also contain failure information"
                )
            target = RowState.CREATED
        else:
            if not isinstance(failure_kind, FailureKind) or error is None:
                raise ValueError("An unsuccessful outcome needs failure_kind and error")
            target = (
                RowState.UNKNOWN
                if failure_kind == FailureKind.UNCERTAIN
                else RowState.FAILED
            )
        async with self._batch(batch_id, write=True) as (_, batch):
            row = batch.row_at(row_number) if batch is not None else None
            if row is None:
                return False
            if row.state == RowState.CREATED and target == RowState.CREATED:
                return row.hospital_id == hospital_id
            if row.state not in (RowState.CREATING, RowState.UNKNOWN):
                return False
            if row.state == RowState.UNKNOWN and target == RowState.FAILED:
                return False  # Lack of evidence cannot turn uncertainty into definite failure.
            row.state, row.failure_kind, row.last_error = target, failure_kind, error
            if hospital_id is not None:
                row.hospital_id = hospital_id
            return True

    async def begin_activation(self, batch_id: UUID) -> bool:
        """Reuse the complete-manifest barrier before entering activation."""
        async with self._batch(
            batch_id, write=True, states=(BatchState.PROCESSING,)
        ) as (_, batch):
            if batch is None or not _activation_ready(batch):
                return False
            batch.state = BatchState.ACTIVATING
            return True

    async def complete_activation(
        self,
        batch_id: UUID,
        *,
        ended_at: datetime | None = None,
    ) -> bool:
        """Commit confirmed row/batch activation and duration in one transaction."""
        ended = utc_datetime(ended_at)
        async with self._batch(
            batch_id, write=True, states=(BatchState.ACTIVATING,)
        ) as (_, batch):
            if batch is None or not _activation_ready(batch):
                return False
            for row in batch.rows:
                row.state = RowState.ACTIVATED
            _end_attempt(batch, BatchState.COMPLETED, ended, None)
            batch.batch_activated = True
            return True

    async def finish_attempt(
        self,
        batch_id: UUID,
        state: BatchState,
        *,
        error: str | None = None,
        ended_at: datetime | None = None,
    ) -> bool:
        """Stop an unsuccessful attempt while retaining all confirmed hospital IDs."""
        if state not in RESUMABLE_BATCH_STATES:
            raise ValueError(
                "finish_attempt requires an unsuccessful terminal attempt state"
            )
        ended = utc_datetime(ended_at)
        async with self._batch(
            batch_id, write=True, states=(BatchState.PROCESSING, BatchState.ACTIVATING)
        ) as (_, batch):
            if batch is None:
                return False
            _end_attempt(batch, state, ended, error)
            return True
