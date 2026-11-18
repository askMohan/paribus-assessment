# 07 - Upload, validation-only, and polling API

Prerequisite: [06](06-batch-processing.md). Next: [08](08-reconciliation.md).

## Outcome

Clients can validate a CSV, submit it for `BackgroundTasks` execution, and poll the exact agreed comprehensive result.

## Files to create or update during implementation

`app/api/routes.py`, shared application-state/acceptance helpers in `app/api/dependencies.py`, and `tests/api/test_bulk_api.py`. Existing application wiring includes this router.

## Tasks

- [x] **07.01** Add multipart `POST /hospitals/bulk` with the field name `file` and the shared validator.
- [x] **07.02** Check the configured unfinished-batch admission limit atomically with registration/claim logic. Reject overload before accepting more work; do not hold a global lock during network calls.
- [x] **07.03** Generate the UUID and save the batch and normalized rows before scheduling. Do not register a task when validation or registration fails.
- [x] **07.04** Register the async processor through FastAPI `BackgroundTasks`; pass the batch identity and application-owned adapter/repository dependencies rather than a closing request-scoped database session. Routes do not create HTTPX clients or directly call the external API.
- [x] **07.05** Return `202`, a JSON body with only `batch_id`, and `Location` pointing to `/hospitals/batch/{batch_id}/status`.
- [x] **07.06** Add the validation-only endpoint; valid CSV returns `204`, invalid input uses `422`/`413`, and no batch is persisted.
- [x] **07.07** Add GET status lookup with UUID validation, `404` for missing batches, and `200` for any existing batch regardless of processing outcome.
- [x] **07.08** Build results explicitly from saved evidence using step 02 models. Preserve input order, nullable IDs, exact fields, and the public status mapping.
- [x] **07.09** Compute active elapsed time plus saved completed-attempt time without double counting. Do not include idle time before resume.
- [x] **07.10** Map standard validation, missing-resource, overload, and server errors without serializing internal error columns into comprehensive results.

## Verification

- [x] Confirm valid acceptance has exactly one body field and a correct `Location`; inspect persistence before allowing a controlled background processor to run.
- [x] Use a real async ASGI/HTTP harness or controlled processor barrier to demonstrate that acceptance does not wait for upstream completion. Account for test clients that await background work before returning control to the test.
- [x] Poll queued, partially created, activation-pending, and completed states; check the exact seven top-level and four row fields.
- [x] Confirm invalid CSV saves nothing and sends no upstream calls; validation-only also has no side effects.
- [x] Force registration failure and confirm no `202` or processing; check missing batch and admission-limit behavior.
- [x] Override the adapter dependency with a fake for API behavior checks; keep real HTTPX translation checks in the adapter tests.

## Completion notes

Completed on 2026-11-18. Added upload acceptance, validation-only, and batch polling routes, reusing the shared CSV validator, repository admission guard, existing response models, and application-owned processor. No new persistence columns, response fields, or dependencies were added.

- Upload validates the complete CSV, registers its UUID/rows atomically under the admission cap, then schedules `processor.process_batch` through `BackgroundTasks`. The response is `202` with only `batch_id`; `Location` contains the status path.
- Shared dependency helpers return application-owned settings/repository/processor; `accept_batch` sets the common Location/body contract for upload and resume. No request owns a long-lived ORM session or HTTP client.
- Validation-only returns empty `204` without persistence or processing. Both upload routes map structured CSV problems to `422` and the file-byte limit to `413`.
- Polling returns the existing seven-field result, preserving input order and current evidence. Duration is saved completed-attempt time plus only the active attempt's elapsed time. Missing batches return `404`; malformed UUIDs use standard `422` validation.
- Full capacity returns `503` before acceptance/scheduling. Application-level handlers map storage/capacity failures consistently across upload, polling, and resume. SQLAlchemy failures return generic `500` details; private diagnoses are logged, never included in comprehensive results.

Verification:

```bash
uvx ruff check app/api/routes.py app/api/dependencies.py tests/api/test_bulk_api.py
uvx ruff format --check app/api/routes.py app/api/dependencies.py tests/api/test_bulk_api.py
.venv/bin/python -m pytest -q tests/api/test_bulk_api.py
```

**19 tests passed** in 1.99 seconds. Tests cover exact acceptance/header/persistence ordering; queued, processing, activating, completed, failed, and unknown polling; a one-hour idle gap; missing/invalid UUIDs and multipart fields; invalid/oversized CSV; dry-run side effects; full admission; and failed registration/lookup without diagnostic leakage. A real processor with an injected fake Directory verifies successful upload-to-result behavior.

The acceptance timing test observes actual ASGI response frames: the complete `202` body is sent while controlled background work remains blocked, and a separate poll succeeds during that interval. It does not mistake HTTPX's background-task wait for the server's response-delivery behavior. All storage uses temporary databases and all external operations use fakes; no live Directory requests were sent.
