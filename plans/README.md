# Step-by-step implementation

Design and usage reference: [project README](../README.md), [technical design](../design_doc.md), and [illustrated PDF](../output/pdf/hospital-bulk-api-guide.pdf).

This folder breaks the agreed design into small implementation steps. Steps 01-13 are implemented and verified; their completion notes record the commands and results. Steps 07-10 were developed in parallel at the user's request and then integrated and reviewed together. [14 - Deployment and submission](14-deployment-and-submission.md) is in progress: repository access and Render configuration are being verified; public deployment checks remain outstanding.

## Working agreement

1. Follow step prerequisites; parallelize only when requested, with shared interfaces agreed before integration.
2. Implement only the step requested by the user; do not automatically start the next step.
3. Read the design reference and the selected step before changing code.
4. Work through its numbered tasks and relevant checks. Add meaningful tests alongside each behavior rather than postponing all tests.
5. Check off a task only when implemented and verified. Record material deviations and the commands/results in that step's completion notes.
6. If a step cannot finish, leave its unfinished tasks unchecked and explain the blocker. Do not silently change the agreed design.
7. Report the completed step, verification results, and next step. Wait for the next implementation instruction.
8. Keep functions small and focused. Reuse existing validators, repository operations, and adapter helpers before adding abstractions. Give functions short purpose docstrings; explain arguments where their names/types are insufficient. Keep enum explanations and comments about non-obvious state, retry, and transaction decisions. Avoid repetitive documentation and speculative infrastructure.

Task IDs use `SS.TT`, where `SS` is the step number and `TT` is the task within that step. Checkboxes in the numbered files are the task tracker; this index is the navigation and sequence.

## Source layout

```text
app/
  main.py                         # Lifecycle and shared storage/capacity errors
  config.py                       # Environment settings and startup validation
  domain.py                       # States, typed data, and failure decisions
  api/
    routes.py                     # Health, upload, validation, polling, and resume
    dependencies.py               # Application-owned services and acceptance helper
    schemas.py                    # Exact public response fields
  services/
    csv_validation.py             # Shared upload/header/row validation
    processor.py                  # Creation, outcome collection, and activation
    reconciliation.py             # Conservative recovery from upstream evidence
  db/
    database.py                   # Engine and short session/transaction lifecycle
    models.py                     # SQLAlchemy mappings
    repository.py                 # Guarded persistence operations
  adapters/
    hospital_directory.py         # Typed external API interface
    httpx_hospital_directory.py   # HTTP translation, concurrency, safe retries
```

Tests mirror these folders. `tests/conftest.py` owns the shared temporary repository fixture. Root-level tests cover configuration and domain decisions. `pyproject.toml` supplies local lint/format settings. Keep one implementation per responsibility; do not add compatibility wrappers at old module paths.

## Sequence

| Step | Deliverable | Prerequisite |
| --- | --- | --- |
| [01](01-project-setup.md) | Runnable FastAPI application and configuration | None |
| [02](02-contracts-and-states.md) | Exact public schemas, internal state/failure definitions, and adapter interface | 01 |
| [03](03-csv-validation.md) | Shared CSV validator | 02 |
| [04](04-sqlite-storage.md) | SQLite tables and tested repository operations | 03 |
| [05](05-directory-client.md) | HTTPX Directory adapter, concurrency control, and safe retries | 04 |
| [06](06-batch-processing.md) | Batch processor and activation flow | 05 |
| [07](07-upload-and-polling-api.md) | Upload acceptance, validation-only route, and result polling | 06 |
| [08](08-reconciliation.md) | Recovery of uncertain upstream outcomes | 07 |
| [09](09-resume-api.md) | Safe manual resume | 08 |
| [10](10-restart-recovery.md) | Persistent interrupted-work detection | 09 |
| [11](11-integration-and-performance.md) | Completed scenario coverage and measured concurrency results | 10 |
| [12](12-docker.md) | Single-service Docker/Compose execution with persistent SQLite | 11 |
| [13](13-documentation-and-examples.md) | README, CSV examples, and reproducible usage instructions | 12 |
| [14](14-deployment-and-submission.md) | Hosted application and submission links | 13 |

## Fixed scope

