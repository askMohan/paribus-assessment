"""Batch orchestration against real temporary SQLite and a typed fake Directory."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.api.schemas import BatchResult
from app.db.repository import BatchRepository
from app.domain import (
    BatchState,
    DirectoryHospital,
    DirectoryOperation,
    DirectoryOperationError,
    FailureEvidence,
    FailureKind,
    HospitalInput,
    RowState,
)
from app.services.csv_validation import validate_csv_bytes
from app.services.processor import BatchProcessor

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
START = datetime(2026, 11, 18, 10, tzinfo=UTC)
SAMPLE = (
    b"name,address,phone\nGeneral Hospital,123 Main Street,555-0101\n"
    b"City Care Hospital,45 Park Road,555-0102\nSunrise Hospital,8 Lake Road,555-0103\n"
)
ROWS = tuple(validate_csv_bytes(SAMPLE, max_upload_bytes=1024))


class FakeDirectory:
    """Supply controlled domain outcomes without HTTP imports or live mutations."""

    def __init__(self) -> None:
        """Record calls and expose optional gates/failures for individual scenarios."""
        self.creations: list[tuple[int, UUID]] = []
        self.activations: list[UUID] = []
        self.errors: dict[int, BaseException] = {}
        self.activation_error: BaseException | None = None
        self.on_create: Callable[[int], Awaitable[None]] | None = None
        self.on_activate: Callable[[], Awaitable[None]] | None = None
        self.evidence: list[DirectoryHospital] = []

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Return an inactive identity for the submitted input, or simulate failure."""
        number = next(row.row_number for row in ROWS if row.hospital == hospital)
        self.creations.append((number, batch_id))
        if self.on_create is not None:
            await self.on_create(number)
        if number in self.errors:
            raise self.errors[number]
        return DirectoryHospital(
            hospital_id=100 + number,
            name=hospital.name,
            address=hospital.address,
            phone=hospital.phone,
            creation_batch_id=batch_id,
            active=False,
        )

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Default to absent evidence so legacy uncertainty scenarios remain paused."""
        return list(self.evidence)

    async def activate_batch(self, batch_id: UUID) -> None:
        """Record one activation attempt and optionally gate/reject its reply."""
        self.activations.append(batch_id)
        if self.on_activate is not None:
            await self.on_activate()
        if self.activation_error is not None:
            raise self.activation_error


@pytest_asyncio.fixture(autouse=True)
async def registered_batch(repository: BatchRepository) -> None:
    """Seed the reference upload in the shared temporary repository."""
    await repository.register_batch(BATCH_ID, ROWS)


def operation_error(
    evidence: FailureEvidence, *, activation: bool = False
) -> DirectoryOperationError:
    """Build typed adapter failures independently of HTTP status/exception classes."""
    return DirectoryOperationError(
        DirectoryOperation.ACTIVATE_BATCH
        if activation
        else DirectoryOperation.CREATE_HOSPITAL,
        evidence,
        "Controlled Directory failure",
    )


@pytest.mark.asyncio
async def test_success_checkpoints_out_of_order_and_activates_after_every_creation(
    repository,
) -> None:
    """Confirm intent-before-call, independent persistence, ordering, and final shape."""
    directory = FakeDirectory()
    entered = [asyncio.Event() for _ in ROWS]
    releases = [asyncio.Event() for _ in ROWS]
    returned = []

    async def create_gate(number: int) -> None:
        """Inspect saved intent and hold each creation independently."""
        batch = await repository.get_batch(BATCH_ID)
        assert batch.rows[number - 1].state == RowState.CREATING
        entered[number - 1].set()
        await releases[number - 1].wait()
        returned.append(number)

    async def activation_check() -> None:
        """Read committed evidence while activation is outside every DB transaction."""
        batch = await repository.get_batch(BATCH_ID)
        assert batch.state == BatchState.ACTIVATING
        assert all(row.state == RowState.CREATED for row in batch.rows)
        assert [row.hospital_id for row in batch.rows] == [101, 102, 103]
        assert len(returned) == 3

    directory.on_create, directory.on_activate = create_gate, activation_check
    times = iter((START, START + timedelta(seconds=8.6)))
    processor = BatchProcessor(repository, directory, clock=lambda: next(times))
    task = asyncio.create_task(processor.process_batch(BATCH_ID))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered)), timeout=2
        )
        # Let row 3 finish while the other two still await external responses.
        releases[2].set()
        for _ in range(100):
            batch = await repository.get_batch(BATCH_ID)
            if batch.rows[2].state == RowState.CREATED:
                break
            await asyncio.sleep(0)
        assert batch.rows[2].hospital_id == 103
        assert batch.rows[0].state == batch.rows[1].state == RowState.CREATING
        assert directory.activations == []
        releases[1].set()
        releases[0].set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(task, return_exceptions=True)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.COMPLETED and batch.batch_activated
    assert batch.attempt_started_at is None and batch.processing_time_seconds == 8.6
    assert batch.last_error is None and batch.failed_hospitals == 0
    assert directory.activations == [BATCH_ID] and returned[0] == 3
    assert all(row.state == RowState.ACTIVATED for row in batch.rows)
    result = BatchResult.from_rows(
        batch_id=BATCH_ID,
        total_hospitals=batch.total_hospitals,
        rows=batch.rows,
        batch_activated=True,
        processing_time_seconds=batch.processing_time_seconds,
    ).model_dump(mode="json")
    assert set(result) == {
        "batch_id",
        "total_hospitals",
        "processed_hospitals",
        "failed_hospitals",
        "processing_time_seconds",
        "batch_activated",
        "hospitals",
    }
    assert [row["row"] for row in result["hospitals"]] == [1, 2, 3]
    assert all(row["status"] == "created_and_activated" for row in result["hospitals"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        FailureEvidence.VALIDATION_REJECTED,
        FailureEvidence.NOT_SENT,
        FailureEvidence.UNKNOWN_OUTCOME,
    ],
)
async def test_partial_failure_retains_other_successes_and_never_activates(
    repository, evidence
) -> None:
    """A definite or uncertain City Care result cannot discard its successful peers."""
    directory = FakeDirectory()
    directory.errors[2] = operation_error(evidence)
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    uncertain = evidence == FailureEvidence.UNKNOWN_OUTCOME
    assert batch.state == (
        BatchState.RECONCILIATION_REQUIRED if uncertain else BatchState.FAILED
    )
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[0].state == batch.rows[2].state == RowState.CREATED
    assert batch.rows[1].state == (RowState.UNKNOWN if uncertain else RowState.FAILED)
    assert batch.rows[1].failure_kind == directory.errors[2].failure_kind
    assert batch.processed_hospitals == 2 and batch.failed_hospitals == int(
        not uncertain
    )
    assert batch.attempt_started_at is None and not batch.batch_activated
    assert len(directory.creations) == 3 and directory.activations == []
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        FailureEvidence.VALIDATION_REJECTED,
        FailureEvidence.NOT_SENT,
        FailureEvidence.UNKNOWN_OUTCOME,
    ],
)
async def test_activation_failure_preserves_every_creation(
    repository, evidence
) -> None:
    """Activation errors never become hospital creation failures or trigger a retry."""
    directory = FakeDirectory()
    directory.activation_error = operation_error(evidence, activation=True)
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == (
        BatchState.RECONCILIATION_REQUIRED
        if evidence == FailureEvidence.UNKNOWN_OUTCOME
        else BatchState.FAILED
    )
    assert all(row.state == RowState.CREATED for row in batch.rows)
    assert [row.hospital_id for row in batch.rows] == [101, 102, 103]
    assert batch.processed_hospitals == 3 and batch.failed_hospitals == 0
    assert not batch.batch_activated and batch.attempt_started_at is None
    assert directory.activations == [BATCH_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_unexpected_row_exception_settles_peers_and_leaves_unknown_evidence(
    repository, caplog, error_type
) -> None:
    """A programming error after saved intent proves no definite remote rejection."""
    directory = FakeDirectory()
    directory.errors[2] = error_type("Unexpected adapter implementation error")
    processor = BatchProcessor(repository, directory)
    await processor.process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[1].failure_kind == FailureKind.UNCERTAIN
    assert batch.attempt_started_at is None and directory.activations == []
    assert str(BATCH_ID) in caplog.text and "row=2" in caplog.text
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
async def test_failed_success_checkpoint_leaves_uncertainty_without_losing_peers(
    repository, monkeypatch
) -> None:
    """A remote success followed by a local write error cannot authorize another POST."""
    record = repository.record_row_outcome

    async def fail_checkpoint(batch_id, row_number, **outcome):
        """Simulate one failed local success transaction, preserving other writes."""
        if row_number == 2 and outcome.get("hospital_id") is not None:
            raise RuntimeError("Checkpoint unavailable")
        return await record(batch_id, row_number, **outcome)

    monkeypatch.setattr(repository, "record_row_outcome", fail_checkpoint)
    directory = FakeDirectory()
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[1].state == RowState.UNKNOWN and directory.activations == []
    assert len(directory.creations) == 3


@pytest.mark.asyncio
async def test_resume_skips_saved_ids_and_only_retries_safe_failed_row(
    repository,
) -> None:
    """A second queued attempt retains its UUID and excludes idle time from duration."""
    directory = FakeDirectory()
    directory.errors[2] = operation_error(FailureEvidence.NOT_SENT)
    times = iter(
        (
            START,
            START + timedelta(seconds=2),
            START + timedelta(hours=1),
            START + timedelta(hours=1, seconds=3),
        )
    )
    processor = BatchProcessor(repository, directory, clock=lambda: next(times))
    await processor.process_batch(BATCH_ID)
    assert await repository.claim_for_resume(BATCH_ID)
    directory.errors.clear()
    await processor.process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.COMPLETED and batch.processing_time_seconds == 5
    assert directory.creations == [
        (1, BATCH_ID),
        (2, BATCH_ID),
        (3, BATCH_ID),
        (2, BATCH_ID),
    ]
    assert directory.activations == [BATCH_ID]


@pytest.mark.asyncio
async def test_activation_only_attempt_does_not_recreate_saved_hospitals(
    repository,
) -> None:
    """Known-unsent activation failure can requeue and activate existing creations."""
    directory = FakeDirectory()
    directory.activation_error = operation_error(
        FailureEvidence.NOT_SENT, activation=True
    )
    processor = BatchProcessor(repository, directory)
    await processor.process_batch(BATCH_ID)
    assert await repository.claim_for_resume(BATCH_ID)
    directory.activation_error = None
    directory.evidence = [
        DirectoryHospital(
            100 + row.row_number,
            row.hospital.name,
            row.hospital.address,
            BATCH_ID,
            False,
            row.hospital.phone,
        )
        for row in ROWS
    ]
    await processor.process_batch(BATCH_ID)
    assert len(directory.creations) == 3 and directory.activations == [
        BATCH_ID,
        BATCH_ID,
    ]
    assert (await repository.get_batch(BATCH_ID)).state == BatchState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence", [FailureEvidence.VALIDATION_REJECTED, FailureEvidence.UNKNOWN_OUTCOME]
)
async def test_requeued_protected_rows_are_never_recreated(
    repository, evidence
) -> None:
    """Premature requeue cannot bypass permanent rejection or reconciliation rules."""
    directory = FakeDirectory()
    directory.errors[2] = operation_error(evidence)
    processor = BatchProcessor(repository, directory)
    await processor.process_batch(BATCH_ID)
    if evidence == FailureEvidence.VALIDATION_REJECTED:
        assert not await repository.claim_for_resume(BATCH_ID)
    else:
        assert await repository.claim_for_resume(BATCH_ID)
        await processor.process_batch(BATCH_ID)
    assert len(directory.creations) == 3 and directory.activations == []
    batch = await repository.get_batch(BATCH_ID)
    assert batch.rows[1].hospital_id is None and batch.attempt_started_at is None


@pytest.mark.asyncio
async def test_competing_calls_claim_batch_once_and_completed_calls_do_nothing(
    repository,
) -> None:
    """Atomic database claims cover both shared and distinct processor instances."""
    directory = FakeDirectory()
    first, second = (
        BatchProcessor(repository, directory),
        BatchProcessor(repository, directory),
    )
    await asyncio.gather(
        first.process_batch(BATCH_ID),
        first.process_batch(BATCH_ID),
        second.process_batch(BATCH_ID),
    )
    await first.process_batch(BATCH_ID)
    await second.process_batch(uuid4())
    assert len(directory.creations) == 3 and directory.activations == [BATCH_ID]
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("during_activation", [False, True])
async def test_cancellation_drains_inflight_work_preserves_ids_and_releases_claim(
    repository, during_activation
) -> None:
    """Cancelled mutations become unresolved while previously saved peers survive."""
    directory = FakeDirectory()
    entered, release = asyncio.Event(), asyncio.Event()

    async def creation_gate(number: int) -> None:
        """Hold City's POST until the processor is cancelled."""
        if number == 2:
            entered.set()
            await release.wait()

    async def activation_gate() -> None:
        """Hold the activation acknowledgement after creation evidence is saved."""
        entered.set()
        await release.wait()

    if during_activation:
        directory.on_activate = activation_gate
    else:
        directory.on_create = creation_gate
    processor = BatchProcessor(repository, directory)
    task = asyncio.create_task(processor.process_batch(BATCH_ID))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if not during_activation:
            for _ in range(100):
                batch = await repository.get_batch(BATCH_ID)
                if batch.processed_hospitals == 2:
                    break
                await asyncio.sleep(0)
            assert batch.processed_hospitals == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert batch.attempt_started_at is None and not batch.batch_activated
    assert batch.processed_hospitals == (3 if during_activation else 2)
    assert batch.failed_hospitals == 0
    assert all(row.state != RowState.CREATING for row in batch.rows)
    assert await repository.count_in_flight_batches() == 0
    assert await repository.count_in_flight_batches() == 0
    # Direct invocation of paused work cannot accidentally replay a mutation.
    await processor.process_batch(BATCH_ID)
    assert len(directory.creations) == 3


