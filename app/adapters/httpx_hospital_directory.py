"""External Directory HTTP translation, bounded concurrency, and safe retries.

The application owns the client and semaphore. This adapter never accesses ORM
sessions, saves row state, or decides whether a batch is eligible for activation.
"""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.config import Settings
from app.domain import (
    DirectoryHospital,
    DirectoryOperation,
    DirectoryOperationError,
    FailureEvidence,
    HospitalInput,
    hospital_key,
)

# Keep a background operation from sleeping indefinitely on a remote delay hint.
# Hints above this budget are returned to the caller; never retry before them.
_MAX_RETRY_DELAY_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 0.5
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class _HospitalPayload(BaseModel):
    """Require explicit batch/activation evidence and reject coerced IDs/flags."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(strict=True)
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    phone: str | None = None
    creation_batch_id: UUID
    active: bool = Field(strict=True)


_BATCH_PAYLOAD = TypeAdapter(list[_HospitalPayload])


def create_directory_client(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """Build the one client owned and closed by the application lifespan."""
    return httpx.AsyncClient(
        base_url=settings.directory_api_base_url,
        timeout=httpx.Timeout(
            connect=settings.http_connect_timeout_seconds,
            read=settings.http_read_timeout_seconds,
            write=settings.http_write_timeout_seconds,
            pool=settings.http_pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
        ),
        follow_redirects=False,
        transport=transport,
    )


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Translate Retry-After into an internal duration without trusting bad values."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.utcoffset() is None:
                return None
            seconds = max(0.0, (deadline - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _decode_hospitals(
    response: httpx.Response, operation: DirectoryOperation, batch_id: UUID
) -> list[DirectoryHospital]:
    """Reuse strict JSON translation for create and batch-read responses."""
    try:
        payloads = (
            [_HospitalPayload.model_validate_json(response.content)]
            if operation == DirectoryOperation.CREATE_HOSPITAL
            else _BATCH_PAYLOAD.validate_json(response.content)
        )
    except ValueError:
        raise DirectoryOperationError(
            operation,
            FailureEvidence.UNKNOWN_OUTCOME,
            f"{operation.value} returned invalid hospital evidence",
        ) from None
    if any(payload.creation_batch_id != batch_id for payload in payloads):
        raise DirectoryOperationError(
            operation,
            FailureEvidence.UNKNOWN_OUTCOME,
            f"{operation.value} returned a different batch UUID",
        )
    if len({payload.id for payload in payloads}) != len(payloads):
        raise DirectoryOperationError(
            operation,
            FailureEvidence.UNKNOWN_OUTCOME,
            f"{operation.value} returned duplicate hospital IDs",
        )
    return [
        DirectoryHospital(
            hospital_id=payload.id,
            name=payload.name,
            address=payload.address,
            phone=payload.phone,
            creation_batch_id=payload.creation_batch_id,
            active=payload.active,
        )
        for payload in payloads
    ]


def _response_error(
    operation: DirectoryOperation, response: httpx.Response
) -> DirectoryOperationError:
    """Translate an unsuccessful HTTP response without exposing transport objects."""
    evidence = (
        FailureEvidence.VALIDATION_REJECTED
        if response.status_code == 422
        else FailureEvidence.UNKNOWN_OUTCOME
    )
    return DirectoryOperationError(
        operation,
        evidence,
        f"{operation.value} returned HTTP {response.status_code}",
        retry_after_seconds=_retry_after_seconds(
            response.headers.get("Retry-After"), datetime.now(UTC)
        ),
    )


class HttpxHospitalDirectoryAdapter:
    """Implement the async Directory protocol without exposing transport objects."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        semaphore: asyncio.Semaphore,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Inject application-owned transport resources rather than opening per-row
        clients.
        """
        self._client = client
        self._semaphore = semaphore
        self._max_attempts = settings.max_request_attempts
        self._attempt_deadline = settings.http_attempt_timeout_seconds
        self._sleep = sleep

    async def _request(
        self,
        operation: DirectoryOperation,
        method: str,
        path: str,
        *,
        payload: dict[str, str | None] | None = None,
    ) -> httpx.Response:
        """Centralize one bounded attempt/retry loop for every external operation."""
        backoff = _BACKOFF_BASE_SECONDS
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._send(operation, method, path, payload)
            except DirectoryOperationError as caught:
                error = caught
                retry = (
                    error.evidence == FailureEvidence.NOT_SENT
                    or operation == DirectoryOperation.GET_BATCH
                )
            else:
                if response.status_code == 200:
                    return response
                error = _response_error(operation, response)
                # Safe-to-repeat GET does not mean every 4xx should be retried.
                # No undocumented write rejection (including 429/5xx) proves that
                # its mutation had no side effect, so none is blindly replayed.
                retry = operation == DirectoryOperation.GET_BATCH and (
                    response.status_code in (408, 429)
                    or 500 <= response.status_code < 600
                )
            if not retry or attempt == self._max_attempts:
                raise error from None
            retry_after = error.retry_after_seconds
            if retry_after is not None and retry_after > _MAX_RETRY_DELAY_SECONDS:
                raise error from None  # Defer rather than retrying before a long server hint.
            delay = max(
                retry_after or 0.0,
                random.uniform(0.0, backoff),
            )
            # The semaphore context ended before this wait; other batches can use
            # the request budget while this safe retry backs off.
            await self._sleep(delay)
            backoff = min(_MAX_RETRY_DELAY_SECONDS, backoff * 2)
        raise AssertionError("A positive attempt budget must return or raise")

    async def _send(
        self,
        operation: DirectoryOperation,
        method: str,
        path: str,
        payload: dict[str, str | None] | None,
    ) -> httpx.Response:
        """Run one bounded network attempt and translate transport failures."""
        try:
            # Local semaphore waiting is outside the network deadline. All verbs
            # share this permit, which is released before any retry sleep.
            async with self._semaphore:
                async with asyncio.timeout(self._attempt_deadline):
                    return await self._client.request(
                        method, path, json=payload, follow_redirects=False
                    )
        except _NOT_SENT_ERRORS as error:
            raise DirectoryOperationError(
                operation,
                FailureEvidence.NOT_SENT,
                f"{operation.value} failed before sending: {type(error).__name__}",
            ) from None
        except (httpx.HTTPError, TimeoutError) as error:
            raise DirectoryOperationError(
                operation,
                FailureEvidence.UNKNOWN_OUTCOME,
                f"{operation.value} has an unconfirmed outcome: {type(error).__name__}",
            ) from None

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Create one inactive hospital and validate the returned identity/evidence."""
        response = await self._request(
            DirectoryOperation.CREATE_HOSPITAL,
            "POST",
            "/hospitals/",
            payload={
                "name": hospital.name,
                "address": hospital.address,
                "phone": hospital.phone,
                "creation_batch_id": str(batch_id),
            },
        )
        record = _decode_hospitals(
            response, DirectoryOperation.CREATE_HOSPITAL, batch_id
        )[0]
        if record.active or hospital_key(record) != hospital_key(hospital):
            raise DirectoryOperationError(
                DirectoryOperation.CREATE_HOSPITAL,
                FailureEvidence.UNKNOWN_OUTCOME,
                "Creation reply does not match the expected inactive hospital",
            )
        return record

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Read typed batch evidence without changing any row or activation state."""
        response = await self._request(
            DirectoryOperation.GET_BATCH, "GET", f"/hospitals/batch/{batch_id}"
        )
        return _decode_hospitals(response, DirectoryOperation.GET_BATCH, batch_id)

    async def activate_batch(self, batch_id: UUID) -> None:
        """Acknowledge the contract's activation response without inventing its body."""
        await self._request(
            DirectoryOperation.ACTIVATE_BATCH,
            "PATCH",
            f"/hospitals/batch/{batch_id}/activate",
        )
