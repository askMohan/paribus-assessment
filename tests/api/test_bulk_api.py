"""Upload acceptance, validation-only behavior, and exact saved-result polling."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

import app.api.routes as routes_module
from app.api.dependencies import get_processor
from app.config import Settings
from app.domain import BatchState, DirectoryHospital, FailureKind
from app.main import create_app
from app.services.csv_validation import validate_csv_bytes
from app.services.processor import BatchProcessor

CSV = (
    b"name,address,phone\nGeneral Hospital,123 Main Street,555-0101\n"
    b"City Care Hospital,45 Park Road,555-0102\nSunrise Hospital,8 Lake Road,555-0103\n"
)
ROWS = validate_csv_bytes(CSV, max_upload_bytes=1024)
NOW = datetime(2026, 11, 18, 10, tzinfo=UTC)
RESULT_FIELDS = {
    "batch_id",
    "total_hospitals",
    "processed_hospitals",
    "failed_hospitals",
    "processing_time_seconds",
    "batch_activated",
    "hospitals",
}
ROW_FIELDS = {"row", "hospital_id", "name", "status"}


class RecordingProcessor:
    """Observe scheduling and optionally pause background work after acceptance."""

    def __init__(self):
        """Each test controls its own task gate and list of accepted identifiers."""
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def process_batch(self, batch_id):
        """Record invocation after the response body has been sent."""
        self.calls.append(batch_id)
        self.entered.set()
        await self.release.wait()


class SuccessfulDirectory:
    """Create typed fake records so API tests exercise the real processor boundary."""

    def __init__(self):
        """Keep counters local; every operation is an in-memory fake."""
        self.created = []
        self.activations = []

    async def create_hospital(self, hospital, batch_id):
        """Return stable identities without an HTTP client or live Directory call."""
        number = next(row.row_number for row in ROWS if row.hospital == hospital)
        record = DirectoryHospital(
            hospital_id=100 + number,
            name=hospital.name,
            address=hospital.address,
            phone=hospital.phone,
            creation_batch_id=batch_id,
            active=False,
        )
        self.created.append(record)
        return record

    async def activate_batch(self, batch_id):
        """Acknowledge activation after the processor verifies the full manifest."""
        self.activations.append(batch_id)

    async def get_hospitals_by_batch(self, batch_id):
        """No successful request should need reconciliation."""
        raise AssertionError("Unexpected lookup on the successful path")


@pytest_asyncio.fixture
async def api(tmp_path: Path):
    """Run normal lifespan against temporary persistence and controlled processing."""
    application = create_app(Settings(sqlite_path=tmp_path / "api.db"))
    processor = RecordingProcessor()
    application.dependency_overrides[get_processor] = lambda: processor
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield application, client, processor


def upload(content=CSV):
    """Keep the multipart field name and CSV body consistent across API cases."""
    return {"file": ("hospitals.csv", content, "text/csv")}


async def seed_batch(repository, state):
    """Build each persisted polling stage through guarded repository operations."""
    batch_id = uuid4()
    await repository.register_batch(batch_id, ROWS)
    if state == BatchState.QUEUED:
        return batch_id
    assert await repository.start_attempt(
        batch_id, started_at=NOW - timedelta(seconds=10)
    )
    for number in (
        (1, 2, 3) if state in (BatchState.ACTIVATING, BatchState.COMPLETED) else (1,)
    ):
        assert await repository.start_creation(batch_id, number)
        assert await repository.record_row_outcome(
            batch_id, number, hospital_id=100 + number
        )
    if state in (BatchState.ACTIVATING, BatchState.COMPLETED):
        assert await repository.begin_activation(batch_id)
        if state == BatchState.COMPLETED:
            assert await repository.complete_activation(batch_id, ended_at=NOW)
    elif state in (BatchState.FAILED, BatchState.RECONCILIATION_REQUIRED):
        assert await repository.start_creation(batch_id, 2)
        kind = (
            FailureKind.UNCERTAIN
            if state == BatchState.RECONCILIATION_REQUIRED
            else FailureKind.RETRYABLE
        )
        assert await repository.record_row_outcome(
            batch_id, 2, failure_kind=kind, error="Internal diagnosis"
        )
        assert await repository.finish_attempt(
            batch_id, state, error="Private batch diagnosis", ended_at=NOW
        )
    return batch_id


@pytest.mark.asyncio
async def test_upload_saves_normalized_manifest_before_scheduling(api):
    """Only the UUID is public; registration and its complete input precede work."""
    application, client, processor = api
    response = await client.post("/hospitals/bulk", files=upload())
    assert response.status_code == 202 and set(response.json()) == {"batch_id"}
    batch_id = UUID(response.json()["batch_id"])
    assert response.headers["Location"] == f"/hospitals/batch/{batch_id}/status"
    assert processor.calls == [batch_id]
    batch = await application.state.repository.get_batch(batch_id)
    assert batch.state == BatchState.QUEUED and batch.rows == tuple(ROWS)


@pytest.mark.asyncio
async def test_202_body_is_sent_while_background_work_is_still_blocked(api):
    """Observe ASGI frames because HTTPX waits for background work before returning."""
    application, client, processor = api
    processor.release.clear()
    messages = []

    async def observed(scope, receive, send):
        """Capture response delivery while preserving the normal ASGI exchange."""

        async def capture(message):
            """Retain response frames before passing them to the HTTP transport."""
            messages.append(message)
            await send(message)

        await application(scope, receive, capture)

    async with AsyncClient(
        transport=ASGITransport(app=observed), base_url="http://test"
    ) as observed_client:
        task = asyncio.create_task(
            observed_client.post("/hospitals/bulk", files=upload())
        )
        try:
            await asyncio.wait_for(processor.entered.wait(), timeout=2)
            start = next(
                message
                for message in messages
                if message["type"] == "http.response.start"
            )
            body = b"".join(
                message.get("body", b"")
                for message in messages
                if message["type"] == "http.response.body"
            )
            assert start["status"] == 202 and not task.done()
            batch_id = UUID(json.loads(body)["batch_id"])
            batch = await application.state.repository.get_batch(batch_id)
            assert batch.state == BatchState.QUEUED and len(batch.rows) == 3
            polling = await client.get(f"/hospitals/batch/{batch_id}/status")
            assert (
                polling.status_code == 200
                and polling.json()["processed_hospitals"] == 0
            )
        finally:
            processor.release.set()
            await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        BatchState.QUEUED,
        BatchState.PROCESSING,
        BatchState.ACTIVATING,
        BatchState.COMPLETED,
        BatchState.FAILED,
        BatchState.RECONCILIATION_REQUIRED,
    ],
)
async def test_polling_projects_exact_fields_and_current_evidence(
    api, monkeypatch, state
):
    """Pending, partial, active, failed, and completed snapshots keep one contract."""
    application, client, _ = api
    monkeypatch.setattr(routes_module, "datetime", Mock(now=Mock(return_value=NOW)))
    batch_id = await seed_batch(application.state.repository, state)
    response = await client.get(f"/hospitals/batch/{batch_id}/status")
    assert response.status_code == 200
    result = response.json()
    assert set(result) == RESULT_FIELDS
    assert all(set(row) == ROW_FIELDS for row in result["hospitals"])
    assert [row["row"] for row in result["hospitals"]] == [1, 2, 3]
    assert result["processing_time_seconds"] == (
        0 if state == BatchState.QUEUED else 10
    )
    assert result["batch_activated"] == (state == BatchState.COMPLETED)
    assert result["processed_hospitals"] == (
        0
        if state == BatchState.QUEUED
        else 3
        if state in (BatchState.ACTIVATING, BatchState.COMPLETED)
        else 1
    )
    assert result["failed_hospitals"] == int(state == BatchState.FAILED)
    assert (
        "Internal diagnosis" not in response.text
        and "Private batch diagnosis" not in response.text
    )
    if state == BatchState.RECONCILIATION_REQUIRED:
        assert result["hospitals"][1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_polling_accumulates_attempts_but_excludes_idle_time(api, monkeypatch):
    """The current attempt adds to saved work, without adding a one-hour idle gap."""
    application, client, _ = api
    repository = application.state.repository
    batch_id = await seed_batch(repository, BatchState.FAILED)
    assert await repository.claim_for_resume(batch_id)
    assert await repository.start_attempt(batch_id, started_at=NOW + timedelta(hours=1))
    current = NOW + timedelta(hours=1, seconds=3)
    monkeypatch.setattr(routes_module, "datetime", Mock(now=Mock(return_value=current)))
    result = (await client.get(f"/hospitals/batch/{batch_id}/status")).json()
    assert result["processing_time_seconds"] == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/hospitals/bulk", "/hospitals/bulk/validate"])
async def test_invalid_csv_never_persists_or_schedules(api, path):
    """Both routes share the same all-or-nothing validator and structured errors."""
    application, client, processor = api
    response = await client.post(path, files=upload(b"name,address\nGeneral,\n"))
    assert response.status_code == 422 and isinstance(response.json()["detail"], list)
    assert response.json()["detail"][0]["row"] == 1
    assert await application.state.repository.count_in_flight_batches() == 0
    assert processor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/hospitals/bulk", "/hospitals/bulk/validate"])
async def test_oversized_file_returns_413_without_side_effects(api, path):
    """The configured file-byte cap applies to validation and accepted uploads."""
    application, client, processor = api
    application.state.settings = Settings(
        sqlite_path=application.state.settings.sqlite_path, max_upload_bytes=8
    )
    response = await client.post(path, files=upload())
    assert response.status_code == 413
    assert await application.state.repository.count_in_flight_batches() == 0
    assert processor.calls == []


@pytest.mark.asyncio
async def test_validation_only_is_empty_204_without_persistence_or_processing(api):
    """A successful dry run creates neither local batches nor upstream hospitals."""
    application, client, processor = api
    response = await client.post("/hospitals/bulk/validate", files=upload())
    assert response.status_code == 204 and response.content == b""
    assert await application.state.repository.count_in_flight_batches() == 0
    assert processor.calls == []


@pytest.mark.asyncio
async def test_full_admission_budget_rejects_upload_but_allows_validation(api):
    """Capacity is checked atomically at registration and does not block dry runs."""
    application, client, processor = api
    application.state.settings = Settings(
        sqlite_path=application.state.settings.sqlite_path, max_in_flight_batches=1
    )
    await application.state.repository.register_batch(uuid4(), ROWS)
    response = await client.post("/hospitals/bulk", files=upload())
    assert response.status_code == 503 and processor.calls == []
    assert await application.state.repository.count_in_flight_batches() == 1
    assert (
        await client.post("/hospitals/bulk/validate", files=upload())
    ).status_code == 204


@pytest.mark.asyncio
async def test_failed_registration_cannot_accept_or_schedule_a_batch(api, monkeypatch):
    """Storage errors return a generic response instead of an unusable 202 UUID."""
    application, client, processor = api

    async def fail_registration(*args, **kwargs):
        """Simulate a database failure whose private text must remain internal."""
        raise SQLAlchemyError("Private database connection information")

    monkeypatch.setattr(
        application.state.repository, "register_batch", fail_registration
    )
    response = await client.post("/hospitals/bulk", files=upload())
    assert response.status_code == 500 and "Private" not in response.text
    assert processor.calls == [] and "batch_id" not in response.json()


@pytest.mark.asyncio
async def test_lookup_failure_is_generic_and_missing_or_invalid_uuids_are_rejected(
    api, monkeypatch
):
    """Only existing UUIDs produce comprehensive results; storage diagnoses stay private."""
    application, client, _ = api
    assert (await client.get(f"/hospitals/batch/{uuid4()}/status")).status_code == 404
    assert (await client.get("/hospitals/batch/not-a-uuid/status")).status_code == 422

    async def fail_lookup(batch_id):
        """Keep storage exception details inside the application's error log."""
        raise SQLAlchemyError("Private database connection information")

    monkeypatch.setattr(application.state.repository, "get_batch", fail_lookup)
    response = await client.get(f"/hospitals/batch/{uuid4()}/status")
    assert response.status_code == 500 and "Private" not in response.text


@pytest.mark.asyncio
async def test_multipart_requires_the_documented_file_field(api):
    """Reject missing and incorrectly named file parts through normal API validation."""
    _, client, processor = api
    assert (await client.post("/hospitals/bulk")).status_code == 422
    assert (
        await client.post("/hospitals/bulk", files={"csv": ("test.csv", CSV)})
    ).status_code == 422
    assert processor.calls == []


@pytest.mark.asyncio
async def test_upload_through_real_processor_with_injected_fake_directory(api):
    """Successful end-to-end API processing uses the adapter interface and exact results."""
    application, client, _ = api
    directory = SuccessfulDirectory()
    processor = BatchProcessor(application.state.repository, directory)
    application.dependency_overrides[get_processor] = lambda: processor
    accepted = await client.post("/hospitals/bulk", files=upload())
    result = (await client.get(accepted.headers["Location"])).json()
    assert result["batch_activated"] and result["processed_hospitals"] == 3
    assert result["failed_hospitals"] == 0
    assert [row["hospital_id"] for row in result["hospitals"]] == [101, 102, 103]
    assert all(row["status"] == "created_and_activated" for row in result["hospitals"])
    assert len(directory.created) == 3 and directory.activations == [
        UUID(result["batch_id"])
    ]
