# 11 - Integration verification and performance

Prerequisite: [10](10-restart-recovery.md). Next: [12](12-docker.md).

## Outcome

The implemented features satisfy the design's scenario matrix, and concurrency improvement is measured using a simulated upstream API.

## Files to create or update during implementation

`tests/integration/test_bulk_flow.py`, shared `tests/simulated_directory.py`, and `scripts/benchmark.py`. Benchmark results and scenario coverage are recorded below. Application code and dependencies are unchanged.

## Tasks

- [x] **11.01** Review the root design's scenario matrix against tests added in steps 01-10. Add only missing behavioral coverage.
- [x] **11.02** Exercise upload -> background creation -> adapter -> simulated upstream -> activation -> polling through real application boundaries with temporary SQLite. Use the concrete HTTPX adapter for these integration checks and fakes for processor/reconciliation unit checks.
- [x] **11.03** Verify partial failure/resume, permanent rejection, lost creation response, unresolved creation, activation failure, and lost activation response end to end.
- [x] **11.04** Cover exact response fields at intermediate and completed states, no database/upstream side effects on invalid CSV, and rollback on registration failure.
- [x] **11.05** Cover concurrent batches under one semaphore, admission limits, simultaneous resume, repeated identical CSV rows, and interrupted startup recovery.
- [x] **11.06** Verify a disconnected client does not intentionally cancel accepted processing while the application remains alive. Do not claim acceptance-response recovery without a retained UUID.
- [x] **11.07** Build a reproducible simulated-latency benchmark for the same 20 records at concurrency 1, 3, and 5. Record latency setting, elapsed time, maximum active requests, and request counts.
- [x] **11.08** Confirm concurrency does not add creations or activate before all rows are confirmed. Treat measured speedup as simulation-specific, not a live-service guarantee.
- [x] **11.09** Run the completed test suite once after changes; resolve failures. Broaden or repeat checks only for new changes or unresolved concerns.
- [x] **11.10** Record commands, results, benchmark conditions, and any remaining limitations for documentation.
- [x] **11.11** Check the adapter boundary: external URLs, HTTP response parsing, and HTTPX exception handling live in the concrete adapter; processing/reconciliation use the interface and typed outcomes. Reuse existing checks rather than adding tests that only mirror imports or implementation details.

## Verification

- [x] All required scenario behaviors have a passing meaningful check, with no routine live upstream calls.
- [x] The benchmark is repeatable and bounded, with no brittle wall-clock speedup threshold in ordinary unit tests.
- [x] No extra public response fields or accidental multi-worker assumptions were introduced.

## Completion notes

Completed on 2026-11-18. Added 16 integration cases using the real routes, background processor, SQLite ORM repository, and concrete HTTPX adapter. Only the Directory's HTTP transport and retry sleeps are replaced. Existing unit/API tests continue covering behavior that does not need another full-stack test.

The shared simulator retains remote records, injects pre-send failures, explicit validation rejection, committed writes with lost replies or ambiguous HTTP statuses, and failed lookups. It records request counts and peak concurrency. Before every activation, it checks that all expected records exist remotely and every corresponding ID is already committed locally. All test storage and benchmark databases are temporary.

### Scenario coverage

