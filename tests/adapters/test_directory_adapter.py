"""Directory contract, safe replay, concurrency, deadline, and lifecycle checks."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

import app.main as main_module
from app.adapters.httpx_hospital_directory import (
    HttpxHospitalDirectoryAdapter,
    _retry_after_seconds,
    create_directory_client,
)
from app.config import Settings
from app.domain import (
    DirectoryHospital,
    DirectoryOperationError,
    FailureEvidence,
    FailureKind,
    HospitalInput,
)
from app.services.processor import BatchProcessor

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
HOSPITAL = HospitalInput("General Hospital", "123 Main Street", "555-0101")


def payload(**changes) -> dict:
    """Build remote evidence while letting cases change one contract property."""
    return {
        "id": 101,
        "name": HOSPITAL.name,
        "address": HOSPITAL.address,
        "phone": HOSPITAL.phone,
        "creation_batch_id": str(BATCH_ID),
        "active": False,
        "created_at": "2026-11-18T10:00:00Z",
        **changes,
    }


async def no_wait(delay: float) -> None:
    """Avoid real retry delays while preserving the adapter's awaitable boundary."""


@asynccontextmanager
async def adapter_context(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    settings: Settings | None = None,
    sleep: Callable[[float], Awaitable[None]] = no_wait,
) -> AsyncIterator[HttpxHospitalDirectoryAdapter]:
    """Share realistic client configuration while mocking all external traffic."""
    settings = settings if settings is not None else Settings()
    async with create_directory_client(
        settings, transport=httpx.MockTransport(handler)
    ) as client:
        yield HttpxHospitalDirectoryAdapter(
            client,
            settings,
            asyncio.Semaphore(settings.max_concurrent_requests),
            sleep=sleep,
        )


@pytest.mark.asyncio
async def test_create_sends_exact_contract_and_translates_typed_evidence() -> None:
    """Check payload fields/path and preserve phone null without an active input flag."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Echo controlled input without making an actual Directory connection."""
        requests.append(request)
        data = json.loads(request.content)
        return httpx.Response(200, json=payload(phone=data["phone"]))

    async with adapter_context(handler) as adapter:
        result = await adapter.create_hospital(HOSPITAL, BATCH_ID)
        optional = await adapter.create_hospital(
            HospitalInput(HOSPITAL.name, HOSPITAL.address), BATCH_ID
        )
    assert isinstance(result, DirectoryHospital)
    assert result.hospital_id == 101 and result.creation_batch_id == BATCH_ID
    assert result.active is False and optional.phone is None
    assert requests[0].method == "POST" and requests[0].url.path == "/hospitals/"
    assert json.loads(requests[0].content) == {
        "name": HOSPITAL.name,
        "address": HOSPITAL.address,
        "phone": HOSPITAL.phone,
        "creation_batch_id": str(BATCH_ID),
    }
    assert json.loads(requests[1].content)["phone"] is None


