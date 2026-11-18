"""Manual resume preserves identity, recorded outcomes, and safe operation choice."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.domain import (
    BatchState,
    DirectoryHospital,
    FailureKind,
    HospitalInput,
)
from app.main import create_app
from app.services.csv_validation import validate_csv_bytes
from app.services.processor import BatchProcessor

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
START = datetime(2026, 11, 18, 10, tzinfo=UTC)
ROWS = validate_csv_bytes(
    b"name,address,phone\nGeneral Hospital,123 Main Street,555-0101\n"
    b"City Care Hospital,45 Park Road,555-0102\nSunrise Hospital,8 Lake Road,555-0103\n",
    max_upload_bytes=1024,
)
PATH = f"/hospitals/batch/{BATCH_ID}/resume"


class Clock:
    """Advance only during simulated work, with a long idle gap before resume."""

    def __init__(self) -> None:
        """Start four hours after the seeded attempt ended."""
        self.current = START + timedelta(hours=4)

    def __call__(self) -> datetime:
        """Provide an aware, controllable processor clock."""
        return self.current

    def advance(self) -> None:
        """Charge one second per simulated remote operation."""
        self.current += timedelta(seconds=1)


def remote_hospital(number: int, *, active: bool = False) -> DirectoryHospital:
    """Build consistent upstream evidence for an original CSV position."""
    hospital = ROWS[number - 1].hospital
    return DirectoryHospital(
        hospital_id=100 + number,
        name=hospital.name,
        address=hospital.address,
        phone=hospital.phone,
        creation_batch_id=BATCH_ID,
        active=active,
    )


class FakeDirectory:
    """Track writes and supply batch evidence without real HTTP requests."""

    def __init__(self, clock: Clock) -> None:
        """Keep call history, remote records, and an optional creation gate."""
        self.clock = clock
        self.records: list[DirectoryHospital] = []
        self.creations: list[int] = []
        self.activations: list[UUID] = []
        self.lookups: list[UUID] = []
        self.on_create: Callable[[], Awaitable[None]] | None = None

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Create one previously missing hospital and retain its batch association."""
        assert batch_id == BATCH_ID
        number = next(row.row_number for row in ROWS if row.hospital == hospital)
        self.creations.append(number)
        if self.on_create is not None:
            await self.on_create()
        self.clock.advance()
        record = remote_hospital(number)
        self.records.append(record)
        return record

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Read controlled evidence before any uncertain mutation is replayed."""
        self.lookups.append(batch_id)
        self.clock.advance()
        return list(self.records)

    async def activate_batch(self, batch_id: UUID) -> None:
        """Activate the retained records after the complete manifest is confirmed."""
        self.activations.append(batch_id)
        self.clock.advance()
        self.records = [replace(record, active=True) for record in self.records]


@pytest_asyncio.fixture
async def application(tmp_path):
    """Wire the real API and repository to a typed fake and a deterministic clock."""
    app = create_app(Settings(sqlite_path=tmp_path / "resume.db"))
    async with app.router.lifespan_context(app):
        clock = Clock()
        directory = FakeDirectory(clock)
        app.state.directory_adapter = directory
        app.state.processor = BatchProcessor(
            app.state.repository, directory, clock=clock
        )
        yield app, directory


async def seed_paused(
    application, *, city_failure: FailureKind | None = FailureKind.RETRYABLE
) -> None:
    """Retain General/Sunrise IDs and pause with City's chosen creation outcome."""
    app, directory = application
    repository = app.state.repository
    await repository.register_batch(BATCH_ID, ROWS)
    assert await repository.start_attempt(BATCH_ID, started_at=START)
    for number in (1, 2, 3):
        assert await repository.start_creation(BATCH_ID, number)
        if number == 2 and city_failure is not None:
            assert await repository.record_row_outcome(
                BATCH_ID,
                number,
                failure_kind=city_failure,
                error="Controlled creation failure",
            )
        else:
            assert await repository.record_row_outcome(
                BATCH_ID, number, hospital_id=100 + number
            )
            directory.records.append(remote_hospital(number))
    state = (
        BatchState.RECONCILIATION_REQUIRED
        if city_failure == FailureKind.UNCERTAIN
        else BatchState.FAILED
    )
    assert await repository.finish_attempt(
        BATCH_ID, state, ended_at=START + timedelta(seconds=2)
    )