| Design scenario | Verification |
| --- | --- |
| Successful upload, concrete adapter, ordered final results | Integration `test_intermediate_polling_then_success_uses_concrete_adapter` |
| Poll after one success; exact fields and nullable IDs | Same gated integration case; existing API polling cases cover all six internal states |
| Invalid CSV and registration rollback, no upstream side effects | Integration `test_rejected_input_or_registration_failure_has_no_side_effects`; existing CSV/API tests cover malformed/oversized input and validation-only |
| Safely retryable partial failure and resume of only missing work | Integration creation-failure case `connect` |
| Permanent rejection and unchanged-input resume conflict | Integration creation-failure case `reject` |
| Creation committed but response lost | Integration creation-failure case `lost_reply` |
| Creation remains unresolved; unknown excluded from failed counts | Integration creation-failure case `unresolved`, including another manual lookup without a repeated POST |
| Ambiguous POST HTTP 500/429 | Integration creation-failure cases commit the hospital before returning the error; lookup recovers it without repeating creation |
| Bounded failed GET retries followed by recovery | Integration `test_failed_reconciliation_get_has_bounded_retries_and_resumes_without_post` |
| Activation fails; preserve every creation ID and resume activation only | Integration activation-failure case `connect` |
| Activation committed but acknowledgement lost | Integration activation-failure case `lost_reply`, confirming completion through GET |
| Identical input rows and exact quantity recovery | Integration `test_identical_unknown_rows_reconcile_by_quantity_without_repeating_posts`; existing reconciliation tests cover missing/extra candidates and protected input |
| Same CSV as a new request; multiple batches and global concurrency/admission | Integration `test_batches_share_request_budget_and_admission_without_cross_upload_deduplication`: two distinct batches, peak five requests, third batch rejected at cap two |
| Simultaneous resume requests | Existing `tests/api/test_resume.py::test_simultaneous_resume_requests_have_one_winner` and independent-connection repository claim tests |
| Restart with saved, in-flight, and pending input | Integration `test_restart_recovers_lost_creation_through_real_adapter_and_resume`; existing restart tests cover interrupted activation and downtime exclusion |
| Client disconnect after acceptance | Integration `test_disconnecting_after_202_does_not_cancel_accepted_work` uses a real loopback Uvicorn server, closes the upload client while creation is gated, then polls and completes from a new connection |
| Missing UUID, time accounting, unexpected exceptions, and transaction failure | Existing API, processor, and repository tests retained without duplicate scenarios |

The disconnect case retains the UUID from the acceptance response. Losing both the response and UUID still has no idempotency recovery; a fresh upload remains a separate batch. Startup recovery is manual, and the application remains a single-instance/single-worker design.

### Reproducible benchmark

Run from the project root with the existing environment:

```bash
.venv/bin/python -m scripts.benchmark --latency-ms 50
```

Conditions:

- Same generated 20-hospital CSV and a fresh temporary SQLite file for each run.
- One application and one shared HTTPX client; pool limits remain five while the shared semaphore changes to 1, 3, or 5.
- The mocked Directory adds 50 ms to every upstream request, including activation. No remote server, TCP/TLS cost, rate limit, or cold start is modeled.
- Elapsed time spans CSV submission through background completion, including ORM checkpoints and activation. HTTPX `ASGITransport` awaits background work, so this is deliberately not a measurement of when a real client receives its `202` response.
- Assertions verify 20 distinct confirmed IDs, exactly 20 creation requests and one activation request, complete-manifest activation ordering, and the configured request-concurrency bound.
- Retry delays are injectable and skipped by the simulator; fault-free benchmark runs perform no retries. The script prints JSON and does not modify project data.

Observed on 2026-11-18:

| Concurrency | Elapsed seconds | Maximum active requests | POST / GET / PATCH | Speedup vs 1 |
| --- | ---: | ---: | --- | ---: |
| 1 | 1.3292 | 1 | 20 / 0 / 1 | 1.00x |
| 3 | 0.5075 | 3 | 20 / 0 / 1 | 2.62x |
| 5 | 0.3711 | 5 | 20 / 0 / 1 | 3.58x |

These numbers are simulation-specific observations, not a live-service speed guarantee. Ordinary tests assert state, counts, ordering, and concurrency bounds rather than a wall-clock speedup threshold. Latency is bounded to 0–1000 ms by the CLI.

### Verification

```bash
uvx ruff check app tests scripts
uvx ruff format --check app tests scripts
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

The concrete adapter still owns URLs, raw HTTP payloads, transport-error translation, deadlines, and safe retries. Processor/reconciliation code continues consuming the typed interface; the original fake-adapter unit cases and new raw-transport integration cases exercise both sides of that boundary.

- **277 tests passed** in 11.68 seconds: 261 existing cases plus 16 integration cases. Deprecation/future warnings remain errors; no warnings were raised.
- Ruff lint and formatting passed for all 36 Python files, including the benchmark. All 27 installed packages are compatible; no dependencies were added.
- The existing project SQLite file is unchanged. Integration and benchmark runs used temporary storage and sent no live Directory requests. The disconnect check used loopback HTTP only.
- Step 12 remains unstarted.
