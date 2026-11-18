# 04 - SQLite tables and repository

Prerequisite: [03](03-csv-validation.md). Next: [05](05-directory-client.md).

## Outcome

Batches, validated inputs, and row outcomes can be registered and updated atomically through SQLAlchemy ORM on SQLite and read after reopening the database.

## Files to create or update during implementation

`app/db/models.py`, `app/db/database.py`, `app/db/repository.py`, application lifecycle wiring, dependency pins, and `tests/db/test_repository.py`.

## Tasks

- [x] **04.01** Initialize the configured SQLite file and parent directory; enable foreign-key enforcement on connections. Use isolated temporary files for tests.
- [x] **04.02** Map `batches` with SQLAlchemy ORM and the seven agreed columns: UUID, internal status, total, activation flag, accumulated time, attempt start, and batch error.
- [x] **04.03** Map `hospital_rows` with SQLAlchemy ORM, including batch UUID, row number, name, address, nullable phone/ID, internal status, failure kind, and error.
- [x] **04.04** Enforce unique `(batch_id, row_number)` and unique `(batch_id, hospital_id)` for non-null IDs. Add the batch foreign key and appropriate state/nonnull constraints.
- [x] **04.05** Add atomic registration of one batch and all validated rows. Roll back the entire registration if any row insert fails.
- [x] **04.06** Add ordered batch/row reads and derived processed/failed counts.
- [x] **04.07** Add guarded creation intent and one reusable `record_row_outcome` method for confirmed IDs, definite failures, and uncertain outcomes.
- [x] **04.08** Add explicit batch writes for attempt start/end, activation start/completion, and errors. Preserve confirmed IDs when updating failure state.
- [x] **04.09** Add compare-and-set state claims needed for scheduling/resume, plus queries for interrupted active batches and accepted unfinished-work counts.
- [x] **04.10** Use a fresh async SQLAlchemy session per short operation, shared repository helpers, and serialized writes where needed. Never share sessions across concurrent tasks or hold transactions during HTTP calls.
- [x] **04.11** Choose an initial schema-initialization approach suitable for a new assignment application; do not introduce a migration framework without a need.

## Verification

- [x] Register the three-hospital input; check one batch, three pending rows, correct order, and zero processed/failed counts.
- [x] Force one insert to fail and verify registration leaves no partial batch or rows.
- [x] Confirm duplicate row identities and assigning one upstream ID to two batch rows are rejected.
- [x] Save two successes and one unknown; confirm processed 2, failed 0, and IDs survive closing/reopening the file.
- [x] Exercise competing claims and confirm only one succeeds.

## Completion notes

Completed on 2026-11-18 and refactored to SQLAlchemy ORM at the user's request. The lightweight SQLite/BackgroundTasks design and all public response fields remain unchanged. Step 05 remains unstarted.

Current implementation:

- `app/db/models.py` maps exactly `batches` and `hospital_rows` using SQLAlchemy declarative models, a relationship ordered by original input position, typed enum columns, and generated constraints/indexes. Existing table/column names, hyphenated UUID strings, lowercase failure kinds, and ISO timestamps remain compatible with pre-ORM files.
- A reusable `UTCDateTime` mapped type centralizes aware-clock conversion. Model snapshot methods centralize conversion to immutable domain data; no live ORM entity leaks into public responses.
- `app/db/database.py` owns an async engine and session factory. Each repository operation gets a fresh `AsyncSession` and one commit/rollback boundary. Local locking protects lifecycle cleanup; SQLite write transactions preserve atomic claims across separate connections. No session is shared across concurrent tasks or retained during HTTP calls.
- SQLite-specific foreign-key enablement and BEGIN/BEGIN IMMEDIATE control remain isolated driver hooks. Repository reads/writes and schema declarations use SQLAlchemy ORM/expressions; there are no hand-written repository data queries or SQL trigger fixtures.
- `app/db/repository.py` consolidates success/failure/unknown persistence into `record_row_outcome`. A success requires `hospital_id`; a failure/unknown requires `failure_kind` and `error`. Mixed/incomplete arguments are rejected before mutation. Uncertainty becomes UNKNOWN, not a definite failure.
- Shared helpers cover transaction/eager loading/state filtering, manifest row lookup, admission counting, activation readiness, and active-attempt finalization. Meaningful public lifecycle operations remain explicit so a generic setter cannot bypass their distinct prerequisites.
- Known successes cannot be overwritten by different IDs or late failures. Unknown/permanent rows cannot start a new creation. Unambiguously recovered IDs clear previous failure evidence.
- Admission and resume still count QUEUED/PROCESSING/ACTIVATING atomically; paused work releases capacity. Attempt durations still exclude idle gaps and cannot be finalized twice.
- Constraint tests now use ORM mutations and SQLAlchemy exceptions. Completion rollback is tested with a mapper flush event instead of a handwritten SQL trigger. The cancellation test flushes a real ORM insert before cancellation.
- Existing `data/hospitals.db` is retained. Schema initialization uses metadata.create_all, never drop/recreate, and no migration framework or production queue was added.

Consolidated outcome examples:

```python
await repository.record_row_outcome(batch_id, 1, hospital_id=101)
await repository.record_row_outcome(
    batch_id, 2, failure_kind=FailureKind.RETRYABLE, error="Request not sent"
)
await repository.record_row_outcome(
    batch_id, 3, failure_kind=FailureKind.UNCERTAIN, error="Response lost"
)
```

Verification:

```bash
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

- Final refactor suite: **114 passed**, no warnings, in 2.31 seconds. All previous 110 checks pass through the updated ORM/internal API, with four additional invalid-outcome-argument cases.
- All 27 installed packages are compatible. SQLAlchemy 2.1.9 and greenlet 3.5.6 were added and pinned; aiosqlite remains the async SQLite driver. Existing runtime and test package versions were retained.
- Tests retain rollback, foreign-key/uniqueness, concurrent row writes, activation barrier/completion atomicity, persistence, timing, competing claims across independent engines, admission races, and cancellation coverage.
- A separate temporary file was initialized with the exact pre-refactor schema and seeded with a confirmed row, unknown row, retryable failure, prior duration, and timezone-aware marker. ORM loading, safe recovery, activation, and reopening all passed without replacing the schema or losing IDs/timing.
- Initialized the existing default file through the ORM and confirmed zero active records. No live Directory API requests were made. Earlier step 04 verification also confirmed real single-worker health/docs startup and an empty default database.

The initial non-ORM implementation had 110 passing tests; it is superseded by the mapped implementation above. Restart orchestration remains step 10; adapter and processing implementation remain steps 05-06.
