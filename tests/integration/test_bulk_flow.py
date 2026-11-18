"""Full API, ORM, processor, and concrete HTTPX adapter scenario verification."""

import asyncio
import socket
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models import BatchModel, HospitalRowModel
from app.domain import BatchState, DirectoryOperationError, RowState
from app.services.csv_validation import validate_csv_bytes
from tests.simulated_directory import (
    SimulatedDirectory,
    directory_application,
    hospital_csv,
)


@pytest_asyncio.fixture
async def system(tmp_path):
    """Keep actual application services and replace only the upstream transport."""
    directory = SimulatedDirectory()
    with directory_application(
        directory, Settings(sqlite_path=tmp_path / "integration.db")
    ) as app:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            yield app, directory, client


async def wait_for(predicate: Callable[[], bool]) -> None:
    """Wait for a controlled state, with a bound instead of a speedup assertion."""
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def poll(
    client: httpx.AsyncClient, batch_id: str, predicate: Callable[[dict], bool]
) -> dict:
    """Observe persisted public progress while another request runs in background."""
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/hospitals/batch/{batch_id}/status")
            assert response.status_code == 200
            result = response.json()
            if predicate(result):
                return result
            await asyncio.sleep(0.005)


def check_schema(result: dict) -> None:
    """Keep full integration responses to the agreed seven and four fields."""
    assert set(result) == {
        "batch_id",
        "total_hospitals",
        "processed_hospitals",
        "failed_hospitals",
        "processing_time_seconds",
        "batch_activated",
        "hospitals",
    }
    assert all(
        set(row) == {"row", "hospital_id", "name", "status"}
        for row in result["hospitals"]
    )


def post_names(directory: SimulatedDirectory) -> Counter:
    """Count actual creation attempts rather than just successful remote records."""
    return Counter(name for method, _, name in directory.requests if method == "POST")


@pytest.mark.asyncio
async def test_intermediate_polling_then_success_uses_concrete_adapter(system):
    """Checkpoint General while City/Sunrise are held, then activate the manifest."""
    _, directory, client = system
    directory.held_names.update({"Hospital 2", "Hospital 3"})
    task = asyncio.create_task(
        client.post("/hospitals/bulk", files={"file": ("input.csv", hospital_csv())})
    )
    try:
        await wait_for(lambda: bool(directory.requests))
        batch_id = directory.requests[0][1]
        result = await poll(
            client, batch_id, lambda value: value["processed_hospitals"] == 1
        )
        check_schema(result)
        assert not result["batch_activated"] and result["failed_hospitals"] == 0
        assert [row["row"] for row in result["hospitals"]] == [1, 2, 3]
        assert result["hospitals"][0]["hospital_id"] is not None
        assert all(row["hospital_id"] is None for row in result["hospitals"][1:])
    finally:
        directory.release.set()
        response = await asyncio.wait_for(task, timeout=5)
    assert response.status_code == 202 and response.json() == {"batch_id": batch_id}
    result = (await client.get(response.headers["Location"])).json()
    check_schema(result)
    assert result["batch_activated"] and result["processed_hospitals"] == 3
    assert all(row["status"] == "created_and_activated" for row in result["hospitals"])
    assert post_names(directory) == {"Hospital 1": 1, "Hospital 2": 1, "Hospital 3": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["connect", "reject", "lost_reply", "unresolved", 500, 429]
)
async def test_creation_failures_and_resume_cross_every_layer(system, fault):
    """Exercise safe retries, permanent rejection, and uncertain committed writes."""
    app, directory, client = system
    directory.creation_faults["Hospital 2"] = fault
    accepted = await client.post(
        "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
    )
    assert accepted.status_code == 202
    batch_id = accepted.json()["batch_id"]
    result = (await client.get(accepted.headers["Location"])).json()
    check_schema(result)
    expected_attempts = 3 if fault == "connect" else 1
    assert post_names(directory)["Hospital 2"] == expected_attempts
    if fault in ("lost_reply", 500, 429):
        assert result["batch_activated"] and len(directory.records) == 3
        assert (
            await app.state.repository.get_batch(UUID(batch_id))
        ).state == BatchState.COMPLETED
        return
    assert not result["batch_activated"] and result["processed_hospitals"] == 2
    assert result["failed_hospitals"] == int(fault != "unresolved")
    retained = {
        row["row"]: row["hospital_id"]
        for row in result["hospitals"]
        if row["hospital_id"] is not None
    }
    if fault == "connect":
        directory.creation_faults.clear()
    resumed = await client.post(f"/hospitals/batch/{batch_id}/resume")
    assert resumed.status_code == (409 if fault == "reject" else 202)
    result = (await client.get(accepted.headers["Location"])).json()
    assert all(
        result["hospitals"][row - 1]["hospital_id"] == identifier
        for row, identifier in retained.items()
    )
    assert (
        post_names(directory)["Hospital 1"] == post_names(directory)["Hospital 3"] == 1
    )
    assert post_names(directory)["Hospital 2"] == expected_attempts + int(
        fault == "connect"
    )
    assert result["batch_activated"] == (fault == "connect")
    if fault == "unresolved":
        assert (
            result["hospitals"][1]["status"] == "unknown"
            and result["failed_hospitals"] == 0
        )


