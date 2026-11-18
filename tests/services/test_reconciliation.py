"""Conservative identity recovery and activation inspection with a typed fake."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from app.domain import (
    BatchState,
    DirectoryHospital,
    DirectoryOperation,
    DirectoryOperationError,
    FailureEvidence,
    FailureKind,
    HospitalInput,
    HospitalRow,
    RowState,
)
from app.services.processor import BatchProcessor
from app.services.reconciliation import BatchReconciler, hospital_key

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
INPUTS = (
    HospitalInput("General Hospital", "123 Main Street", "555-0101"),
    HospitalInput("City Care Hospital", "45 Park Road", "555-0102"),
    HospitalInput("Sunrise Hospital", "8 Lake Road", "555-0103"),
)


def remote(
    identifier: int, hospital: HospitalInput, *, active: bool = False
) -> DirectoryHospital:
    """Build typed evidence with explicit identity, input, membership, and activity."""
    return DirectoryHospital(
        identifier, hospital.name, hospital.address, BATCH_ID, active, hospital.phone
    )


class FakeDirectory:
    """Keep remote side effects separately from whether their replies arrive."""

    def __init__(self) -> None:
        """Default to successful requests; individual tests inject one uncertainty."""
        self.records = []
        self.creations = []
        self.activations = 0
        self.lookups = 0
        self.lost_creation = False
        self.commit_creation = True
        self.lost_activation = False
        self.commit_activation = True
        self.lookup_error = None

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Optionally commit City Care while losing its creation reply."""
        assert batch_id == BATCH_ID
        self.creations.append(hospital)
        record = remote(101 + INPUTS.index(hospital), hospital)
        if not (hospital == INPUTS[1] and self.lost_creation) or self.commit_creation:
            self.records.append(record)
        if hospital == INPUTS[1] and self.lost_creation:
            raise DirectoryOperationError(
                DirectoryOperation.CREATE_HOSPITAL,
                FailureEvidence.UNKNOWN_OUTCOME,
                "Creation reply lost",
            )
        return record

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Return one controlled snapshot per reconciliation decision."""
        assert batch_id == BATCH_ID
        self.lookups += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        return list(self.records)

    async def activate_batch(self, batch_id: UUID) -> None:
        """Optionally commit activation while losing its acknowledgement."""
        assert batch_id == BATCH_ID
        self.activations += 1
        if self.commit_activation:
            self.records = [replace(record, active=True) for record in self.records]
        if self.lost_activation:
            raise DirectoryOperationError(
                DirectoryOperation.ACTIVATE_BATCH,
                FailureEvidence.UNKNOWN_OUTCOME,
                "Activation reply lost",
            )


async def register(repository, inputs=INPUTS) -> None:
    """Persist ordered fresh input without making any external request."""
    await repository.register_batch(
        BATCH_ID,
        [HospitalRow(number, hospital) for number, hospital in enumerate(inputs, 1)],
    )


async def seed(repository, *, inputs=INPUTS, unknown=(2,)) -> None:
    """Create an active saved manifest with selected unconfirmed hospital IDs."""
    await register(repository, inputs)
    assert await repository.start_attempt(BATCH_ID)
    for number in range(1, len(inputs) + 1):
        assert await repository.start_creation(BATCH_ID, number)
        if number in unknown:
            assert await repository.record_row_outcome(
                BATCH_ID, number, failure_kind=FailureKind.UNCERTAIN, error="Reply lost"
            )
        else:
            assert await repository.record_row_outcome(
                BATCH_ID, number, hospital_id=100 + number
            )


@pytest.mark.asyncio
async def test_lost_creation_reply_recovers_id_without_another_post(repository) -> None:
    """GET evidence recovers City and enables the normal activation barrier."""
    await register(repository)
    directory = FakeDirectory()
    directory.lost_creation = True
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.COMPLETED and batch.batch_activated
    assert [row.hospital_id for row in batch.rows] == [101, 102, 103]
    assert all(row.failure_kind is None for row in batch.rows)
    assert (
        len(directory.creations) == 3
        and directory.lookups == directory.activations == 1
    )


@pytest.mark.asyncio
async def test_missing_record_remains_unknown_across_bounded_manual_attempts(
    repository,
) -> None:
    """Repeated absence must never become permission to issue City's POST again."""
    await register(repository)
    directory = FakeDirectory()
    directory.lost_creation, directory.commit_creation = True, False
    processor = BatchProcessor(repository, directory)
    await processor.process_batch(BATCH_ID)
    for _ in range(2):
        assert await repository.claim_for_resume(BATCH_ID)
        await processor.process_batch(BATCH_ID, reconcile_before_processing=True)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert batch.rows[1].state == RowState.UNKNOWN and batch.rows[1].hospital_id is None
    assert batch.failed_hospitals == 0 and batch.processed_hospitals == 2
    assert (
        len(directory.creations) == 3
        and directory.lookups == 3
        and directory.activations == 0
    )


