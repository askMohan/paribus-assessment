# 03 - CSV validation

Prerequisite: [02](02-contracts-and-states.md). Next: [04](04-sqlite-storage.md).

## Outcome

One reusable validator returns normalized hospital rows or useful validation errors. It has no database or upstream side effects.

## Files to create or update during implementation

`app/services/csv_validation.py` and `tests/services/test_csv_validation.py`.

## Tasks

- [x] **03.01** Read at most the configured upload byte limit plus one byte so oversized input is detected without unbounded loading.
- [x] **03.02** Decode UTF-8 and UTF-8 BOM; reject invalid encoding. Do not trust filename or MIME type alone as proof of valid CSV.
- [x] **03.03** Parse with the standard CSV parser, including quoted addresses containing commas.
- [x] **03.04** Require `name` and `address` headers, permit optional `phone`, and reject duplicate/unexpected headers.
- [x] **03.05** Detect malformed row widths and CSV parsing errors instead of silently dropping extra cells.
- [x] **03.06** Trim surrounding input whitespace, require nonblank name/address, and convert blank/missing phone to null. Do not add phone-format restrictions.
- [x] **03.07** Ignore fully blank records consistently, number remaining data records from 1, and require 1-20 hospital records.
- [x] **03.08** Preserve identical rows as distinct numbered input records.
- [x] **03.09** Gather row/field validation errors for the whole accepted-size input before returning normalized rows. Never partially accept a CSV.
- [x] **03.10** Return transport-independent validation results/errors so both upload routes can share the same logic. Map malformed input to `422` and oversized input to `413` at the API boundary later.

## Verification

- [x] Accept the shared three-row example, a file without the phone column, blank phone, BOM, quoted commas, and exactly 20 records.
- [x] Reject missing/duplicate headers, nonblank malformed rows, invalid encoding, missing required cell values, zero records, and 21 records.
- [x] Check byte-limit boundaries, blank-record numbering, and retention of identical rows.
- [x] Confirm the validator makes no database or HTTP calls.

## Completion notes

Completed on 2026-11-18. Added only the shared validation module, its tests, and planning updates; no upload endpoints or storage were implemented.

Implemented in `app/services/csv_validation.py`:

- `validate_csv_bytes(content, max_upload_bytes=...)` returns normalized pending `HospitalRow` objects or raises a structured validation error without returning partial rows.
- `validate_csv_upload(upload, max_upload_bytes=...)` supports async short reads and consumes at most the byte limit plus one byte. It leaves stream ownership with the caller and is structurally compatible with `UploadFile.read` without importing FastAPI.
- `CSVValidationError.issues` contains immutable message/row/field details. `UploadTooLargeError` carries the configured byte cap. Neither exception embeds an HTTP status; step 07 maps them to `422` and `413`.
- Header/cell whitespace is trimmed, phone strings retain leading zeros and punctuation, and omitted phone headers or blank phone cells become null.
- Strict standard-library CSV parsing supports quoted commas, escaped quotes, and multiline values. A row must have the number of cells declared by its header; when a phone column exists, a blank phone is an empty cell rather than a missing cell.
- Fully blank records include empty lines and whitespace/delimiter-only records. They do not create gaps in data-record numbering. Repeated identical nonblank rows remain separate rows.
- Row/field errors are aggregated through the allowed record range. Parsing stops at the 21st nonblank data record because the upload is already rejected; unrecoverable quoting errors also stop parsing rather than guessing later row boundaries.
- The standard CSV parser's process-wide field capacity is raised when needed to fit already byte-bounded input, avoiding an undocumented lower field-length limit. Parsing is synchronous in the planned single-event-loop application.

Verification command:

```bash
.venv/bin/python -m pytest -q
```

Result: **94 passed**, no warnings, in 1.01 seconds, including all 54 earlier tests and 40 new CSV checks.

Covered three-hospital normalization, optional phone, UTF-8/BOM and invalid encoding, quoted commas/newlines/escaped quotes, blank records, identical records, 1/20/21 record boundaries, invalid headers and widths, aggregated errors, exact byte limits, multibyte input, a bounded large field, async short reads, and limit-plus-one consumption. Reviewed imports: only standard-library modules and the existing domain types; no database or HTTP operations. No dependencies were added. Step 04 remains unstarted.