@pytest.mark.asyncio
async def test_failed_reconciliation_get_has_bounded_retries_and_resumes_without_post(
    system,
):
    """Failed reads preserve uncertainty even when City already exists upstream."""
    _, directory, client = system
    directory.creation_faults["Hospital 2"] = "lost_reply"
    directory.lookup_status = 503
    accepted = await client.post(
        "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
    )
    batch_id = accepted.json()["batch_id"]
    result = (await client.get(accepted.headers["Location"])).json()
    assert result["hospitals"][1]["status"] == "unknown"
    assert sum(method == "GET" for method, _, _ in directory.requests) == 3
    directory.lookup_status = 200
    assert (await client.post(f"/hospitals/batch/{batch_id}/resume")).status_code == 202
    result = (await client.get(accepted.headers["Location"])).json()
    assert result["batch_activated"] and len(directory.records) == 3
    assert all(count == 1 for count in post_names(directory).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["connect", "lost_reply"])
async def test_activation_failure_or_lost_reply_preserves_creations(system, fault):
    """Resume activation only, or confirm its lost response through a GET."""
    _, directory, client = system
    directory.activation_fault = fault
    accepted = await client.post(
        "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
    )
    result = (await client.get(accepted.headers["Location"])).json()
    identifiers = [row["hospital_id"] for row in result["hospitals"]]
    assert result["processed_hospitals"] == 3 and result["failed_hospitals"] == 0
    if fault == "connect":
        assert not result["batch_activated"]
        directory.activation_fault = None
        assert (
            await client.post(f"/hospitals/batch/{accepted.json()['batch_id']}/resume")
        ).status_code == 202
    result = (await client.get(accepted.headers["Location"])).json()
    assert (
        result["batch_activated"]
        and [row["hospital_id"] for row in result["hospitals"]] == identifiers
    )
    assert sum(post_names(directory).values()) == len(directory.records) == 3
    assert sum(method == "PATCH" for method, _, _ in directory.requests) == (
        4 if fault == "connect" else 1
    )


@pytest.mark.asyncio
async def test_identical_unknown_rows_reconcile_by_quantity_without_repeating_posts(
    system,
):
    """Three identical committed rows need three distinct recovered identities."""
    _, directory, client = system
    directory.creation_faults["Hospital 1"] = "lost_reply"
    accepted = await client.post(
        "/hospitals/bulk", files={"file": ("input.csv", hospital_csv(identical=True))}
    )
    result = (await client.get(accepted.headers["Location"])).json()
    assert result["batch_activated"] and result["processed_hospitals"] == 3
    assert len({row["hospital_id"] for row in result["hospitals"]}) == 3
    assert post_names(directory) == {"Hospital 1": 3}


@pytest.mark.asyncio
async def test_batches_share_request_budget_and_admission_without_cross_upload_deduplication(
    system,
):
    """Two identical uploads are independent batches under one global semaphore."""
    app, directory, client = system
    app.state.settings = replace(app.state.settings, max_in_flight_batches=2)
    directory.held_names.update({"Hospital 1", "Hospital 2", "Hospital 3"})
    tasks = [
        asyncio.create_task(
            client.post(
                "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
            )
        )
        for _ in range(2)
    ]
    try:
        await wait_for(
            lambda: (
                directory.active == 5
                and len({batch for _, batch, _ in directory.requests}) == 2
            )
        )
        assert directory.peak_active == 5
        rejected = await client.post(
            "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
        )
        assert (
            rejected.status_code == 503
            and await app.state.repository.count_in_flight_batches() == 2
        )
    finally:
        directory.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    batch_ids = {response.json()["batch_id"] for response in responses}
    assert len(batch_ids) == 2 and all(
        response.status_code == 202 for response in responses
    )
    assert directory.peak_active == 5 and directory.active == 0
    assert len(directory.records) == sum(post_names(directory).values()) == 6
    for response in responses:
        result = (await client.get(response.headers["Location"])).json()
        assert result["batch_activated"] and result["processed_hospitals"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_failure", [False, True])
async def test_rejected_input_or_registration_failure_has_no_side_effects(
    system, registration_failure
):
    """Concrete upstream transport must receive nothing when acceptance fails."""
    app, directory, client = system

    def reject_second_row(mapper, connection, row):
        """Force a real flush failure after the batch registration has started."""
        if row.row_number == 2:
            raise SQLAlchemyError("Private registration failure")

    if registration_failure:
        event.listen(HospitalRowModel, "before_insert", reject_second_row)
    try:
        content = (
            hospital_csv() if registration_failure else b"name,address\n,Missing Name\n"
        )
        response = await client.post(
            "/hospitals/bulk", files={"file": ("input.csv", content)}
        )
    finally:
        if registration_failure:
            event.remove(HospitalRowModel, "before_insert", reject_second_row)
    assert response.status_code == (500 if registration_failure else 422)
    assert "Private" not in response.text and directory.requests == []
    async with app.state.repository.database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(BatchModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(HospitalRowModel))
            == 0
        )


