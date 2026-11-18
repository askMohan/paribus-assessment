"""Real SQLite transaction, constraint, ownership, and recovery-evidence checks."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError, StatementError

from app.api.schemas import BatchResult
from app.db.database import Database
from app.db.models import BatchModel, HospitalRowModel
from app.db.repository import BatchCapacityError, BatchRepository
from app.domain import BatchState, FailureKind, HospitalInput, RowState
from app.services.csv_validation import validate_csv_bytes

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
START = datetime(2026, 11, 18, 10, tzinfo=UTC)
SAMPLE = (
    b"name,address,phone\nGeneral Hospital,123 Main Street,555-0101\n"
    b"City Care Hospital,45 Park Road,555-0102\nSunrise Hospital,8 Lake Road,555-0103\n"
)


def sample_rows():
    """Use the real validator to build the shared three-row manifest, ensuring
    repository tests exercise the same normalized input shape as upload acceptance.
    """
    return validate_csv_bytes(SAMPLE, max_upload_bytes=1024)


async def start_batch(repository: BatchRepository) -> None:
    """Register the reference batch and claim its first attempt at a fixed UTC time, so
    later tests focus on outcome persistence rather than repeated setup.
    """
    await repository.register_batch(BATCH_ID, sample_rows())
    assert await repository.start_attempt(BATCH_ID, started_at=START)


async def create_all(repository: BatchRepository) -> None:
    """Checkpoint all three controlled creation IDs locally, establishing the
    prerequisite for activation tests without live Directory calls.
    """
    for number in (1, 2, 3):
        assert await repository.start_creation(BATCH_ID, number)
        assert await repository.record_row_outcome(
            BATCH_ID, number, hospital_id=100 + number
        )


@pytest.mark.asyncio
async def test_registration_preserves_input_and_returns_consistent_ordered_snapshot(
    repository,
):
    """Verify atomic input registration and ordered typed reads, including absent-batch
    behavior and derived initial counts.
    """
    await repository.register_batch(BATCH_ID, list(reversed(sample_rows())))
    batch = await repository.get_batch(BATCH_ID)

    assert batch.batch_id == BATCH_ID
    assert batch.state == BatchState.QUEUED
    assert batch.total_hospitals == 3
    assert not batch.batch_activated
    assert batch.attempt_started_at is None
    assert batch.processing_time_seconds == 0
    assert batch.processed_hospitals == batch.failed_hospitals == 0
    assert [row.row_number for row in batch.rows] == [1, 2, 3]
    assert batch.rows == tuple(sample_rows())
    assert await repository.get_batch(uuid4()) is None


@pytest.mark.asyncio
async def test_registration_rolls_back_batch_and_earlier_rows_if_later_insert_fails(
    repository,
):
    """Force a later row constraint failure to prove the earlier batch/header and row
    inserts are also rolled back, then confirm the connection remains usable.
    """
    rows = sample_rows()
    rows[1] = replace(rows[1], hospital=HospitalInput("City Care", ""))
    with pytest.raises(IntegrityError):
        await repository.register_batch(BATCH_ID, rows)

    assert await repository.get_batch(BATCH_ID) is None
    async with repository.database.transaction() as session:
        assert (
            await session.scalar(select(func.count()).select_from(HospitalRowModel))
            == 0
        )
    await repository.register_batch(BATCH_ID, sample_rows())
    assert (await repository.get_batch(BATCH_ID)).total_hospitals == 3


@pytest.mark.asyncio
async def test_foreign_keys_duplicate_rows_and_invalid_states_are_enforced(repository):
    """Check that SQLite itself rejects orphan rows, duplicate positions, impossible
    states, and unsupported success claims, not just the repository happy path.
    """
    await repository.register_batch(BATCH_ID, sample_rows())
    for parent in (str(uuid4()), str(BATCH_ID)):
        with pytest.raises(IntegrityError):
            async with repository.database.transaction(write=True) as session:
                session.add(
                    HospitalRowModel(
                        batch_id=parent,
                        row_number=1,
                        name="Invalid row",
                        address="Main Street",
                    )
                )
    for state in ("INVALID", RowState.CREATED, RowState.UNKNOWN):
        with pytest.raises(StatementError):
            async with repository.database.transaction(write=True) as session:
                row = await session.get(HospitalRowModel, (str(BATCH_ID), 1))
                row.state = state
    with pytest.raises(IntegrityError):
        async with repository.database.transaction(write=True) as session:
            batch = await session.get(BatchModel, str(BATCH_ID))
            batch.batch_activated = True
    assert (await repository.get_batch(BATCH_ID)).rows == tuple(sample_rows())


@pytest.mark.asyncio
async def test_same_remote_id_cannot_be_assigned_to_two_rows(repository):
    """Prevent one upstream hospital satisfying two input rows and confirm a failed
    checkpoint leaves prior evidence intact.
    """
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 1)
    await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)
    await repository.start_creation(BATCH_ID, 2)
    with pytest.raises(IntegrityError):
        await repository.record_row_outcome(BATCH_ID, 2, hospital_id=101)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.rows[0].hospital_id == 101
    assert batch.rows[1].hospital_id is None
    assert batch.rows[1].state == RowState.CREATING
    assert await repository.record_row_outcome(BATCH_ID, 2, hospital_id=102)


@pytest.mark.asyncio
async def test_independent_concurrent_updates_do_not_share_transactions(repository):
    """Run simultaneous row writes to confirm transaction serialization preserves
    independent successes and uncertainty without accidental shared commits.
    """
    await start_batch(repository)
    assert all(
        await asyncio.gather(
            *(repository.start_creation(BATCH_ID, number) for number in (1, 2, 3))
        )
    )
    results = await asyncio.gather(
        repository.record_row_outcome(BATCH_ID, 3, hospital_id=103),
        repository.record_row_outcome(
            BATCH_ID, 2, failure_kind=FailureKind.UNCERTAIN, error="Response lost"
        ),
        repository.record_row_outcome(BATCH_ID, 1, hospital_id=101),
    )
    assert all(results)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.processed_hospitals == 2
    assert batch.failed_hospitals == 0
    assert batch.rows[1].failure_kind == FailureKind.UNCERTAIN
    assert batch.rows[1].last_error == "Response lost"
    assert not await repository.begin_activation(BATCH_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_update", [False, True])
async def test_abandoned_intent_cleanup_is_atomic_and_preserves_confirmed_ids(
    repository, reject_update
):
    """Checkpoint every unconfirmed row together; a write failure preserves all intent."""
    await start_batch(repository)
    for number in (1, 2, 3):
        assert await repository.start_creation(BATCH_ID, number)
    assert await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)
    before = await repository.get_batch(BATCH_ID)

    def reject_last_update(mapper, connection, row):
        """Fail the last unknown checkpoint to exercise whole-transaction rollback."""
        if row.row_number == 3 and row.state == RowState.UNKNOWN:
            raise RuntimeError("Unknown checkpoint unavailable")

    if reject_update:
        event.listen(HospitalRowModel, "before_update", reject_last_update)
        try:
            with pytest.raises(RuntimeError, match="checkpoint unavailable"):
                await repository.mark_unconfirmed(BATCH_ID, "Response lost")
        finally:
            event.remove(HospitalRowModel, "before_update", reject_last_update)
        assert await repository.get_batch(BATCH_ID) == before

    batch = await repository.mark_unconfirmed(BATCH_ID, "Response lost")
    assert [row.state for row in batch.rows] == [
        RowState.CREATED,
        RowState.UNKNOWN,
        RowState.UNKNOWN,
    ]
    assert [row.hospital_id for row in batch.rows] == [101, None, None]
    assert batch.state == BatchState.PROCESSING and batch.attempt_started_at == START
    assert batch.processed_hospitals == 1 and batch.failed_hospitals == 0
    assert all(row.failure_kind == FailureKind.UNCERTAIN for row in batch.rows[1:])
    assert await repository.mark_unconfirmed(BATCH_ID, "Later cleanup") == batch


@pytest.mark.asyncio
async def test_saved_ids_and_unknown_outcomes_survive_reopening_and_initialization(
    tmp_path,
):
    """Close and reopen the same temporary file to verify idempotent schema startup
    preserves confirmed IDs, uncertainty, and the active attempt marker.
    """
    path = tmp_path / "nested" / "hospitals.db"
    async with Database(path) as database:
        repository = BatchRepository(database)
        await start_batch(repository)
        for number in (1, 2, 3):
            await repository.start_creation(BATCH_ID, number)
        await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)
        await repository.record_row_outcome(BATCH_ID, 3, hospital_id=103)
        await repository.record_row_outcome(
            BATCH_ID, 2, failure_kind=FailureKind.UNCERTAIN, error="Response lost"
        )
    async with Database(path) as reopened:
        batch = await BatchRepository(reopened).get_batch(BATCH_ID)
        assert batch.processed_hospitals == 2
        assert batch.failed_hospitals == 0
        assert [row.hospital_id for row in batch.rows] == [101, None, 103]
        assert batch.attempt_started_at == START
        assert batch.rows[1].state == RowState.UNKNOWN


@pytest.mark.asyncio
async def test_creation_failures_cannot_overwrite_confirmed_success(repository):
    """Guard against late error callbacks and conflicting IDs downgrading or replacing
    a previously confirmed hospital.
    """
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 1)
    await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)

    assert not await repository.record_row_outcome(
        BATCH_ID, 1, failure_kind=FailureKind.UNCERTAIN, error="Late error"
    )
    assert not await repository.record_row_outcome(
        BATCH_ID, 1, failure_kind=FailureKind.PERMANENT, error="Late failure"
    )
    assert not await repository.start_creation(BATCH_ID, 1)
    assert not await repository.record_row_outcome(BATCH_ID, 1, hospital_id=999)
    assert await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)
    assert (await repository.get_batch(BATCH_ID)).rows[0].hospital_id == 101


@pytest.mark.asyncio
async def test_known_safe_failure_can_retry_and_confirmed_success_clears_error(
    repository,
):
    """Allow replay only for established safe failure and verify current failure
    metadata/counts disappear after confirmation.
    """
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 2)
    assert await repository.record_row_outcome(
        BATCH_ID, 2, failure_kind=FailureKind.RETRYABLE, error="Not sent"
    )
    assert (await repository.get_batch(BATCH_ID)).failed_hospitals == 1
    assert await repository.start_creation(BATCH_ID, 2)
    assert await repository.record_row_outcome(BATCH_ID, 2, hospital_id=102)

    row = (await repository.get_batch(BATCH_ID)).rows[1]
    assert row.failure_kind is None and row.last_error is None
    assert (await repository.get_batch(BATCH_ID)).failed_hospitals == 0


@pytest.mark.asyncio
async def test_permanent_and_unknown_outcomes_cannot_start_another_post(repository):
    """Keep unchanged rejected input and possibly committed creation out of the
    automatic retry path; only recovered evidence can resolve the unknown row.
    """
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 1)
    await repository.start_creation(BATCH_ID, 2)
    await repository.record_row_outcome(
        BATCH_ID, 1, failure_kind=FailureKind.PERMANENT, error="Rejected input"
    )
    await repository.record_row_outcome(
        BATCH_ID, 2, failure_kind=FailureKind.UNCERTAIN, error="Read timeout"
    )
    assert not await repository.start_creation(BATCH_ID, 1)
    assert not await repository.start_creation(BATCH_ID, 2)
    assert await repository.record_row_outcome(BATCH_ID, 2, hospital_id=102)
    assert (await repository.get_batch(BATCH_ID)).rows[1].failure_kind is None
    assert await repository.start_creation(BATCH_ID, 3)
    assert await repository.record_row_outcome(
        BATCH_ID, 3, failure_kind=FailureKind.UNCERTAIN, error="Uncertain"
    )
    assert (await repository.get_batch(BATCH_ID)).rows[2].state == RowState.UNKNOWN


@pytest.mark.asyncio
async def test_activation_barrier_and_atomic_completion(repository):
    """Require all manifest rows to be confirmed before activation, then save row
    activation, batch completion, and timing consistently with no repeat completion.
    """
    await start_batch(repository)
    assert not await repository.begin_activation(BATCH_ID)
    assert not await repository.complete_activation(BATCH_ID)
    await create_all(repository)
    assert await repository.begin_activation(BATCH_ID)
    assert not await repository.begin_activation(BATCH_ID)
    assert await repository.complete_activation(
        BATCH_ID, ended_at=START + timedelta(seconds=5)
    )
    batch = await repository.get_batch(BATCH_ID)

    assert batch.state == BatchState.COMPLETED and batch.batch_activated
    assert batch.attempt_started_at is None
    assert batch.processing_time_seconds == 5
    assert all(row.state == RowState.ACTIVATED for row in batch.rows)
    assert batch.processed_hospitals == 3 and batch.failed_hospitals == 0
    assert not await repository.claim_for_resume(BATCH_ID)
    assert not await repository.complete_activation(
        BATCH_ID, ended_at=START + timedelta(seconds=50)
    )
    result = BatchResult.from_rows(
        batch_id=batch.batch_id,
        total_hospitals=batch.total_hospitals,
        rows=batch.rows,
        processing_time_seconds=batch.processing_time_seconds,
        batch_activated=batch.batch_activated,
    )
    assert (
        result.model_dump(mode="json")["hospitals"][0]["status"]
        == "created_and_activated"
    )


@pytest.mark.asyncio
async def test_completion_rolls_back_all_activation_updates_if_flush_fails(repository):
    """Inject an ORM flush failure to prove batch and row updates remain atomic."""
    await start_batch(repository)
    await create_all(repository)
    await repository.begin_activation(BATCH_ID)

    def reject_activation(mapper, connection, target):
        """Fail a real mapper flush without depending on hand-written SQL triggers."""
        if target.state == RowState.ACTIVATED:
            raise RuntimeError("injected completion failure")

    event.listen(HospitalRowModel, "before_update", reject_activation)
    try:
        with pytest.raises(RuntimeError, match="injected completion failure"):
            await repository.complete_activation(BATCH_ID)
    finally:
        event.remove(HospitalRowModel, "before_update", reject_activation)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.ACTIVATING and not batch.batch_activated
    assert all(row.state == RowState.CREATED for row in batch.rows)


@pytest.mark.asyncio
async def test_activation_failure_and_resume_preserve_ids_and_exclude_idle_time(
    repository,
):
    """Recover activation without recreating hospitals, and prove two active attempt
    durations exclude a long idle gap between manual resumes.
    """
    await start_batch(repository)
    await create_all(repository)
    await repository.begin_activation(BATCH_ID)
    assert await repository.finish_attempt(
        BATCH_ID,
        BatchState.FAILED,
        error="Activation failed",
        ended_at=START + timedelta(seconds=5),
    )
    assert not await repository.finish_attempt(BATCH_ID, BatchState.FAILED)
    failed = await repository.get_batch(BATCH_ID)
    assert failed.processed_hospitals == 3 and failed.failed_hospitals == 0
    assert failed.last_error == "Activation failed"
    assert failed.processing_time_seconds == 5

    assert await repository.claim_for_resume(BATCH_ID)
    assert not await repository.claim_for_resume(BATCH_ID)
    resumed_at = START + timedelta(hours=2)
    assert await repository.start_attempt(BATCH_ID, started_at=resumed_at)
    assert await repository.begin_activation(BATCH_ID)
    assert await repository.complete_activation(
        BATCH_ID, ended_at=resumed_at + timedelta(seconds=2)
    )
    completed = await repository.get_batch(BATCH_ID)
    assert completed.processing_time_seconds == 7
    assert [row.hospital_id for row in completed.rows] == [101, 102, 103]
    assert completed.last_error is None


@pytest.mark.asyncio
async def test_competing_claims_on_independent_connections_have_one_winner(tmp_path):
    """Exercise real SQLite write ownership across separate connections so both initial
    processing and resume cannot be claimed twice.
    """
    path = tmp_path / "shared.db"
    async with Database(path) as first_db, Database(path) as second_db:
        first, second = BatchRepository(first_db), BatchRepository(second_db)
        await first.register_batch(BATCH_ID, sample_rows())
        claims = await asyncio.gather(
            first.start_attempt(BATCH_ID, started_at=START),
            second.start_attempt(BATCH_ID, started_at=START),
        )
        assert sorted(claims) == [False, True]
        await first.finish_attempt(BATCH_ID, BatchState.FAILED, ended_at=START)
        resumes = await asyncio.gather(
            first.claim_for_resume(BATCH_ID), second.claim_for_resume(BATCH_ID)
        )
        assert sorted(resumes) == [False, True]


@pytest.mark.asyncio
async def test_atomic_admission_and_resume_limits(tmp_path):
    """Race new submissions against one remaining capacity slot and ensure
    paused/resumed work follows the same atomic admission budget.
    """
    path = tmp_path / "capacity.db"
    async with Database(path) as first_db, Database(path) as second_db:
        first, second = BatchRepository(first_db), BatchRepository(second_db)
        second_id = uuid4()
        results = await asyncio.gather(
            first.register_batch(BATCH_ID, sample_rows(), max_in_flight_batches=1),
            second.register_batch(second_id, sample_rows(), max_in_flight_batches=1),
            return_exceptions=True,
        )
        assert sum(isinstance(result, BatchCapacityError) for result in results) == 1
        assert await first.count_in_flight_batches() == 1
        winner = BATCH_ID if await first.get_batch(BATCH_ID) is not None else second_id
        loser = second_id if winner == BATCH_ID else BATCH_ID
        assert await first.get_batch(loser) is None
        await first.start_attempt(winner, started_at=START)
        await first.finish_attempt(winner, BatchState.FAILED, ended_at=START)
        assert await first.count_in_flight_batches() == 0
        await first.register_batch(loser, sample_rows(), max_in_flight_batches=1)
        with pytest.raises(BatchCapacityError):
            await first.claim_for_resume(winner, max_in_flight_batches=1)
        assert (await first.get_batch(winner)).state == BatchState.FAILED


@pytest.mark.asyncio
async def test_active_batch_query_does_not_replay_or_mutate_saved_work(repository):
    """Keep restart inspection read-only and verify missing/ineligible state changes
    and invalid clock values are handled explicitly.
    """
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 1)
    assert await repository.count_in_flight_batches() == 1
    assert (await repository.get_batch(BATCH_ID)).rows[0].state == RowState.CREATING
    assert not await repository.start_attempt(uuid4())
    assert not await repository.claim_for_resume(uuid4())
    with pytest.raises(ValueError, match="terminal"):
        await repository.finish_attempt(BATCH_ID, BatchState.COMPLETED)
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.start_attempt(BATCH_ID, started_at=datetime(2026, 11, 18))


@pytest.mark.asyncio
async def test_canceled_write_rolls_back_and_leaves_connection_usable(repository):
    """Cancel a write before completion and prove rollback releases the connection with
    no partial accepted-looking batch.
    """
    inserted = asyncio.Event()

    async def interrupted_registration():
        """Pause intentionally after the header insert so cancellation tests can
        inspect rollback of a genuinely open write transaction; this wait exists
        only in the test.
        """
        async with repository.database.transaction(write=True) as session:
            session.add(BatchModel(batch_id=str(BATCH_ID), total_hospitals=3))
            await session.flush()
            inserted.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(interrupted_registration())
    await inserted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await repository.get_batch(BATCH_ID) is None
    await repository.register_batch(BATCH_ID, sample_rows())
    assert (await repository.get_batch(BATCH_ID)).total_hospitals == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        {},
        {"hospital_id": True},
        {
            "hospital_id": 101,
            "failure_kind": FailureKind.RETRYABLE,
            "error": "Conflicting",
        },
        {"failure_kind": FailureKind.UNCERTAIN},
    ],
)
async def test_combined_outcome_rejects_conflicting_or_incomplete_evidence(
    repository, outcome
):
    """Keep the consolidated write API from accepting ambiguous argument combinations."""
    await start_batch(repository)
    await repository.start_creation(BATCH_ID, 1)
    with pytest.raises(ValueError):
        await repository.record_row_outcome(BATCH_ID, 1, **outcome)
    row = (await repository.get_batch(BATCH_ID)).rows[0]
    assert row.state == RowState.CREATING
    assert row.hospital_id is None and row.failure_kind is None