@pytest.mark.asyncio
async def test_unexpected_activation_exception_keeps_all_creation_evidence(
    repository,
) -> None:
    """A broken adapter during activation leaves a recoverable, inspectable batch."""
    directory = FakeDirectory()
    directory.activation_error = RuntimeError(
        "Unexpected activation implementation error"
    )
    processor = BatchProcessor(repository, directory)
    with pytest.raises(RuntimeError, match="Unexpected activation"):
        await processor.process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert batch.processed_hospitals == 3 and batch.failed_hospitals == 0
    assert all(row.state == RowState.CREATED for row in batch.rows)
    assert batch.attempt_started_at is None
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
async def test_cancel_during_claim_commit_resolves_ownership_and_ends_attempt(
    repository, monkeypatch
) -> None:
    """Cancellation between claim commit and its return cannot strand active work."""
    entered, release = asyncio.Event(), asyncio.Event()
    start = repository.start_attempt

    async def delayed_claim(batch_id, *, started_at):
        """Commit normally, then hold the result until caller cancellation arrives."""
        result = await start(batch_id, started_at=started_at)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(repository, "start_attempt", delayed_claim)
    directory = FakeDirectory()
    times = iter((START, START + timedelta(seconds=1)))
    processor = BatchProcessor(repository, directory, clock=lambda: next(times))
    task = asyncio.create_task(processor.process_batch(BATCH_ID))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.INTERRUPTED
    assert batch.attempt_started_at is None and batch.processing_time_seconds == 1
    assert all(row.state == RowState.PENDING for row in batch.rows)
    assert directory.creations == directory.activations == []
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_acknowledgement", [False, True])
async def test_unexpected_activation_storage_error_preserves_manifest(
    repository, monkeypatch, after_acknowledgement
) -> None:
    """Distinguish pre-activation local failure from a lost completion checkpoint."""

    async def fail(batch_id, **options):
        """Simulate a local storage failure without changing any saved evidence."""
        raise RuntimeError("Activation storage failure")

    monkeypatch.setattr(
        repository,
        "complete_activation" if after_acknowledgement else "begin_activation",
        fail,
    )
    directory = FakeDirectory()
    processor = BatchProcessor(repository, directory)
    with pytest.raises(RuntimeError, match="Activation storage failure"):
        await processor.process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == (
        BatchState.RECONCILIATION_REQUIRED
        if after_acknowledgement
        else BatchState.FAILED
    )
    assert [row.hospital_id for row in batch.rows] == [101, 102, 103]
    assert batch.failed_hospitals == 0 and not batch.batch_activated
    assert batch.attempt_started_at is None
    assert directory.activations == ([BATCH_ID] if after_acknowledgement else [])
    assert await repository.count_in_flight_batches() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("abandoned_intent", [False, True])
