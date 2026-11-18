# 10 - Interrupted-work recovery

Prerequisite: [09](09-resume-api.md). Next: [11](11-integration-and-performance.md).

## Outcome

Restarting with the same SQLite file preserves progress, detects abandoned active work, and allows safe manual recovery. No automatic job replay is added.

## Files to create or update during implementation

Application lifecycle and repository recovery operations, processor timing cleanup, and `tests/services/test_restart_recovery.py`.

## Tasks

- [x] **10.01** At startup, find batches saved as queued, processing, or activating by the previous single application process.
- [x] **10.02** Mark interrupted batches internally as `INTERRUPTED`, preserving inputs, IDs, activation evidence, and saved processing time.
- [x] **10.03** Change interrupted `CREATING` rows to `UNKNOWN` with uncertain classification; a pre-request state write cannot prove the POST was or was not sent.
- [x] **10.04** Preserve unattempted pending rows and confirmed created/activated rows. Do not convert all unfinished rows into definitive failures.
- [x] **10.05** Clear abandoned active timing markers without inventing completion time. Define how the last uncheckpointed interval is treated; use saved checkpoints and document any crash-time undercount rather than counting downtime as work.
- [x] **10.06** Do not automatically schedule jobs at startup. Existing polling remains available; the client can request resume.
- [x] **10.07** Recover a queued batch saved before task execution by processing its pending rows on manual resume.
- [x] **10.08** Recover interrupted activation through upstream inspection before any activation retry; never recreate confirmed hospital rows.
- [x] **10.09** Ensure application lifecycle cleanup closes the shared HTTPX client used by the adapter and database resources with clear single ownership. Do not assume shutdown always completes before termination.

## Verification

- [x] Close one application and open another on the same temporary SQLite file; confirm persisted input and IDs remain.
- [x] Test one created row, one in-flight row, and one pending row; confirm their recovery classifications differ correctly.
- [x] Confirm startup causes zero upstream writes and no automatic processing.
- [x] Resume a saved-but-never-started batch and an interrupted activation batch; check safe operation selection.
- [x] Confirm a long downtime is excluded from active processing time.

## Completion notes

Completed on 2026-11-18.

- `BatchRepository.recover_interrupted_batches()` uses one short ORM transaction to find queued, processing, and activating batches, mark them `INTERRUPTED`, and change only abandoned `CREATING` rows to `UNKNOWN` with uncertain failure evidence. Pending input, confirmed IDs, activated rows, and previously checkpointed processing duration are preserved. Completed and already-paused batches are untouched.
- Application startup invokes recovery before exposing its repository/processor to requests. Recovery makes no Directory calls and schedules no jobs. The same application lifespan still owns and closes the shared HTTPX client and database exactly once.
- Abandoned `attempt_started_at` values are cleared without calculating elapsed time from the restart clock. The last attempt's uncheckpointed interval is deliberately lost because the process's actual stop time is unknown. This can undercount work before a crash; it never treats downtime as processing. Later resumed attempts add their own active time to the existing saved duration.
- Manual resume reuses the existing claim/processor path. Queued-but-never-started input is processed on demand; interrupted activation inspects all expected upstream identities before retrying activation or confirming an already-active batch. Existing hospital IDs are never recreated for activation recovery.

Verification:

```bash
uvx ruff check app/db/repository.py app/main.py tests/services/test_restart_recovery.py
uvx ruff format --check app/db/repository.py app/main.py tests/services/test_restart_recovery.py
.venv/bin/python -m pytest -q tests/services/test_restart_recovery.py tests/api/test_health.py
```

Seven restart cases cover mixed created/in-flight/pending rows across two application lifespans, exact saved timing after long downtime, public polling after restart, idempotent recovery, no startup network requests, manual recovery of never-started input, active/inactive interrupted-activation evidence, and preservation of completed/failed/reconciliation-required batches. The combined restart/health run passed all nine cases; a subsequent restart-only run passed all seven cases after adding polling assertions. All traffic uses a typed fake or rejecting HTTPX transport and temporary SQLite files.

Plans 07–10 were implemented together at the user's request. Step 11 remains unstarted.
