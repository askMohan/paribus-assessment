# 13 - Documentation and examples

Prerequisite: [12](12-docker.md). Next: [14](14-deployment-and-submission.md).

## Outcome

A reviewer can run the application, understand its behavior, and reproduce the intended flows without reading the planning conversation.

## Files to create or update during implementation

Project `README.md`, three `examples/` CSV fixtures, `design_doc.md`, and `output/pdf/hospital-bulk-api-guide.pdf`.

## Tasks

- [x] **13.01** Explain the separate bulk-service boundary, `BackgroundTasks` execution, SQLite persistence, and the upload/polling flow. Show processing -> `HospitalDirectoryAdapter` -> HTTPX implementation -> external API, and explain fake-adapter testing without introducing a new framework.
- [x] **13.02** Document the tested interpreter, local dependency installation, environment configuration, startup, and test commands.
- [x] **13.03** Document Docker/Compose commands and the persistent data volume.
- [x] **13.04** Add the shared three-hospital CSV, a missing-address invalid CSV, and an optional-phone example. Do not add unnecessary generated datasets.
- [x] **13.05** Include runnable curl examples for validation-only, upload, reading `Location`/batch UUID, polling, and resume.
- [x] **13.06** Show the exact accepted and comprehensive response bodies, with no extra result fields. Explain null IDs, row statuses, counts, and activation semantics.
- [x] **13.07** Explain safe retryable/permanent/uncertain classification, retention of inactive successes, and why unknown creation outcomes cannot be blindly retried.
- [x] **13.08** State limits: 20 rows, configured bytes/concurrency/admission limits, one process/instance, persistent-storage requirement, and manual restart recovery.
- [x] **13.09** State that identical new uploads are new batches, global hospital identity is unavailable, and losing the acceptance response without its UUID has no request-idempotency recovery.
- [x] **13.10** Include measured benchmark conditions/results and the distinction between simulated and live performance.
- [x] **13.11** Explain that acceptance/results are split across POST and GET by design; the assignment's comprehensive result is delivered by GET.
- [x] **13.12** Reserve a clearly identified place for the final repository and hosted API URLs; replace it with real verified links in step 14.

## Verification

- [x] Follow setup and validation instructions using the implemented configuration names and actual example paths.
- [x] Compare response examples with the explicit models and implementation; check no unsupported feature or recovery guarantee is promised.
- [x] Ensure examples and documentation contain no credentials, local database contents, or invented performance figures.

## Completion notes

Completed on 2026-11-19.

- Added a README with architecture diagrams, tested configuration/setup, Docker persistence, all five routes, copyable curl flows, exact response bodies, failure/resume limits, and the recorded Plan 11 simulation results.
- Added only three CSVs: `examples/hospitals.csv`, `examples/optional_phone.csv`, and `examples/invalid_missing_address.csv`.
- Created an illustrated PDF at `output/pdf/hospital-bulk-api-guide.pdf`, subsequently revised into a four-page technical design with editable source at `design_doc.md`. It covers current architecture, two-table schema, exact client/Directory contracts, processing order, failure/recovery scenarios, and production evolution. The final page specifies upload keys, per-row creation keys, activation keys, storage/uniqueness/replay rules, required upstream enforcement, and queue/worker/ownership changes. Future capabilities are explicitly proposed, not implemented. Every final PDF page was rendered and visually checked.
- Verified Python 3.12.14 and the documented `uv pip install` command against all 27 installed packages. The existing virtual environment has no pip module, so the instructions use uv directly. Compose configuration and lint checks pass.
- Executed the README curl flows against loopback Uvicorn, temporary SQLite, and the real adapter with a simulated HTTP transport: validation `204/204/422` without writes, upload `202` plus `Location`, polling, completed resume `409`, and failed-batch resume `202` with retained IDs. Response examples pass the exact Pydantic models.
- Full regression: **277 tests passed in 35.25 seconds**. No application code or dependencies changed; PDF tooling ran in an isolated temporary environment. No live Directory mutations occurred, and the existing local database hash is unchanged.
- Repository/hosted links remain explicitly pending Plan 14. Fixed the plan index's obsolete root design link to point to the current README.
