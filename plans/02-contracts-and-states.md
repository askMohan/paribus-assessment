# 02 - Response contracts, states, failure decisions, and adapter interface

Prerequisite: [01](01-project-setup.md). Next: [03](03-csv-validation.md).

## Outcome

Public response models exactly match the agreed fields. Internal states and failure decisions have one shared definition, and an HTTP-independent adapter interface defines our external API boundary.

## Files to create or update during implementation

`app/api/schemas.py`, `app/domain.py`, `app/adapters/__init__.py`, `app/adapters/hospital_directory.py`, and focused schema/failure-decision tests. The concrete HTTPX adapter is implemented in step 05.

## Tasks

- [x] **02.01** Read the assignment example and `contracts.json`; distinguish our bulk-service contract from the upstream API contract.
- [x] **02.02** Define upload/resume acceptance with exactly one field: UUID `batch_id`.
- [x] **02.03** Define the comprehensive result with exactly `batch_id`, `total_hospitals`, `processed_hospitals`, `failed_hospitals`, `processing_time_seconds`, `batch_activated`, and `hospitals`.
- [x] **02.04** Define row results with exactly `row`, nullable `hospital_id`, `name`, and `status`. First data row is row 1. Preserve input order.
- [x] **02.05** Define public row values: `pending`, `processing`, `created`, `failed`, `unknown`, and `created_and_activated`. Do not expose internal batch status.
- [x] **02.06** Define internal batch states: `QUEUED`, `PROCESSING`, `ACTIVATING`, `COMPLETED`, `FAILED`, `RECONCILIATION_REQUIRED`, and `INTERRUPTED`.
- [x] **02.07** Define internal row states: `PENDING`, `CREATING`, `CREATED`, `ACTIVATED`, `FAILED`, and `UNKNOWN`.
- [x] **02.08** Define nullable `failure_kind` values: `retryable`, `permanent`, and `uncertain`. Clear the kind on confirmed creation success.
- [x] **02.09** Define failure decisions from operation and evidence, using the table below. HTTP status alone must not determine whether creation is safe to repeat.
- [x] **02.10** Define counts: confirmed creations/activations are processed; definitive current row failures are failed; unknown rows are neither. Activation failure is a batch error, not three row failures.
- [x] **02.11** Define processing time as cumulative active attempt time, excluding idle time before manual resume. Select one rounding convention for serialization.
- [x] **02.12** Define the minimal typed internal hospital input/results and classified operation error used at the external boundary. Keep these independent of HTTPX and public bulk-response serialization.
- [x] **02.13** Define `HospitalDirectoryAdapter` as a small `Protocol` with async `create_hospital(hospital, batch_id)`, `get_hospitals_by_batch(batch_id)`, and `activate_batch(batch_id)` operations. Return typed domain data/acknowledgement or classified errors; do not expose HTTPX responses/exceptions.

## Failure-decision rules

| Evidence | Saved outcome | Automatic creation retry |
| --- | --- | --- |
| Connection establishment or pool acquisition fails before POST transmission | Retryable failure | Allowed within attempt limit |
| Explicit upstream input-validation rejection | Permanent failure | No unchanged-input retry |
| Read/write timeout after POST may have been transmitted | Unknown, uncertain | Reconcile first |
| Ambiguous creation `5xx` or invalid creation success payload | Unknown, uncertain | Reconcile first |
| `429` without documented no-side-effect behavior | Uncertain unless rejection without creation is established | Do not assume safe |
| Interrupted in-flight POST | Unknown, uncertain | Reconcile first |
| Activation failure | Keep confirmed row creations; save batch error | No hospital recreation |
| Failed GET reconciliation | Preserve existing row evidence | Do not infer POST failure |

CSV rejection happens before registration and creates no saved row failures. An internal programming error must not be mislabeled as a permanent upstream input rejection.

## Verification

- [x] Serialize an accepted batch and the three-hospital completed example; check exact top-level and row field sets.
- [x] Check nullable IDs, row order, and creation/activation count semantics using behavioral examples.
- [x] Check decisions for known-unsent failure, explicit validation rejection, lost response, interrupted POST, and activation failure.
- [x] Review that the interface and domain types have no HTTPX or SQLite imports and can be implemented by a simple fake. Do not add network execution in this step.

## Completion notes

Completed on 2026-11-18 using the existing lightweight assessment design. No PostgreSQL jobs, separate workers, leases, or other production-PDF features were introduced.

Implemented:

- `app/api/schemas.py`: `BatchAccepted`, `HospitalResult`, and `BatchResult` reject extra fields. The result projection orders rows by their original CSV record number, derives counts from current outcomes, and never exposes internal failure information.
- Public validation prevents missing/duplicate row entries, success without an upstream ID, contradictory counts, and claiming activation for unconfirmed rows.
- Processing time uses cumulative active attempt duration and rounds to three decimal places. Actual timing and accumulation are implemented with the processor in step 06, not simulated here.
- `app/domain.py`: batch/row states, failure kinds, operation/evidence-based classification, a classified adapter exception, and immutable hospital input/result/row dataclasses. Confirmed-success rows use a null failure kind; persistence of transitions belongs to later steps.
- `app/adapters/hospital_directory.py`: a three-operation async `Protocol` with typed domain results and no HTTPX/SQLite imports. Activation returns `None` on acknowledgement or raises a classified operation error. The concrete adapter remains in step 05.
- `tests/test_domain.py` and `tests/api/test_schemas.py`: 28 additional checks for failure decisions, exact response shapes, ordering, unknown versus failed counts, activation outcomes, current resume-result counts, timing bounds/rounding, and inconsistent output rejection.

Verification:

```bash
.venv/bin/python -m pytest -q
```

Result: **54 passed**, no warnings, in 0.73 seconds, including all 26 step 01 tests.

Also exercised create/read/activate through a simple in-memory fake satisfying the adapter interface, and reviewed imports to confirm the domain/interface use only the standard library. No database or live Directory API was used. No new dependencies or endpoints were added. Step 03 remains unstarted.