@pytest.mark.asyncio
async def test_failed_lookup_preserves_unknown_and_known_evidence(repository) -> None:
    """Read failures cannot reclassify an unconfirmed write as definite rejection."""
    await seed(repository)
    directory = FakeDirectory()
    directory.lookup_error = DirectoryOperationError(
        DirectoryOperation.GET_BATCH,
        FailureEvidence.UNKNOWN_OUTCOME,
        "Lookup unavailable",
    )
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert not result.resolved and result.error == "Lookup unavailable"
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[1].failure_kind == FailureKind.UNCERTAIN
    assert directory.lookups == 1 and directory.creations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 2, 3])
async def test_identical_input_requires_exact_remaining_multiplicity(
    repository, candidate_count
) -> None:
    """One record cannot satisfy two rows and extra records remain ambiguous."""
    await seed(repository, inputs=(INPUTS[0], INPUTS[0]), unknown=(1, 2))
    directory = FakeDirectory()
    directory.records = [
        remote(101 + number, INPUTS[0]) for number in range(candidate_count)
    ]
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert result.resolved == (candidate_count == 2)
    assert [row.hospital_id for row in batch.rows] == (
        [101, 102] if candidate_count == 2 else [None, None]
    )


@pytest.mark.asyncio
async def test_known_id_is_accounted_before_matching_identical_unknown_input(
    repository,
) -> None:
    """A confirmed peer's ID cannot be reassigned to satisfy another identical row."""
    await seed(repository, inputs=(INPUTS[0], INPUTS[0]), unknown=(2,))
    directory = FakeDirectory()
    directory.records = [remote(102, INPUTS[0]), remote(101, INPUTS[0])]
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    assert result.resolved
    assert [row.hospital_id for row in (await repository.get_batch(BATCH_ID)).rows] == [
        101,
        102,
    ]


@pytest.mark.asyncio
async def test_record_cannot_be_attributed_between_unknown_and_pending_identical_rows(
    repository,
) -> None:
    """Conservative attribution blocks candidates also matching protected input."""
    await register(repository, (INPUTS[0], INPUTS[0]))
    assert await repository.start_attempt(BATCH_ID)
    assert await repository.start_creation(BATCH_ID, 1)
    assert await repository.record_row_outcome(
        BATCH_ID, 1, failure_kind=FailureKind.UNCERTAIN, error="Lost"
    )
    directory = FakeDirectory()
    directory.records = [remote(101, INPUTS[0])]
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    assert not result.resolved
    assert [row.hospital_id for row in (await repository.get_batch(BATCH_ID)).rows] == [
        None,
        None,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variation",
    [
        "batch",
        "duplicate",
        "known_input",
        "extra",
        "missing_known",
        "mixed_active",
        "id_type",
        "active_type",
    ],
)
async def test_inconsistent_evidence_blocks_recovery_before_any_new_id_is_saved(
    repository, variation
) -> None:
    """Validate the complete snapshot before adopting otherwise plausible City data."""
    await seed(repository)
    directory = FakeDirectory()
    records = [remote(101 + number, hospital) for number, hospital in enumerate(INPUTS)]
    if variation == "batch":
        records[1] = replace(records[1], creation_batch_id=uuid4())
    elif variation == "duplicate":
        records.append(records[1])
    elif variation == "known_input":
        records[0] = replace(records[0], address="Other address")
    elif variation == "extra":
        records.append(remote(999, HospitalInput("Unexpected", "Address")))
    elif variation == "missing_known":
        records.pop(0)
    elif variation == "mixed_active":
        records[0] = replace(records[0], active=True)
    elif variation == "id_type":
        records[1] = replace(records[1], hospital_id=True)
    else:
        records[1] = replace(records[1], active="false")
    directory.records = records
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    assert not result.resolved
    assert [row.hospital_id for row in (await repository.get_batch(BATCH_ID)).rows] == [
        101,
        None,
        103,
    ]
    assert directory.activations == 0 and directory.creations == []


@pytest.mark.asyncio
async def test_distinct_resolved_group_is_saved_while_other_unknown_stays_unresolved(
    repository,
) -> None:
    """Independent evidence can be retained without declaring the whole batch ready."""
    await seed(repository, unknown=(1, 2))
    directory = FakeDirectory()
    directory.records = [remote(101, INPUTS[0]), remote(103, INPUTS[2])]
    result = await BatchReconciler(repository, directory).reconcile(BATCH_ID)
    assert not result.resolved
    batch = await repository.get_batch(BATCH_ID)
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[1].state == RowState.UNKNOWN


