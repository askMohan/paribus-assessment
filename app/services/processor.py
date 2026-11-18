"""Coordinate saved batch work through the repository and Directory interface."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from app.adapters.hospital_directory import HospitalDirectoryAdapter
from app.db.repository import BatchRepository
from app.domain import (
    Batch,
    BatchState,
    DirectoryOperationError,
    FailureKind,
    HospitalRow,
    can_create_hospital,
)
from app.services.reconciliation import BatchReconciler

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return an aware UTC clock value for persisted attempt boundaries."""
    return datetime.now(UTC)


class BatchProcessor:
    """Run one attempt without retaining ORM sessions across external requests."""

    def __init__(
        self,
        repository: BatchRepository,
        directory: HospitalDirectoryAdapter,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Inject application-lifetime services, not request-owned sessions."""
        self._repository = repository
        self._directory = directory
        self._clock = clock
        self._reconciler = BatchReconciler(repository, directory)

    async def process_batch(
        self, batch_id: UUID, *, reconcile_before_processing: bool = False
    ) -> None:
        """Claim queued work, settle row outcomes, and conditionally activate it."""
        claimed = False
        # Resolve the database claim even if cancellation arrives during commit.
        # The persisted state is the sole execution guard, shared by all callers.
        claim = asyncio.create_task(
            self._repository.start_attempt(batch_id, started_at=self._clock())
        )
        try:
            claimed = await asyncio.shield(claim)
            if not claimed:
                return
            logger.info("Batch attempt started batch_id=%s", batch_id)
            batch = await self._repository.get_batch(batch_id)
            if batch is None:
                raise RuntimeError("Claimed batch is missing")
            # Recheck the claimed snapshot: a route's earlier recovery hint can
            # be stale after another attempt changes the saved outcomes.
            if reconcile_before_processing or batch.needs_reconciliation:
                if not await self._reconcile(batch_id):
                    return
                batch = await self._repository.get_batch(batch_id)
            await self._create_rows(batch)
            batch = await self._repository.get_batch(batch_id)
            if batch.has_unknown_creations:
                if not await self._reconcile(batch_id):
                    return
            if await self._settle_batch(batch_id):
                return
            await self._activate(batch_id)
        except (Exception, asyncio.CancelledError) as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            if cancelled:
                claimed = await claim
            logger.log(
                logging.WARNING if cancelled else logging.ERROR,
                "Batch attempt stopped batch_id=%s error_type=%s",
                batch_id,
                type(error).__name__,
                exc_info=not cancelled,
            )
            if claimed:
                await self._settle_batch(
                    batch_id,
                    error=f"Batch attempt stopped: {type(error).__name__}",
                    interrupted=cancelled,
                )
            raise

    async def _reconcile(self, batch_id: UUID) -> bool:
        """Return permission to continue, or stop after unresolved/completed evidence."""
        result = await self._reconciler.reconcile(batch_id)
        if not result.resolved:
            await self._repository.finish_attempt(
                batch_id,
                BatchState.RECONCILIATION_REQUIRED,
                error=result.error,
                ended_at=self._clock(),
            )
            return False
        if result.all_active:
            batch = await self._repository.get_batch(batch_id)
            if batch.state == BatchState.PROCESSING:
                if not await self._repository.begin_activation(batch_id):
                    raise RuntimeError("Reconciled batch could not enter completion")
            if not await self._repository.complete_activation(
                batch_id, ended_at=self._clock()
            ):
                raise RuntimeError("Reconciled activation could not be checkpointed")
            logger.info("Activation confirmed by lookup batch_id=%s", batch_id)
            return False
        return True

    async def _create_rows(self, batch: Batch) -> None:
        """Create eligible rows concurrently and let every in-flight call settle."""
        tasks = [
            asyncio.create_task(self._create_row(batch.batch_id, row))
            for row in batch.rows
            if can_create_hospital(row.state, row.failure_kind)
        ]
        try:
            # One failed row must not discard its peers' possibly committed writes.
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _create_row(self, batch_id: UUID, row: HospitalRow) -> None:
        """Save intent before one adapter call and immediately checkpoint its result."""
        try:
            if not await self._repository.start_creation(batch_id, row.row_number):
                return
            logger.info("Creation started batch_id=%s row=%s", batch_id, row.row_number)
            try:
                hospital = await self._directory.create_hospital(row.hospital, batch_id)
            except DirectoryOperationError as error:
                recorded = await self._repository.record_row_outcome(
                    batch_id,
                    row.row_number,
                    failure_kind=error.failure_kind,
                    error=str(error),
                )
                logger.warning(
                    "Creation unsuccessful batch_id=%s row=%s failure_kind=%s",
                    batch_id,
                    row.row_number,
                    error.failure_kind.value,
                )
            else:
                recorded = await self._repository.record_row_outcome(
                    batch_id, row.row_number, hospital_id=hospital.hospital_id
                )
            if not recorded:
                raise RuntimeError("Creation outcome was not checkpointed")
        except Exception:
            logger.exception(
                "Creation task failed batch_id=%s row=%s", batch_id, row.row_number
            )
            raise

    async def _settle_batch(
        self,
        batch_id: UUID,
        *,
        error: str | None = None,
        interrupted: bool = False,
    ) -> bool:
        """Convert abandoned intent to uncertainty and finalize unsuccessful work."""
        batch = await self._repository.mark_unconfirmed(
            batch_id, error or "Creation ended without a saved outcome"
        )
        if batch is None or batch.state not in (
            BatchState.PROCESSING,
            BatchState.ACTIVATING,
        ):
            return True
        if (
            batch.creation_complete
            and error is None
            and batch.state == BatchState.PROCESSING
        ):
            return False
        uncertain = batch.state == BatchState.ACTIVATING or batch.has_unknown_creations
        if uncertain:
            state = BatchState.RECONCILIATION_REQUIRED
        elif interrupted:
            state = BatchState.INTERRUPTED
        else:
            state = BatchState.FAILED
        await self._repository.finish_attempt(
            batch_id,
            state,
            error=error or "Creation incomplete",
            ended_at=self._clock(),
        )
        logger.info("Batch attempt ended batch_id=%s state=%s", batch_id, state.value)
        return True

    async def _activate(self, batch_id: UUID) -> None:
        """Cross the saved manifest barrier, request activation, and commit completion."""
        if not await self._repository.begin_activation(batch_id):
            raise RuntimeError("Complete batch could not enter activation")
        logger.info("Activation started batch_id=%s", batch_id)
        try:
            await self._directory.activate_batch(batch_id)
        except DirectoryOperationError as error:
            if error.failure_kind == FailureKind.UNCERTAIN:
                if not await self._reconcile(batch_id):
                    return
                # One activation mutation per attempt. Conclusive inactive
                # evidence permits a later manual attempt, not a retry loop.
                state = BatchState.RECONCILIATION_REQUIRED
            else:
                state = BatchState.FAILED
            await self._repository.finish_attempt(
                batch_id, state, error=str(error), ended_at=self._clock()
            )
            logger.warning(
                "Activation unsuccessful batch_id=%s failure_kind=%s",
                batch_id,
                error.failure_kind.value,
            )
            return
        if not await self._repository.complete_activation(
            batch_id, ended_at=self._clock()
        ):
            raise RuntimeError("Activation acknowledgement could not be checkpointed")
        logger.info("Batch completed batch_id=%s", batch_id)
