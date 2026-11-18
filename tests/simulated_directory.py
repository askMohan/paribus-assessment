"""Local Directory transport shared by integration tests and the benchmark."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import partial
from unittest.mock import patch
from uuid import UUID

import httpx
from fastapi import FastAPI

from app.adapters.httpx_hospital_directory import (
    HttpxHospitalDirectoryAdapter,
    create_directory_client,
)
from app.config import Settings
from app.main import create_app


def hospital_csv(count: int = 3, *, identical: bool = False) -> bytes:
    """Generate the same bounded input for each test or concurrency setting."""
    rows = [
        f"Hospital {1 if identical else n},Address {1 if identical else n},00123"
        for n in range(1, count + 1)
    ]
    return ("name,address,phone\n" + "\n".join(rows) + "\n").encode()


async def no_wait(delay: float) -> None:
    """Skip retry backoff in simulations; request latency is still applied."""


class SimulatedDirectory:
    """Retain remote records, inject failures, and observe actual HTTP concurrency."""

    def __init__(self, *, expected_count: int = 3, latency_seconds: float = 0) -> None:
        """Configure record count and per-request latency without external traffic."""
        self.expected_count = expected_count
        self.latency_seconds = latency_seconds
        self.records: list[dict] = []
        self.requests: list[tuple[str, str, str | None]] = []
        self.creation_faults: dict[str, str | int] = {}
        self.activation_fault: str | None = None
        self.lookup_status = 200
        self.held_names: set[str] = set()
        self.release = asyncio.Event()
        self.active = self.peak_active = 0
        self.on_activate: Callable[[str], Awaitable[None]] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Handle the contract's three operations through a real HTTPX request."""
        data = json.loads(request.content) if request.method == "POST" else None
        batch_id = data["creation_batch_id"] if data else request.url.path.split("/")[3]
        name = data["name"] if data else None
        self.requests.append((request.method, batch_id, name))
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(self.latency_seconds)
            if request.method == "POST":
                assert request.url.path == "/hospitals/"
                return await self._create(request, data)
            if request.method == "GET":
                assert request.url.path == f"/hospitals/batch/{batch_id}"
                return httpx.Response(
                    self.lookup_status, json=self.batch_records(batch_id)
                )
            assert request.method == "PATCH"
            assert request.url.path == f"/hospitals/batch/{batch_id}/activate"
            return await self._activate(request, batch_id)
        finally:
            self.active -= 1

    def batch_records(self, batch_id: str) -> list[dict]:
        """Scope upstream evidence to the batch UUID supplied by the adapter."""
        return [
            record for record in self.records if record["creation_batch_id"] == batch_id
        ]

    async def _create(self, request: httpx.Request, data: dict) -> httpx.Response:
        """Distinguish unsent failure, rejection, and possibly committed creation."""
        if data["name"] in self.held_names:
            await self.release.wait()
        fault = self.creation_faults.get(data["name"])
        if fault == "connect":
            raise httpx.ConnectError("Connection unavailable", request=request)
        if fault == "reject":
            return httpx.Response(422, json={"detail": "Rejected input"})
        if fault == "unresolved":
            raise httpx.ReadTimeout("Unconfirmed creation", request=request)
        record = {**data, "id": len(self.records) + 101, "active": False}
        self.records.append(record)
        if fault == "lost_reply":
            raise httpx.ReadTimeout("Creation committed; reply lost", request=request)
        return httpx.Response(fault if isinstance(fault, int) else 200, json=record)

    async def _activate(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        """Require all remote records and optionally lose the activation reply."""
        records = self.batch_records(batch_id)
        assert len(records) == self.expected_count
        if self.on_activate is not None:
            await self.on_activate(batch_id)
        if self.activation_fault == "connect":
            raise httpx.ConnectError("Activation not sent", request=request)
        for record in records:
            record["active"] = True
        if self.activation_fault == "lost_reply":
            raise httpx.ReadTimeout("Activation committed; reply lost", request=request)
        return httpx.Response(200, json={})


@contextmanager
def directory_application(
    directory: SimulatedDirectory, settings: Settings
) -> Iterator[FastAPI]:
    """Keep real application services, replacing only transport and retry sleep."""
    with (
        patch(
            "app.main.create_directory_client",
            partial(create_directory_client, transport=httpx.MockTransport(directory)),
        ),
        patch(
            "app.main.HttpxHospitalDirectoryAdapter",
            partial(HttpxHospitalDirectoryAdapter, sleep=no_wait),
        ),
    ):
        application = create_app(settings)

        async def check_activation(batch_id: str) -> None:
            """Ensure every ID is committed locally before the remote activation."""
            batch = await application.state.repository.get_batch(UUID(batch_id))
            assert batch.creation_complete
            assert {row.hospital_id for row in batch.rows} == {
                record["id"] for record in directory.batch_records(batch_id)
            }

        directory.on_activate = check_activation
        yield application
