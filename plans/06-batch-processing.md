# 06 - Background batch processing and activation

Prerequisite: [05](05-directory-client.md). Next: [07](07-upload-and-polling-api.md).

## Outcome

An async processor can create a registered batch's hospitals, save independent outcomes, and activate only a fully confirmed batch. Uncertain outcomes stop safely until reconciliation is added in step 08.

## Files to create or update during implementation

`app/services/processor.py`, application lifespan wiring in `app/main.py`, and `tests/services/test_processor.py`. Extend the existing mocked-client lifecycle test to confirm processor wiring. Reuse repository claims; no separate concurrency module is needed.

## Tasks

- [x] **06.01** Add an async `process_batch(batch_id)` entry point using the repository and an injected `HospitalDirectoryAdapter` interface that remain valid after the request ends. Do not import HTTPX, construct external URLs, or inspect raw HTTP responses in the processor.
- [x] **06.02** Establish one batch execution claim, load input rows, and start attempt timing. Keep admission/claim acquisition distinct from holding a database transaction.
- [x] **06.03** Skip rows with confirmed created/activated outcomes; do not use a raw retry of every input row.
- [x] **06.04** Mark each eligible row `CREATING` before calling adapter `create_hospital`. Start concurrent row tasks and rely on the adapter's shared semaphore for the upstream bound.
- [x] **06.05** Persist each confirmed creation ID immediately. Save definitive failures and uncertain outcomes using the agreed classifications.
- [x] **06.06** Collect all in-flight task outcomes even when one row fails. Do not treat cancellation as proof that no upstream hospital was created.
- [x] **06.07** Leave a partial batch inactive. Save `FAILED` for definitive incomplete work or `RECONCILIATION_REQUIRED` for unresolved creation outcomes.
- [x] **06.08** Enter `ACTIVATING` only when every row has a confirmed ID and no pending, failed, or unknown outcome remains, then call adapter `activate_batch`. Keep this eligibility decision in the processor.
- [x] **06.09** Persist confirmed activation atomically with row activation state. On uncertain activation, preserve successful creations and defer confirmation to reconciliation.
- [x] **06.10** Finalize accumulated processing time and release claims in cleanup paths. Preserve enough evidence after unexpected exceptions; attempted-but-unconfirmed POSTs stay uncertain.
- [x] **06.11** Add batch/row-correlated logs for outcomes and unexpected errors; do not dump whole CSV contents or add response fields.

## Verification

- [x] Run the three-hospital success path with out-of-order responses; confirm IDs are saved against original rows and activation happens after all creations.
- [x] Make City Care fail definitively; confirm General/Sunrise are retained inactive and activation is never called.
- [x] Lose City Care's response; confirm it becomes unknown and no duplicate POST or activation occurs.
- [x] Fail activation; confirm all creation IDs remain and no row is mislabeled as a creation failure.
- [x] Raise an unexpected task exception and check saved evidence, timing finalization, and claim cleanup.
- [x] Run processor tests with a fake implementation of `HospitalDirectoryAdapter`; assert requested operations and ordering without relying on HTTPX transport internals.

## Completion notes

Completed on 2026-11-18. Added `BatchProcessor` in `app/services/processor.py` and 24 processor test cases in `tests/services/test_processor.py`. Application lifespan creates one processor with its existing repository and Directory adapter and exposes it as `application.state.processor`. The existing lifecycle test verifies that wiring without making live requests. No dependencies, database columns, or public response fields were added.

Implementation flow:

1. `process_batch(batch_id)` uses the repository's atomic `start_attempt` operation as its sole execution guard. Missing, active, completed, and paused-but-not-requeued work is ignored. A short shield resolves claim ownership if cancellation lands during the commit; no network workflow is shielded as a whole.
2. Load the saved manifest. Existing `UNKNOWN` or abandoned `CREATING` evidence stops the attempt for reconciliation. Skip confirmed creations and permanent failures; schedule only pending or established-safe retryable input.
3. `_create_row` saves `CREATING` before calling the injected interface, then immediately checkpoints a confirmed ID or classified failure. It adds no HTTP code, retry loop, connection client, or ORM query. Concurrency and safe transport retries remain in the shared adapter.
4. `_create_rows` gathers every row task with independent outcomes and drains them on cancellation. A failure or independently cancelled task does not discard peer successes. Attempted creation without a saved outcome becomes `UNKNOWN`; uncertainty takes priority over definite peer failure when choosing the final batch state.
5. `_settle_batch` reads the saved manifest once, retains successful IDs, converts abandoned intent conservatively, and ends partial work as `FAILED` or `RECONCILIATION_REQUIRED`. Cancellation without uncertain writes can end as `INTERRUPTED`. Saved timing excludes idle gaps between attempts; finalization releases the finished attempt's admission slot.
6. `_activate` crosses the repository's complete-manifest barrier, calls the adapter once, and uses `complete_activation` to atomically save row activation, batch completion, and duration. Rejected/known-unsent activation failure keeps all creation IDs in `CREATED`; uncertain activation or a lost local completion checkpoint requires reconciliation. Activation errors never become creation failures.

Unexpected row exceptions are logged with batch/row correlation and leave conservative saved evidence after peers settle. Unexpected orchestration/storage errors are logged and propagated after state finalization where storage remains available. Caller cancellation is propagated after draining row tasks and cleanup; neither cancellation nor an internal exception proves remote rejection. Short docstrings explain function purpose, and comments explain claims, cancellation, persistence boundaries, and activation safety.

Verification commands:

```bash
uvx ruff check app/services/processor.py app/main.py tests/services/test_processor.py tests/adapters/test_directory_adapter.py
uvx ruff format --check app/services/processor.py app/main.py tests/services/test_processor.py tests/adapters/test_directory_adapter.py
.venv/bin/python -m pytest -q
```

- **191 tests passed**, no warnings, in 7.24 seconds: 167 existing cases and 24 new processor cases.
- Lint and formatting checks passed for all four touched Python files.
- Three-hospital success was gated to return row 3 first. Its ID was saved while rows 1/2 were still in flight; activation observed all three committed IDs and the `ACTIVATING` state. Final result ordering and the exact existing seven-field public schema were preserved.
- Verified permanent, safely retryable, and uncertain City Care failures; mixed definite/uncertain failures; activation failure categories; unexpected and independently cancelled row tasks; local success-checkpoint failure; and unexpected activation errors before/after acknowledgement.
- Verified competing execution calls, cancellation during creation/activation/claim return, claim release, preservation of pending input and confirmed IDs, old uncertainty blocking new POSTs, safe requeued work skipping successes/permanent failures, activation-only attempts, and cumulative timing excluding a one-hour idle interval.
- Processor tests use a typed fake adapter and temporary SQLite databases. No live Directory requests were sent.

Step 07 remains unstarted. FastAPI `BackgroundTasks` scheduling, upload acceptance, and result polling routes are added there. Reconciliation remains step 08; the future resume service in step 09 must establish safe eligibility, including resolving uncertain activation, before requeueing an attempt. Tests call the existing repository requeue operation directly to verify processor behavior, without adding a resume API.
