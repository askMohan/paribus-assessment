# 09 - Resume endpoint

Prerequisite: [08](08-reconciliation.md). Next: [10](10-restart-recovery.md).

## Outcome

A failed or unresolved batch can safely continue using the same UUID and saved successes.

## Files to create or update during implementation

`app/api/routes.py`, shared helpers in `app/api/dependencies.py`, `app/domain.py`, repository claim operations, processor integration, and `tests/api/test_resume.py`.

## Tasks

- [x] **09.01** Add POST `/hospitals/batch/{batch_id}/resume` and load the saved batch and rows.
- [x] **09.02** Return `404` for a missing UUID and `409` for active/completed batches or batches with no eligible recovery work.
- [x] **09.03** Determine eligibility from evidence: pending rows, safely retryable definitive failures, unresolved outcomes needing another lookup, or remaining activation. Permanent rejected input is not eligible for unchanged-input retry.
- [x] **09.04** Atomically claim resume before scheduling so simultaneous requests cannot both pass the eligibility check.
- [x] **09.05** Return `202` with only the existing `batch_id` and a `Location` header; schedule through `BackgroundTasks`.
- [x] **09.06** Reuse the existing processor and its injected `HospitalDirectoryAdapter`: skip known creations, reconcile uncertain rows, process safe work, and activate only once ready. Do not add a second external-request path to resume.
- [x] **09.07** Retain historical confirmed IDs. Update current failed counts when eligible work actually changes state; keep unknown outcomes outside definitive failed counts.
- [x] **09.08** Accumulate attempt duration without adding the user's idle wait between attempts.
- [x] **09.09** Release the claim on all completion/failure paths; leave unresolved results inspectable without a new public `can_resume` field.

## Verification

- [x] General/Sunrise IDs are saved and City Care has a safe retryable failure: resume creates only City Care, then activates the same batch.
- [x] All three IDs exist but activation failed: resume performs no creation POSTs.
- [x] Unknown City Care outcome: resume reads/reconciles first and never blindly repeats POST.
- [x] Permanent-only failure returns conflict; active/completed/missing cases return the documented codes.
- [x] Fire two resume requests concurrently; exactly one is accepted and one receives `409`.
- [x] Compare timing across two attempts with a simulated idle gap and confirm the gap is excluded.

## Completion notes

Completed on 2026-11-18. Added resume to the shared API router and reused the existing acceptance helpers, repository, processor, and adapter. No database columns, response fields, dependencies, or separate processing path were added.

- `Batch.can_resume` identifies paused work with pending input, safe retryable failures, unresolved creation, or complete creation awaiting activation. Permanent-only failures, active work, and completed batches are rejected. `claim_for_resume` rechecks eligibility and admission inside its write transaction before changing state to `QUEUED`.
- The endpoint returns `404` for missing batches, `409` for ineligible/already claimed work, `503` at capacity, and generic `500` details for storage failure. Accepted work returns `202` with only the original `batch_id`; the status path remains in `Location`.
- `Batch.needs_reconciliation` carries recovery context into the existing background processor. The processor also inspects its fresh claimed snapshot, so a stale request hint cannot bypass lookup before activation recovery. Resume skips confirmed IDs, never blindly repeats unknown creation, and sends no independent upstream requests from the route.
- Current result counts follow the saved row evidence. Cumulative timing includes active attempts and excludes the wait before manual resume. Every completed/failed/unresolved attempt finalizes its timing and admission state through the existing repository operations.

Verification:

```bash
uvx ruff check app tests
uvx ruff format --check app tests
.venv/bin/python -m pytest -q tests/api/test_resume.py
.venv/bin/python -m pytest -q
```

- **16 resume API cases passed** in 3.49 seconds. Covered safe partial retry; activation-only recovery with active/inactive evidence; unknown creation present/absent upstream; permanent-only conflict; queued/processing/activating/completed conflict; missing/invalid UUIDs; concurrent claims; admission; and private storage errors.
- The two-request race accepts exactly one attempt while the other receives `409`. The saved General/Sunrise IDs are retained and only City Care is created on a safe retry.
- A two-second saved attempt followed by a four-hour idle gap and two seconds of resumed work totals four seconds. Idle time is excluded.
- The HTTP restart integration seeds General as created, City as in-flight, and Sunrise as pending. Startup marks the batch interrupted; resume reads City's existing ID, creates only Sunrise, activates the same batch, and exposes the exact polling result.
- The full integrated suite passed **259 tests** in 14.35 seconds. Lint/format checks passed. Tests use temporary SQLite files and typed fake adapters; no live Directory mutations were made.