@pytest.mark.asyncio
async def test_restart_recovers_lost_creation_through_real_adapter_and_resume(tmp_path):
    """Emulate termination between remote commitment and a local checkpoint."""
    settings = Settings(sqlite_path=tmp_path / "restart.db")
    directory = SimulatedDirectory()
    rows = validate_csv_bytes(hospital_csv(), max_upload_bytes=1024)
    batch_id = uuid4()
    with directory_application(directory, settings) as first:
        async with first.router.lifespan_context(first):
            repository = first.state.repository
            await repository.register_batch(batch_id, rows)
            assert await repository.start_attempt(batch_id)
            for row in rows[:2]:
                assert await repository.start_creation(batch_id, row.row_number)
                if row.row_number == 2:
                    directory.creation_faults[row.hospital.name] = "lost_reply"
                    with pytest.raises(DirectoryOperationError):
                        await first.state.directory_adapter.create_hospital(
                            row.hospital, batch_id
                        )
                else:
                    record = await first.state.directory_adapter.create_hospital(
                        row.hospital, batch_id
                    )
                    assert await repository.record_row_outcome(
                        batch_id, row.row_number, hospital_id=record.hospital_id
                    )
    with directory_application(directory, settings) as second:
        async with (
            second.router.lifespan_context(second),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second), base_url="http://test"
            ) as client,
        ):
            batch = await second.state.repository.get_batch(batch_id)
            assert batch.state == BatchState.INTERRUPTED
            assert [row.state for row in batch.rows] == [
                RowState.CREATED,
                RowState.UNKNOWN,
                RowState.PENDING,
            ]
            assert len(directory.requests) == 2  # Startup sends no upstream request.
            accepted = await client.post(f"/hospitals/batch/{batch_id}/resume")
            assert accepted.status_code == 202
            result = (await client.get(accepted.headers["Location"])).json()
            assert result["batch_activated"] and result["processed_hospitals"] == 3
    assert post_names(directory) == {"Hospital 1": 1, "Hospital 2": 1, "Hospital 3": 1}


@asynccontextmanager
async def serve(app):
    """Use loopback HTTP for actual disconnect behavior; lifespan is already owned."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            log_level="critical",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await wait_for(lambda: server.started or task.done())
        if task.done():
            await task
            raise RuntimeError("HTTP server stopped before startup")
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()


@pytest.mark.asyncio
async def test_disconnecting_after_202_does_not_cancel_accepted_work(system):
    """A retained UUID can be polled from a new connection after upload disconnect."""
    app, directory, _ = system
    directory.held_names.update({"Hospital 1", "Hospital 2", "Hospital 3"})
    async with serve(app) as base_url:
        try:
            async with httpx.AsyncClient(base_url=base_url) as upload_client:
                accepted = await upload_client.post(
                    "/hospitals/bulk", files={"file": ("input.csv", hospital_csv())}
                )
                assert accepted.status_code == 202
                batch_id = accepted.json()["batch_id"]
            # The upload connection is now closed while creations remain gated.
            async with httpx.AsyncClient(base_url=base_url) as polling_client:
                result = await poll(
                    polling_client,
                    batch_id,
                    lambda value: (
                        value["processed_hospitals"] == 0
                        and all(
                            row["status"] == "processing" for row in value["hospitals"]
                        )
                    ),
                )
                assert all(row["status"] == "processing" for row in result["hospitals"])
                batch = await app.state.repository.get_batch(UUID(batch_id))
                assert batch.state == BatchState.PROCESSING
                directory.release.set()
                result = await poll(
                    polling_client, batch_id, lambda value: value["batch_activated"]
                )
                assert result["processed_hospitals"] == 3
        finally:
            directory.release.set()
    assert len(directory.records) == sum(post_names(directory).values()) == 3