- FastAPI `BackgroundTasks` executes work; SQLite saves state.
- Use SQLAlchemy ORM with the aiosqlite driver, operation-scoped async sessions, and reusable repository guards. Data access must not revert to raw SQL queries. Driver-only SQLite configuration stays in database lifecycle hooks.
- All external Directory API operations use `HospitalDirectoryAdapter`. Its HTTPX implementation owns request/response translation and safe transport retries; processing and reconciliation receive the interface through plain dependency injection.
- One application instance and one worker. No Redis, Celery, Arq, or separate queue service.
- Upload returns `202` with only `batch_id`; the polling URL is in `Location`.
- Polling returns only the seven documented result fields. Row results contain only `row`, `hospital_id`, `name`, and `status`.
- Internal state and `failure_kind` are not additional public fields.
- Retain inactive successful creations and resume only safe remaining work.
- A new upload is a new batch; cross-upload deduplication and request-idempotency headers are outside scope.
- No frontend, batch-input editing, automatic job replay, or automatic deletion of failed batches.
- SQLite recovery requires persistent storage; `BackgroundTasks` is not a durable queue.

## Shared three-hospital example

```csv
name,address,phone
General Hospital,123 Main Street,555-0101
City Care Hospital,45 Park Road,555-0102
Sunrise Hospital,8 Lake Road,555-0103
```

Use fake adapters for processing/reconciliation tests, mocked HTTPX transport for adapter tests, and temporary databases for persistence tests. Do not create live upstream hospital data as part of routine testing. The design's scenario matrix remains the acceptance reference.

Cleanup on 2026-11-18: organized the implementation/tests by responsibility, shortened repetitive documentation, and split CSV/HTTP/processing work into focused functions. Removed the redundant in-memory batch execution set, duplicate cleanup branches, repeated batch reads, and separate per-setting environment parsing. The atomic repository claim remains the execution guard; the short claim shield preserves cancellation safety.

Steps 07-10 integrated on 2026-11-18: upload/validation/polling, conservative reconciliation, manual resume, and startup interruption recovery are implemented with the existing ORM schema and exact response contract. Full verification: **259 tests passed**, lint/format checks passed. Startup performs no upstream calls or job replay. Lost creation/activation evidence is inspected before any further mutation; saved timing excludes downtime and idle gaps. Step 10 documents the uncheckpointed crash-time interval policy.

Simplicity review on 2026-11-18: all endpoints now share one router; the separate resume module and a single-use status-URL wrapper were removed. Storage/capacity errors are handled once by the application. Creation eligibility, hospital matching, and batch readiness use shared domain rules. Abandoned-intent cleanup is one atomic repository operation reused by reconciliation/cancellation, with the same row mutation reused during restart recovery. Removed the unused active-ID listing method. Existing API fields, retry limits, claims, activation barriers, and persistent schema remain intact; two added database cases verify cleanup rollback and confirmed-ID preservation.

Post-cleanup verification: **261 tests passed** in 9.67 seconds; lint, formatting, and installed-package checks passed. Existing SQLite data is unchanged. Steps 11-14 remain unstarted.

Step 11 verification on 2026-11-18: **277 tests passed**, including 16 concrete-adapter integration cases and a real loopback disconnect check. Shared simulation lives in `tests/simulated_directory.py`; run `.venv/bin/python -m scripts.benchmark --latency-ms 50` from the project root. The 20-record simulation measured 1.3292s / 0.5075s / 0.3711s at concurrency 1 / 3 / 5, always with exactly 20 creation requests and one activation. Conditions, coverage, and limits are recorded in step 11. No production code or dependencies changed. Steps 12-14 remain unstarted.

Step 12 packaging is complete: minimal Dockerfile/Compose, runtime-only image, non-root execution, one worker, configurable port, standard-library health check, and persistent SQLite volume. Real image builds and running-container checks verified health/docs/validation plus saved-state recovery after container recreation. `.env.example` documents the supported values; the ignored local `.env` was created only when absent. Build/start/stop commands and volume-retention behavior are recorded in step 12.

Step 13 documentation is complete on 2026-11-19: project README, three CSV examples, and a four-page technical design in editable Markdown and a visually verified PDF. Current-flow and production-flow diagrams accompany the actual schema, execution/recovery flow, exact client and Directory contracts, and proposed upload/per-row/activation idempotency. Copyable curl validation/upload/poll/resume flows passed against a local simulated Directory; exact JSON examples match the response models. Full regression: **277 tests passed**; lint, dependency, and Compose checks passed. Plan 14 is in progress; its checklist records deployment verification separately from local preparation.
