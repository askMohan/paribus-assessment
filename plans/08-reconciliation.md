# 08 - Uncertain-outcome reconciliation

Prerequisite: [07](07-upload-and-polling-api.md). Next: [09](09-resume-api.md).

## Outcome

The processor can recover unambiguous upstream outcomes without blindly recreating hospitals or claiming success from missing evidence.

## Files to create or update during implementation

`app/services/reconciliation.py`, processor integration, and `tests/services/test_reconciliation.py`.

## Tasks

- [x] **08.01** Load expected input, known IDs, and unresolved rows for one batch; call injected `HospitalDirectoryAdapter.get_hospitals_by_batch` using that UUID. Consume typed records and classified errors, not HTTPX responses/exceptions.
- [x] **08.02** Validate batch membership and identify already-known IDs before considering unmatched records. Unexpected records or inconsistent evidence block automatic completion.
- [x] **08.03** Match remaining outcomes using conservative name/address/phone comparisons. Reuse input normalization; do not use fuzzy hospital identity matching.
- [x] **08.04** Account for record multiplicity when identical input rows exist. Never assign one upstream ID to two rows; preserve ambiguity rather than guessing about pending or in-flight rows.
- [x] **08.05** Persist recovered IDs and clear failure information only when an outcome can be accounted for confidently. Do not overwrite confirmed IDs with a different candidate.
- [x] **08.06** Add a bounded reconciliation-read policy. A missing record after any number of quick reads is not alone proof that a late creation is impossible.
- [x] **08.07** Keep unresolved rows `UNKNOWN` and the batch `RECONCILIATION_REQUIRED`; do not activate or automatically retry an ambiguous POST.
- [x] **08.08** Reconcile activation against all expected IDs. Confirm completion only when expected records are accounted for and active; preserve uncertainty if lookup fails or evidence is inconsistent.
- [x] **08.09** Permit another activation attempt only after a conclusive state check establishes the appropriate remaining operation. Never route activation recovery through hospital creation.
- [x] **08.10** Integrate reconciliation into the processor for uncertain creation/activation results without introducing an unbounded polling loop.

## Verification

- [x] Simulate City Care created upstream with its response lost; lookup recovers ID 102, no extra creation occurs, and activation can proceed.
- [x] Return no matching City Care immediately and then on all bounded reads; confirm uncertainty persists and POST is not repeated.
- [x] Fail the GET lookup; confirm no unknown row becomes definitively failed merely because reconciliation failed.
- [x] Test repeated identical input, multiple candidates, unexpected extra records, and one candidate that must not satisfy two rows.
- [x] Lose activation's response; all expected active IDs confirm completion, while inconsistent or missing evidence does not.
- [x] Use a fake adapter for reconciliation scenarios; verify lookup/recovery logic has no direct HTTP or concrete HTTPX adapter dependency.

## Completion notes

Completed on 2026-11-18. Added `BatchReconciler` and integrated it with the existing processor using typed domain evidence. No database columns, dependencies, HTTP wrappers, or public response fields were added.

Implementation:

- One lookup is made per reconciliation decision. The adapter owns bounded transient GET retries; the reconciler adds no nested retry or polling loop. Missing evidence stays uncertain across repeated manual attempts.
- Known IDs, batch membership, duplicate identities, and exact trimmed input values are validated before new IDs are adopted. Case, punctuation, and phone zeros are preserved. Unexpected extras, protected pending/failed input, missing known records, or inconsistent activation flags block completion.
- Unknown rows are grouped by identical normalized input. Only fully accounted groups receive distinct IDs; one candidate cannot satisfy two rows, and extra candidates are not guessed away. Independently resolved groups can be checkpointed while other groups remain unknown.
- Repository `mark_unconfirmed` converts all abandoned creation intent to unknown and returns a consistent snapshot in one transaction, shared by reconciliation and processor cancellation. Confirmed identities are retained; failed cleanup rolls back the whole update.
- `process_batch(..., reconcile_before_processing=True)` inspects uncertain/resumed work before mutations. The processor also rechecks `needs_reconciliation` on its fresh claimed snapshot so stale route hints cannot bypass activation inspection. Newly uncertain creation results are reconciled after all row tasks settle. Successful recovery can continue through the existing activation barrier.
- An uncertain activation response triggers inspection. All expected IDs active confirms completion locally. Conclusive inactive evidence defers another activation to a separately claimed manual attempt; each processing attempt sends at most one activation mutation. Activation-only resume performs no hospital creation.

Verification:

```bash
uvx ruff check app/services/reconciliation.py app/services/processor.py tests/services/test_reconciliation.py tests/services/test_processor.py
uvx ruff format --check app/services/reconciliation.py app/services/processor.py tests/services/test_reconciliation.py tests/services/test_processor.py
.venv/bin/python -m pytest -q tests/services/test_reconciliation.py tests/services/test_processor.py tests/services/test_restart_recovery.py
```

- **56 targeted tests passed**: reconciliation, existing processor behavior, and restart/manual-recovery integration. Lint and formatting checks passed.
- Covered lost creation acknowledgement, repeated absent evidence, failed lookup, exact duplicate multiplicity, known-ID priority, ambiguous pending input, wrong batches/duplicate IDs/extras/missing known records, partial recovery, inconsistent activation evidence, lost activation acknowledgement, activation-only resume, cancellation during lookup, and a stale-false route hint with newly saved creation IDs.
- All tests use fake adapters and temporary SQLite files. No live Directory requests were sent.