@pytest.mark.asyncio
async def test_factory_uses_configured_base_url_and_socket_timeouts() -> None:
    """Inspect request-level timeout metadata rather than private pool internals."""
    settings = Settings(
        directory_api_base_url="https://directory.example.test",
        http_connect_timeout_seconds=2,
        http_read_timeout_seconds=3,
        http_write_timeout_seconds=4,
        http_pool_timeout_seconds=1,
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Retain the actual configured request for assertions."""
        requests.append(request)
        return httpx.Response(200, json=[])

    async with adapter_context(handler, settings=settings) as adapter:
        assert await adapter.get_hospitals_by_batch(BATCH_ID) == []
    assert requests[0].url.host == "directory.example.test"
    assert requests[0].extensions["timeout"] == {
        "connect": 2,
        "read": 3,
        "write": 4,
        "pool": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_known_unsent_creation_retries_within_one_budget(error_type) -> None:
    """Retry only transport evidence that the creation was not transmitted."""
    calls, delays = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail two known-unsent attempts before confirming one creation."""
        calls.append(request)
        if len(calls) < 3:
            raise error_type("Not sent", request=request)
        return httpx.Response(200, json=payload())

    async def sleep(delay: float) -> None:
        """Capture backoff without delaying the test."""
        delays.append(delay)

    async with adapter_context(handler, sleep=sleep) as adapter:
        assert (await adapter.create_hospital(HOSPITAL, BATCH_ID)).hospital_id == 101
    assert len(calls) == 3 and len(delays) == 2
    assert 0 <= delays[0] <= 0.5 and 0 <= delays[1] <= 1.0


@pytest.mark.asyncio
async def test_exhausted_connect_budget_raises_domain_error_without_httpx_cause() -> (
    None
):
    """Stop safe attempts at the configured limit and preserve their classification."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate an unavailable connection, never a possibly committed POST."""
        calls.append(request)
        raise httpx.ConnectError("Offline", request=request)

    async with adapter_context(handler) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.create_hospital(HOSPITAL, BATCH_ID)
    assert len(calls) == 3
    assert caught.value.evidence == FailureEvidence.NOT_SENT
    assert caught.value.failure_kind == FailureKind.RETRYABLE
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "activate"])
@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError]
)
async def test_uncertain_writes_are_never_automatically_replayed(
    operation, error_type
) -> None:
    """A failed exchange may follow remote commit, for activation as well as creation."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Raise uncertain transport evidence for the only allowed attempt."""
        calls.append(request)
        raise error_type("Unconfirmed reply", request=request)

    async with adapter_context(handler) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            if operation == "create":
                await adapter.create_hospital(HOSPITAL, BATCH_ID)
            else:
                await adapter.activate_batch(BATCH_ID)
    assert len(calls) == 1
    assert caught.value.failure_kind == FailureKind.UNCERTAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [422, 500, 429, 307, 202])
async def test_mutation_status_errors_do_not_cause_another_post(status: int) -> None:
    """Separate explicit validation rejection from ambiguous/unsupported outcomes."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return controlled status, including a redirect that must not be followed."""
        requests.append(request)
        return httpx.Response(
            status,
            json={"detail": "Rejected"},
            headers={"Location": "/other", "Retry-After": "2"},
        )

    async with adapter_context(handler) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.create_hospital(HOSPITAL, BATCH_ID)
    assert len(requests) == 1
    assert caught.value.failure_kind == (
        FailureKind.PERMANENT if status == 422 else FailureKind.UNCERTAIN
    )
    assert caught.value.retry_after_seconds == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": "101"},
        {"active": True},
        {"active": "false"},
        {"creation_batch_id": str(uuid4())},
        {"creation_batch_id": None},
        {"name": "Different"},
        {"address": "Different"},
        {"phone": "Different"},
    ],
)
async def test_bad_create_evidence_is_uncertain_without_replay(changes: dict) -> None:
    """A successful status cannot override identity/type/inactivity mismatches."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one malformed/mismatching successful response."""
        calls.append(request)
        return httpx.Response(200, json=payload(**changes))

    async with adapter_context(handler) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.create_hospital(HOSPITAL, BATCH_ID)
    assert len(calls) == 1 and caught.value.failure_kind == FailureKind.UNCERTAIN


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}"])
async def test_invalid_create_body_is_not_mistaken_for_safe_failure(
    body: bytes,
) -> None:
    """Malformed JSON or absent fields leave the remote creation outcome unknown."""
    async with adapter_context(
        lambda request: httpx.Response(200, content=body)
    ) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.create_hospital(HOSPITAL, BATCH_ID)
    assert caught.value.failure_kind == FailureKind.UNCERTAIN


@pytest.mark.asyncio
async def test_lookup_retries_transient_responses_and_honors_retry_after() -> None:
    """Read retry policy respects server hints without changing database state."""
    calls, delays = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        """Supply a server failure, then rate limit, then valid batch evidence."""
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[payload(id=102, active=True), payload()])

    async def sleep(delay: float) -> None:
        """Capture the actual delay selected by retry policy."""
        delays.append(delay)

    async with adapter_context(handler, sleep=sleep) as adapter:
        records = await adapter.get_hospitals_by_batch(BATCH_ID)
    assert len(calls) == 3 and delays[1] == 2
    assert calls[0].url.path == f"/hospitals/batch/{BATCH_ID}"
    assert [record.hospital_id for record in records] == [102, 101]
    assert records[0].active and not records[1].active


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [1, 2])
async def test_lookup_transport_retries_respect_configured_budget(
    attempts: int,
) -> None:
    """A lost read can be replayed, within the configured total-attempt limit."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Lose the first reply, then provide valid lookup evidence."""
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("Lost read", request=request)
        return httpx.Response(200, json=[])

    async with adapter_context(
        handler, settings=Settings(max_request_attempts=attempts)
    ) as adapter:
        if attempts == 1:
            with pytest.raises(DirectoryOperationError) as caught:
                await adapter.get_hospitals_by_batch(BATCH_ID)
            assert caught.value.failure_kind == FailureKind.RETRYABLE
        else:
            assert await adapter.get_hospitals_by_batch(BATCH_ID) == []
    assert len(calls) == attempts