async def test_old_uncertainty_blocks_new_creations_until_reconciliation(
    repository, abandoned_intent
) -> None:
    """A queued attempt cannot skip uncertainty and continue issuing new creations."""
    assert await repository.start_attempt(BATCH_ID)
    assert await repository.start_creation(BATCH_ID, 2)
    if not abandoned_intent:
        assert await repository.record_row_outcome(
            BATCH_ID, 2, failure_kind=FailureKind.UNCERTAIN, error="Prior reply lost"
        )
    assert await repository.finish_attempt(BATCH_ID, BatchState.INTERRUPTED)
    assert await repository.claim_for_resume(BATCH_ID)
    directory = FakeDirectory()
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert [row.state for row in batch.rows] == [
        RowState.PENDING,
        RowState.UNKNOWN,
        RowState.PENDING,
    ]
    assert directory.creations == directory.activations == []
    assert batch.attempt_started_at is None


@pytest.mark.asyncio
async def test_uncertainty_takes_priority_over_definite_peer_failure(
    repository,
) -> None:
    """A definite rejection cannot hide a different row's possibly committed POST."""
    directory = FakeDirectory()
    directory.errors = {
        1: operation_error(FailureEvidence.VALIDATION_REJECTED),
        2: operation_error(FailureEvidence.UNKNOWN_OUTCOME),
    }
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert [row.state for row in batch.rows] == [
        RowState.FAILED,
        RowState.UNKNOWN,
        RowState.CREATED,
    ]
    assert batch.processed_hospitals == batch.failed_hospitals == 1
    assert directory.activations == []
