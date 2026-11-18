# 05 - HTTPX Directory adapter, concurrency, and safe retries

Prerequisite: [04](04-sqlite-storage.md). Next: [06](06-batch-processing.md).

## Outcome

`HttpxHospitalDirectoryAdapter` implements the step 02 interface, isolating external requests and HTTPX-specific behavior while respecting timeouts, concurrency bounds, and failure evidence.

## Files to create or update during implementation

`app/adapters/httpx_hospital_directory.py`, application resource lifecycle wiring, and `tests/adapters/test_directory_adapter.py`. Reuse the interface in `app/adapters/hospital_directory.py`; do not create a redundant `app/directory_client.py` wrapper.

## Tasks

- [x] **05.01** Create one `httpx.AsyncClient` in application startup and close it in shutdown. Create one `HttpxHospitalDirectoryAdapter` implementing `HospitalDirectoryAdapter`, inject the shared client/settings/semaphore, and reuse it across rows and batches.
- [x] **05.02** Configure the base URL, connect/read/write/pool timeouts, and connection limits from step 01.
- [x] **05.03** Create one application-wide semaphore; acquire it per HTTP attempt, including attempts from different batches, and release it before retry backoff.
- [x] **05.04** Implement adapter `create_hospital` using POST `/hospitals/` with only `name`, `address`, `phone`, and `creation_batch_id`. Do not send `active` or local row UUIDs.
- [x] **05.05** Implement adapter `get_hospitals_by_batch` using GET `/hospitals/batch/{batch_id}` and `activate_batch` using PATCH `/hospitals/batch/{batch_id}/activate` with the supplied contract.
- [x] **05.06** Validate creation results sufficiently to associate an ID and input with the correct batch. Preserve evidence rather than treating malformed success as safe failure.
- [x] **05.07** Translate HTTPX exceptions and HTTP responses into the step 02 domain operation outcomes and decision categories inside the adapter. Connect/pool failures known to precede POST transmission differ from read/write timeouts. Do not leak raw HTTPX responses/exceptions to processing or reconciliation.
- [x] **05.08** Add bounded backoff and jitter for safe operations. Retry transient GET failures and known-unsent creation failures; do not blanket-retry POST `5xx`, `429`, or lost responses.
- [x] **05.09** Parse `Retry-After` when relevant without assuming a `429` guarantees no creation. Keep a bounded attempt duration and handle expiration according to whether a POST might be in flight.
- [x] **05.10** Return the interface's typed internal data/acknowledgement or classified errors. The adapter does not mutate repository state, independently decide batch activation eligibility, or expose extra public fields.
- [x] **05.11** Wire the application-owned adapter into later processing/reconciliation through the interface using ordinary constructor/function parameters. Use one owner for client cleanup and avoid nested retry layers that multiply the configured attempts.

## Verification

- [x] Mock HTTP transport and assert exact paths and creation payload, including a shared batch UUID and no `active` field.
- [x] Verify a known-unsent failure is retried within limits while a read timeout or ambiguous `500` does not generate another POST automatically.
- [x] Verify transient GET retries, permanent rejection handling, malformed creation success, and uncertain activation.
- [x] Track active mocked requests from multiple callers and confirm the shared limit is never exceeded, including during retries.
- [x] Make retry sleeps controllable in tests; do not wait through real long backoff or call the deployed API.
- [x] Confirm all three interface operations translate input/results consistently, transport errors become domain errors, and adapter execution does not access SQLite. Verify lifecycle closes the shared HTTPX client once.

## Completion notes

Completed on 2026-11-18. Added `app/adapters/httpx_hospital_directory.py` and `tests/adapters/test_directory_adapter.py`; application lifespan now owns the shared client and adapter. No dependencies were added.

Implementation details:

- `create_directory_client` applies the existing base URL, connection limits, and four timeout settings. Redirects stay disabled so mutations are not silently replayed elsewhere.
- `HttpxHospitalDirectoryAdapter` implements all three protocol methods and receives the client, settings, semaphore, and an injectable sleep function. The adapter is exposed as `application.state.directory_adapter` for plain injection into the processor in step 06 and reconciliation in step 08. Those later workflows have not been implemented.
- Creation sends exactly the contract fields and accepts only matching inactive hospital evidence with a strict integer ID and matching batch UUID. Lookup rejects duplicate IDs and wrong-batch records. Activation accepts the documented HTTP 200 acknowledgement without inventing response-body requirements.
- One request/retry loop applies `MAX_REQUEST_ATTEMPTS`, including the first attempt. Known-unsent connection/connect-timeout/pool-timeout failures can retry; transient GET transport failures and HTTP 408/429/5xx can retry. HTTP 422 is a permanent validation rejection. Uncertain mutation failures, including read/write timeouts, HTTP 429/5xx, redirects, and malformed creation results, do not automatically repeat the mutation.
- Full jitter starts with a 0.5-second exponential ceiling and has a 30-second maximum. Numeric and HTTP-date `Retry-After` values are supported. Hints above 30 seconds return the classified error instead of causing an unbounded wait or an early retry. `DirectoryOperationError.retry_after_seconds` retains the internal hint without changing public schemas.
- Each network attempt also has an overall deadline equal to the sum of the four socket budgets: 55 seconds with defaults. Local semaphore waiting is excluded. Expired mutations are uncertain; caller cancellation propagates, leaving persistence/recovery to the future processor. Every request path releases the semaphore, and backoff occurs outside it.
- Adapter methods return domain records/acknowledgements or domain errors. They import no repository/database code, persist nothing, and do not decide activation eligibility. Functions and arguments have explanatory docstrings and non-obvious safety decisions have comments.

Verification:

```bash
uvx ruff format app/adapters/httpx_hospital_directory.py app/adapters/hospital_directory.py app/main.py app/domain.py tests/adapters/test_directory_adapter.py
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

- **167 tests passed**, no warnings, in 2.76 seconds: 114 existing tests and 53 adapter/lifecycle cases.
- All 27 installed packages are compatible.
- Mixed requests from multiple batches and adapter instances stayed within the shared limit. An unrelated lookup completed while a creation retry was held in backoff. Timeout and caller-cancellation tests also confirmed permit release.
- Exact payloads/paths, safe retry exhaustion, configurable attempt limits, unsafe write non-replay, result validation, server hints, generic activation acknowledgements, and one-time client cleanup were verified with mocked HTTP transport and temporary SQLite files.
- Functions have purpose docstrings and comments explain retry safety. No live Directory calls were made.

Step 06 remains unstarted. No upload, polling, processing, reconciliation, or resume routes were added by this step.
