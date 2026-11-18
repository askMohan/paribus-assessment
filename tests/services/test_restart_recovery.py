"""Restart detection preserves saved evidence without replaying external writes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

import app.main as main_module
from app.config import Settings
from app.db.repository import BatchRepository
from app.domain import (
    BatchState,
    DirectoryHospital,
    FailureKind,
    HospitalInput,
    HospitalRow,
    RowState,
)
from app.services.processor import BatchProcessor

START = datetime(2026, 11, 1, tzinfo=UTC)
ROWS = tuple(
    HospitalRow(number, HospitalInput(name, f"Address {number}"))
    for number, name in enumerate(("General", "City Care", "Sunrise"), start=1)
)


class RecoveryDirectory:
    """Return explicit batch evidence and count each kind of remote operation."""

    def __init__(self, records: list[DirectoryHospital] | None = None) -> None:
        """Keep upstream records independent from the local saved checkpoints."""
        self.records = records or []
        self.creations = 0
        self.lookups = 0
        self.activations = 0

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Simulate a confirmed creation only when a pending row is resumed."""
        self.creations += 1
        record = DirectoryHospital(
            100 + self.creations,
            hospital.name,
            hospital.address,
            batch_id,
            False,
            hospital.phone,
        )
        self.records.append(record)
        return record

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Return inspection evidence without creating another hospital."""
        self.lookups += 1
        return self.records.copy()

    async def activate_batch(self, batch_id: UUID) -> None:
        """Acknowledge activation while retaining the same confirmed IDs."""
        assert self.lookups > 0  # Interrupted activation must be inspected first.
        self.activations += 1
        self.records = [replace(record, active=True) for record in self.records]


@pytest.fixture
def local_clients(monkeypatch):
    """Reject any startup network request and retain clients for shutdown checks."""
    clients = []

    def reject_request(request: httpx.Request) -> httpx.Response:
        """Fail if startup or shutdown attempts an external operation."""
        raise AssertionError(f"Unexpected startup request: {request.method}")

    def client_factory(settings: Settings) -> httpx.AsyncClient:
        """Supply an application-owned client with no real network transport."""
        client = httpx.AsyncClient(
            base_url=settings.directory_api_base_url,
            transport=httpx.MockTransport(reject_request),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(main_module, "create_directory_client", client_factory)
    return clients


async def save_creations(repository: BatchRepository, batch_id: UUID) -> None:
    """Checkpoint all reference identities without making upstream requests."""
    for row in ROWS:
        assert await repository.start_creation(batch_id, row.row_number)
        assert await repository.record_row_outcome(
            batch_id, row.row_number, hospital_id=100 + row.row_number
        )


@pytest.mark.asyncio
async def test_restart_distinguishes_created_inflight_and_pending_and_excludes_downtime(
    tmp_path: Path,
    local_clients,
) -> None:
    """Only abandoned creation intent becomes unknown; saved duration is retained."""
    settings = Settings(sqlite_path=tmp_path / "restart.db")
    batch_id = uuid4()
    first = main_module.create_app(settings)
    async with first.router.lifespan_context(first):
        repository = first.state.repository
        await repository.register_batch(batch_id, ROWS)
        assert await repository.start_attempt(batch_id, started_at=START)
        assert await repository.start_creation(batch_id, 1)
        assert await repository.record_row_outcome(batch_id, 1, hospital_id=101)
        assert await repository.finish_attempt(
            batch_id, BatchState.FAILED, ended_at=START + timedelta(seconds=2)
        )
        assert await repository.claim_for_resume(batch_id)
        assert await repository.start_attempt(
            batch_id, started_at=START + timedelta(hours=1)
        )
        assert await repository.start_creation(batch_id, 2)

    second = main_module.create_app(settings)
    async with second.router.lifespan_context(second):
        repository = second.state.repository
        batch = await repository.get_batch(batch_id)
        assert batch.state == BatchState.INTERRUPTED and not batch.batch_activated
        assert [row.state for row in batch.rows] == [
            RowState.CREATED,
            RowState.UNKNOWN,
            RowState.PENDING,
        ]
        assert [row.hospital_id for row in batch.rows] == [101, None, None]
        assert [row.hospital for row in batch.rows] == [row.hospital for row in ROWS]
        assert batch.rows[1].failure_kind == FailureKind.UNCERTAIN
        assert batch.rows[0].failure_kind is batch.rows[2].failure_kind is None
        assert batch.processing_time_seconds == 2 and batch.attempt_started_at is None
        assert await repository.count_in_flight_batches() == 0
        assert await repository.recover_interrupted_batches() == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/hospitals/batch/{batch_id}/status")
        assert response.status_code == 200
        assert response.json()["processing_time_seconds"] == 2
        assert [row["status"] for row in response.json()["hospitals"]] == [
            "created",
            "unknown",
            "pending",
        ]
    assert len(local_clients) == 2 and all(client.is_closed for client in local_clients)


@pytest.mark.asyncio
async def test_never_started_batch_waits_for_manual_resume(
    tmp_path: Path, local_clients
) -> None:
    """A saved queued upload is recovered without startup automatically processing it."""
    settings = Settings(sqlite_path=tmp_path / "queued.db")
    batch_id = uuid4()
    first = main_module.create_app(settings)
    async with first.router.lifespan_context(first):
        await first.state.repository.register_batch(batch_id, ROWS)
    second = main_module.create_app(settings)
    async with second.router.lifespan_context(second):
        repository = second.state.repository
        batch = await repository.get_batch(batch_id)
        assert batch.state == BatchState.INTERRUPTED
        assert all(row.state == RowState.PENDING for row in batch.rows)
        assert batch.processing_time_seconds == 0
        directory = RecoveryDirectory()
        assert await repository.claim_for_resume(batch_id)
        await BatchProcessor(repository, directory).process_batch(
            batch_id, reconcile_before_processing=True
        )
        result = await repository.get_batch(batch_id)
        assert result.state == BatchState.COMPLETED and result.batch_activated
        assert directory.creations == 3 and directory.activations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("already_active", [False, True])
async def test_interrupted_activation_inspects_before_retry_and_never_recreates(
    tmp_path: Path,
    local_clients,
    already_active: bool,
) -> None:
    """A lost activation can complete from active evidence or retry after inspection."""
    settings = Settings(sqlite_path=tmp_path / "activation.db")
    batch_id = uuid4()
    first = main_module.create_app(settings)
    async with first.router.lifespan_context(first):
        repository = first.state.repository
        await repository.register_batch(batch_id, ROWS)
        assert await repository.start_attempt(batch_id, started_at=START)
        await save_creations(repository, batch_id)
        assert await repository.finish_attempt(
            batch_id, BatchState.FAILED, ended_at=START + timedelta(seconds=2)
        )
        assert await repository.claim_for_resume(batch_id)
        assert await repository.start_attempt(
            batch_id, started_at=START + timedelta(hours=1)
        )
        assert await repository.begin_activation(batch_id)
    second = main_module.create_app(settings)
    async with second.router.lifespan_context(second):
        repository = second.state.repository
        batch = await repository.get_batch(batch_id)
        assert batch.state == BatchState.INTERRUPTED
        assert all(row.state == RowState.CREATED for row in batch.rows)
        records = [
            DirectoryHospital(
                100 + row.row_number,
                row.hospital.name,
                row.hospital.address,
                batch_id,
                already_active,
                row.hospital.phone,
            )
            for row in ROWS
        ]
        directory = RecoveryDirectory(records)
        assert await repository.claim_for_resume(batch_id)
        times = iter((START + timedelta(days=7), START + timedelta(days=7, seconds=3)))
        await BatchProcessor(
            repository, directory, clock=lambda: next(times)
        ).process_batch(batch_id, reconcile_before_processing=True)
        result = await repository.get_batch(batch_id)
        assert result.state == BatchState.COMPLETED and result.batch_activated
        assert result.processing_time_seconds == 5
        assert [row.hospital_id for row in result.rows] == [101, 102, 103]
        assert directory.creations == 0 and directory.lookups >= 1
        assert directory.activations == int(not already_active)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [BatchState.COMPLETED, BatchState.FAILED, BatchState.RECONCILIATION_REQUIRED],
)
async def test_recovery_does_not_change_finished_or_paused_batches(
    repository, state
) -> None:
    """Startup targets only active states and leaves earlier saved outcomes intact."""
    batch_id = uuid4()
    await repository.register_batch(batch_id, ROWS)
    assert await repository.start_attempt(batch_id, started_at=START)
    if state == BatchState.COMPLETED:
        await save_creations(repository, batch_id)
        assert await repository.begin_activation(batch_id)
        assert await repository.complete_activation(
            batch_id, ended_at=START + timedelta(seconds=2)
        )
    else:
        assert await repository.finish_attempt(
            batch_id,
            state,
            error="Prior saved diagnosis",
            ended_at=START + timedelta(seconds=2),
        )
    before = await repository.get_batch(batch_id)
    assert await repository.recover_interrupted_batches() == 0
    assert await repository.get_batch(batch_id) == before