@pytest.mark.asyncio
async def test_resume_only_creates_safe_missing_row_and_excludes_idle_time(
    application,
) -> None:
    """Keep successful IDs and the same batch UUID while retrying only City Care."""
    await seed_paused(application)
    app, directory = application
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(PATH)
        assert response.status_code == 202 and response.json() == {
            "batch_id": str(BATCH_ID)
        }
        assert response.headers["Location"] == f"/hospitals/batch/{BATCH_ID}/status"
        result = (await client.get(response.headers["Location"])).json()
        assert (await client.post(PATH)).status_code == 409
    assert directory.creations == [2] and directory.activations == [BATCH_ID]
    assert directory.lookups == []
    assert [row["hospital_id"] for row in result["hospitals"]] == [101, 102, 103]
    assert result["processed_hospitals"] == 3 and result["failed_hospitals"] == 0
    assert result["processing_time_seconds"] == 4 and result["batch_activated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_activation_only_resume_reads_first_without_creating_hospitals(
    application, active
) -> None:
    """Confirm lost activation or perform the remaining activation after evidence."""
    await seed_paused(application, city_failure=None)
    app, directory = application
    directory.records = [replace(record, active=active) for record in directory.records]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(PATH)
    assert response.status_code == 202
    assert directory.creations == [] and directory.lookups == [BATCH_ID]
    assert directory.activations == ([] if active else [BATCH_ID])
    batch = await app.state.repository.get_batch(BATCH_ID)
    assert batch.batch_activated and batch.failed_hospitals == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_creation_resume_reads_and_never_repeats_the_post(
    application, committed
) -> None:
    """Recover a lost creation response, or keep missing evidence unresolved."""
    await seed_paused(application, city_failure=FailureKind.UNCERTAIN)
    app, directory = application
    if committed:
        directory.records.append(remote_hospital(2))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(PATH)
    assert response.status_code == 202
    assert directory.lookups == [BATCH_ID] and directory.creations == []
    assert directory.activations == ([BATCH_ID] if committed else [])
    batch = await app.state.repository.get_batch(BATCH_ID)
    assert batch.state == (
        BatchState.COMPLETED if committed else BatchState.RECONCILIATION_REQUIRED
    )
    assert batch.failed_hospitals == 0 and batch.attempt_started_at is None


@pytest.mark.asyncio
async def test_permanent_only_failure_cannot_be_claimed_or_scheduled(
    application,
) -> None:
    """Reject unchanged rejected input at both HTTP and repository boundaries."""
    await seed_paused(application, city_failure=FailureKind.PERMANENT)
    app, directory = application
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(PATH)).status_code == 409
    assert not await app.state.repository.claim_for_resume(BATCH_ID)
    assert directory.creations == directory.activations == directory.lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        BatchState.QUEUED,
        BatchState.PROCESSING,
        BatchState.ACTIVATING,
        BatchState.COMPLETED,
    ],
)
async def test_active_and_completed_batches_conflict(application, state) -> None:
    """A second caller cannot resume already scheduled, running, or completed work."""
    await seed_paused(application, city_failure=None)
    app, directory = application
    repository = app.state.repository
    assert await repository.claim_for_resume(BATCH_ID)
    if state != BatchState.QUEUED:
        assert await repository.start_attempt(BATCH_ID)
    if state in (BatchState.ACTIVATING, BatchState.COMPLETED):
        assert await repository.begin_activation(BATCH_ID)
    if state == BatchState.COMPLETED:
        assert await repository.complete_activation(BATCH_ID)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(PATH)).status_code == 409
    assert directory.creations == directory.activations == directory.lookups == []