@pytest.mark.asyncio
async def test_lookup_does_not_retry_nontransient_validation_rejection() -> None:
    """Safe read replay does not justify repeated unchanged input rejection."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Report documented validation rejection on the first read."""
        calls.append(request)
        return httpx.Response(422, json={"detail": []})

    async with adapter_context(handler) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.get_hospitals_by_batch(BATCH_ID)
    assert len(calls) == 1 and caught.value.failure_kind == FailureKind.PERMANENT


@pytest.mark.asyncio
async def test_long_retry_after_defers_instead_of_retrying_too_early() -> None:
    """A long hint must not become an early retry or an unbounded background sleep."""
    calls, delays = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        """Request a longer delay than the adapter's bounded retry-sleep policy."""
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async def sleep(delay: float) -> None:
        """Record any unintended wait."""
        delays.append(delay)

    async with adapter_context(handler, sleep=sleep) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await adapter.get_hospitals_by_batch(BATCH_ID)
    assert len(calls) == 1 and not delays
    assert caught.value.retry_after_seconds == 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [[payload(), payload()], [payload(creation_batch_id=str(uuid4()))], {}]
)
async def test_lookup_rejects_duplicate_or_wrong_batch_evidence(body) -> None:
    """Invalid evidence must not be supplied to reconciliation as trustworthy IDs."""
    async with adapter_context(
        lambda request: httpx.Response(200, json=body)
    ) as adapter:
        with pytest.raises(DirectoryOperationError):
            await adapter.get_hospitals_by_batch(BATCH_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"done", b'{"activated":true}'])
async def test_activation_acknowledges_generic_contract_body(body: bytes) -> None:
    """Do not invent required response JSON when the activation schema is generic."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return an activation acknowledgement, preserving its exact request."""
        requests.append(request)
        return httpx.Response(200, content=body)

    async with adapter_context(handler) as adapter:
        assert await adapter.activate_batch(BATCH_ID) is None
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == f"/hospitals/batch/{BATCH_ID}/activate"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2", 2),
        ("0.5", 0.5),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("bad", None),
    ],
)
def test_retry_after_seconds_rejects_invalid_or_unbounded_values(
    value, expected
) -> None:
    """Keep retry hints finite/nonnegative instead of propagating invalid sleeps."""
    assert _retry_after_seconds(value, datetime(2026, 11, 18, tzinfo=UTC)) == expected


def test_retry_after_http_date_uses_utc_and_clamps_past_dates() -> None:
    """Absolute server dates need a fixed UTC reference, not the host's timezone."""
    now = datetime(2026, 11, 18, tzinfo=UTC)
    assert _retry_after_seconds(format_datetime(now + timedelta(seconds=3)), now) == 3
    assert _retry_after_seconds(format_datetime(now - timedelta(seconds=3)), now) == 0


@pytest.mark.asyncio
async def test_shared_limit_covers_batches_verbs_and_adapter_instances() -> None:
    """One injected semaphore bounds all callers, not five requests per batch."""
    active = peak = serial = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """Measure simultaneous actual transport calls under mixed operation load."""
        nonlocal active, peak, serial
        active += 1
        serial += 1
        identifier = serial
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            if request.method == "POST":
                data = json.loads(request.content)
                return httpx.Response(200, json=payload(id=identifier, **data))
            return httpx.Response(200, json=[] if request.method == "GET" else {})
        finally:
            active -= 1

    settings = Settings(max_concurrent_requests=2)
    semaphore = asyncio.Semaphore(2)
    async with create_directory_client(
        settings, transport=httpx.MockTransport(handler)
    ) as client:
        first = HttpxHospitalDirectoryAdapter(
            client, settings, semaphore, sleep=no_wait
        )
        second = HttpxHospitalDirectoryAdapter(
            client, settings, semaphore, sleep=no_wait
        )
        await asyncio.gather(
            *(first.create_hospital(HOSPITAL, uuid4()) for _ in range(4)),
            *(second.get_hospitals_by_batch(uuid4()) for _ in range(3)),
            *(second.activate_batch(uuid4()) for _ in range(3)),
        )
    assert peak == 2 and active == 0 and serial == 10