@pytest.mark.asyncio
async def test_lost_activation_reply_is_confirmed_from_all_expected_active_ids(
    repository,
) -> None:
    """Confirmed remote activation completes locally without another activation call."""
    await register(repository)
    directory = FakeDirectory()
    directory.lost_activation = True
    await BatchProcessor(repository, directory).process_batch(BATCH_ID)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.COMPLETED and batch.batch_activated
    assert (
        directory.activations == directory.lookups == 1
        and len(directory.creations) == 3
    )


@pytest.mark.asyncio
async def test_lost_inactive_activation_defers_retry_until_a_new_checked_attempt(
    repository,
) -> None:
    """One mutation per attempt avoids an activation/reconciliation retry loop."""
    await register(repository)
    directory = FakeDirectory()
    directory.lost_activation, directory.commit_activation = True, False
    processor = BatchProcessor(repository, directory)
    await processor.process_batch(BATCH_ID)
    assert (
        await repository.get_batch(BATCH_ID)
    ).state == BatchState.RECONCILIATION_REQUIRED
    assert directory.activations == directory.lookups == 1
    assert await repository.claim_for_resume(BATCH_ID)
    directory.lost_activation, directory.commit_activation = False, True
    await processor.process_batch(BATCH_ID, reconcile_before_processing=True)
    assert (await repository.get_batch(BATCH_ID)).state == BatchState.COMPLETED
    assert (
        directory.activations == directory.lookups == 2
        and len(directory.creations) == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("all_active", [False, True])
async def test_activation_only_resume_inspects_evidence_before_remaining_operation(
    repository, all_active
) -> None:
    """Saved creations are never routed through the creation path on activation recovery."""
    await seed(repository, unknown=())
    assert await repository.finish_attempt(BATCH_ID, BatchState.RECONCILIATION_REQUIRED)
    assert await repository.claim_for_resume(BATCH_ID)
    directory = FakeDirectory()
    directory.records = [
        remote(101 + number, hospital, active=all_active)
        for number, hospital in enumerate(INPUTS)
    ]
    await BatchProcessor(repository, directory).process_batch(
        BATCH_ID, reconcile_before_processing=True
    )
    assert (await repository.get_batch(BATCH_ID)).state == BatchState.COMPLETED
    assert directory.lookups == 1 and directory.activations == int(not all_active)
    assert directory.creations == []


@pytest.mark.asyncio
async def test_cancelling_lookup_preserves_creation_ids_and_unknown_evidence(
    repository,
) -> None:
    """Cancelling a read cannot authorize mutation replay or strand the attempt."""
    await seed(repository)
    assert await repository.finish_attempt(BATCH_ID, BatchState.RECONCILIATION_REQUIRED)
    assert await repository.claim_for_resume(BATCH_ID)
    entered = asyncio.Event()

    class GatedDirectory(FakeDirectory):
        """Hold the recovery lookup long enough to cancel its owning processor."""

        async def get_hospitals_by_batch(
            self, batch_id: UUID
        ) -> list[DirectoryHospital]:
            """Signal the read boundary; cancellation interrupts this wait."""
            entered.set()
            await asyncio.Event().wait()
            return []

    directory = GatedDirectory()
    task = asyncio.create_task(
        BatchProcessor(repository, directory).process_batch(
            BATCH_ID, reconcile_before_processing=True
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    batch = await repository.get_batch(BATCH_ID)
    assert batch.state == BatchState.RECONCILIATION_REQUIRED
    assert batch.attempt_started_at is None
    assert [row.hospital_id for row in batch.rows] == [101, None, 103]
    assert batch.rows[1].state == RowState.UNKNOWN
    assert directory.creations == [] and directory.activations == 0


def test_matching_trims_only_and_preserves_case_phone_and_punctuation() -> None:
    """Comparison must not invent fuzzy hospital identity or lose leading zeros."""
    assert hospital_key(HospitalInput(" A ", " B ", " 001 ")) == ("A", "B", "001")
    assert hospital_key(HospitalInput("a", "B", "001")) != hospital_key(
        HospitalInput("A", "B", "1")
    )


@pytest.mark.asyncio
async def test_fresh_claimed_state_requires_lookup_when_route_hint_is_stale(
    repository,
) -> None:
    """New saved IDs can require activation inspection despite an old false hint."""
    await seed(repository, unknown=())
    assert await repository.finish_attempt(BATCH_ID, BatchState.RECONCILIATION_REQUIRED)
    assert await repository.claim_for_resume(BATCH_ID)
    directory = FakeDirectory()
    directory.records = [
        remote(101 + number, hospital, active=True)
        for number, hospital in enumerate(INPUTS)
    ]
    await BatchProcessor(repository, directory).process_batch(
        BATCH_ID, reconcile_before_processing=False
    )
    assert (await repository.get_batch(BATCH_ID)).state == BatchState.COMPLETED
    assert directory.lookups == 1 and directory.activations == 0
    assert directory.creations == []