@pytest.mark.asyncio
async def test_missing_and_invalid_identifiers_use_standard_errors(application) -> None:
    """Distinguish an unknown UUID from malformed route input."""
    app, _ = application
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(f"/hospitals/batch/{uuid4()}/resume")
        ).status_code == 404
        assert (
            await client.post("/hospitals/batch/not-a-uuid/resume")
        ).status_code == 422


@pytest.mark.asyncio
async def test_simultaneous_resume_requests_have_one_winner(application) -> None:
    """The persisted claim makes simultaneous scheduling exactly once."""
    await seed_paused(application)
    app, directory = application
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate() -> None:
        """Hold the accepted request's background work while the other caller races."""
        entered.set()
        await release.wait()

    directory.on_create = gate
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(client.post(PATH))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await client.post(PATH)
            assert second.status_code == 409
        finally:
            release.set()
        assert (await asyncio.wait_for(first, timeout=2)).status_code == 202
    assert directory.creations == [2] and directory.activations == [BATCH_ID]


@pytest.mark.asyncio
async def test_resume_rechecks_capacity_before_claiming(application) -> None:
    """A paused batch cannot bypass the admission bound when work resumes."""
    await seed_paused(application)
    app, directory = application
    app.state.settings = replace(app.state.settings, max_in_flight_batches=1)
    await app.state.repository.register_batch(uuid4(), ROWS)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post(PATH)).status_code == 503
    assert (await app.state.repository.get_batch(BATCH_ID)).state == BatchState.FAILED
    assert directory.creations == directory.activations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_batch", "claim_for_resume"])
async def test_storage_errors_do_not_accept_or_expose_internal_details(
    application, monkeypatch, operation
) -> None:
    """An unregistered recovery task must not run after a failed storage operation."""
    await seed_paused(application)
    app, directory = application

    async def fail(*args, **kwargs):
        """Replace the selected storage call with an internal driver failure."""
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(app.state.repository, operation, fail)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(PATH)
    assert response.status_code == 500 and "private" not in response.text
    assert directory.creations == directory.activations == directory.lookups == []


@pytest.mark.asyncio
async def test_interrupted_creation_resumes_through_http_without_duplicate_posts(
    tmp_path,
) -> None:
    """Integrate restart detection, lookup recovery, safe remaining work, and polling."""
    settings = Settings(sqlite_path=tmp_path / "interrupted.db")
    first = create_app(settings)
    async with first.router.lifespan_context(first):
        repository = first.state.repository
        await repository.register_batch(BATCH_ID, ROWS)
        assert await repository.start_attempt(BATCH_ID, started_at=START)
        assert await repository.start_creation(BATCH_ID, 1)
        assert await repository.record_row_outcome(BATCH_ID, 1, hospital_id=101)
        assert await repository.start_creation(BATCH_ID, 2)
        # City's POST committed remotely but its local success was never saved.

    second = create_app(settings)
    async with second.router.lifespan_context(second):
        repository = second.state.repository
        batch = await repository.get_batch(BATCH_ID)
        assert batch.state == BatchState.INTERRUPTED
        clock = Clock()
        directory = FakeDirectory(clock)
        directory.records = [remote_hospital(1), remote_hospital(2)]
        second.state.processor = BatchProcessor(repository, directory, clock=clock)
        async with AsyncClient(
            transport=ASGITransport(app=second), base_url="http://test"
        ) as client:
            accepted = await client.post(PATH)
            assert accepted.status_code == 202
            result = (await client.get(accepted.headers["Location"])).json()
        assert directory.creations == [3]
        assert directory.lookups == directory.activations == [BATCH_ID]
        assert [row["hospital_id"] for row in result["hospitals"]] == [101, 102, 103]
        assert result["batch_activated"] and result["processing_time_seconds"] == 3