@pytest.mark.asyncio
async def test_retry_backoff_releases_the_shared_request_permit() -> None:
    """Another batch can read while a safe creation retry is sleeping."""
    entered, release = asyncio.Event(), asyncio.Event()
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail one unsent POST, but allow an unrelated read immediately."""
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posts += 1
        if posts == 1:
            raise httpx.ConnectError("Not sent", request=request)
        return httpx.Response(200, json=payload())

    async def sleep(delay: float) -> None:
        """Hold backoff open until the competing lookup demonstrates progress."""
        entered.set()
        await release.wait()

    async with adapter_context(
        handler, settings=Settings(max_concurrent_requests=1), sleep=sleep
    ) as adapter:
        task = asyncio.create_task(adapter.create_hospital(HOSPITAL, BATCH_ID))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert (
                await asyncio.wait_for(
                    adapter.get_hospitals_by_batch(uuid4()), timeout=1
                )
                == []
            )
        finally:
            release.set()
            await task
    assert posts == 2


@pytest.mark.asyncio
async def test_overall_attempt_deadline_marks_a_hung_write_uncertain() -> None:
    """Socket inactivity limits alone do not bound an endlessly running request."""
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Hang POST until the overall deadline cancels it; allow later GET."""
        calls.append(request)
        if request.method == "POST":
            await asyncio.Event().wait()
        return httpx.Response(200, json=[])

    settings = Settings(
        http_connect_timeout_seconds=0.01,
        http_read_timeout_seconds=0.01,
        http_write_timeout_seconds=0.01,
        http_pool_timeout_seconds=0.01,
    )
    async with adapter_context(handler, settings=settings) as adapter:
        with pytest.raises(DirectoryOperationError) as caught:
            await asyncio.wait_for(
                adapter.create_hospital(HOSPITAL, BATCH_ID), timeout=1
            )
        assert caught.value.failure_kind == FailureKind.UNCERTAIN
        assert await adapter.get_hospitals_by_batch(BATCH_ID) == []
    assert sum(request.method == "POST" for request in calls) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_releases_the_permit() -> None:
    """Cancellation must not be swallowed or converted into another creation."""
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Block creation long enough for the caller to cancel its task."""
        if request.method == "POST":
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=[])

    async with adapter_context(
        handler, settings=Settings(max_concurrent_requests=1)
    ) as adapter:
        task = asyncio.create_task(adapter.create_hospital(HOSPITAL, BATCH_ID))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await asyncio.wait_for(adapter.get_hospitals_by_batch(BATCH_ID), timeout=1)
            == []
        )


class CountingTransport(httpx.AsyncBaseTransport):
    """Observe application-owned client closure without touching private HTTPX state."""

    def __init__(self) -> None:
        """Keep counts local to one lifespan test so other clients do not interfere."""
        self.closed = 0
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Supply harmless empty lookup evidence and count actual requests."""
        self.requests += 1
        return httpx.Response(200, json=[])

    async def aclose(self) -> None:
        """Count ownership cleanup; the adapter must not close a shared client too."""
        self.closed += 1


@pytest.mark.asyncio
async def test_application_owns_one_client_and_closes_it_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Startup wires the adapter without remote calls; shutdown owns cleanup once."""
    settings = Settings(sqlite_path=tmp_path / "app.db")
    transport = CountingTransport()
    client = create_directory_client(settings, transport=transport)

    def factory(config: Settings) -> httpx.AsyncClient:
        """Reuse the configured test client through the normal lifespan owner."""
        assert config == settings
        return client

    monkeypatch.setattr(main_module, "create_directory_client", factory)
    application = main_module.create_app(settings)
    async with application.router.lifespan_context(application):
        assert transport.requests == 0 and not client.is_closed
        assert isinstance(application.state.processor, BatchProcessor)
        assert (
            await application.state.directory_adapter.get_hospitals_by_batch(BATCH_ID)
            == []
        )
        assert await application.state.repository.count_in_flight_batches() == 0
    assert client.is_closed and transport.closed == 1 and transport.requests == 1
