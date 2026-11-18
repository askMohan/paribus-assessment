"""Shared CSV validation without API, database, or upstream dependencies."""

import csv
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from typing import Protocol

from app.domain import HospitalInput, HospitalRow

# This comes from the assignment, not an environment-controlled capacity setting.
MAX_HOSPITALS = 20
_REQUIRED_COLUMNS = {"name", "address"}
_ALLOWED_COLUMNS = _REQUIRED_COLUMNS | {"phone"}


@dataclass(frozen=True, slots=True)
class CSVValidationIssue:
    """One transport-independent validation problem for a shared parser result."""

    message: str
    row: int | None = None
    field: str | None = None


class CSVValidationError(ValueError):
    """Invalid CSV; routes can map the issues to a standard 422 response."""

    def __init__(self, issues: list[CSVValidationIssue]) -> None:
        """Preserve all discovered validation issues for either upload route."""
        super().__init__("CSV validation failed")
        self.issues = tuple(issues)


class UploadTooLargeError(ValueError):
    """Byte-limit violation, mapped to 413 by the API boundary later."""

    def __init__(self, max_upload_bytes: int) -> None:
        """Retain the byte cap needed to explain a rejected upload."""
        super().__init__(f"CSV upload must not exceed {max_upload_bytes} bytes")
        self.max_upload_bytes = max_upload_bytes


class AsyncByteReader(Protocol):
    """Minimum upload behavior needed for bounded reading, without API coupling."""

    async def read(self, size: int = -1) -> bytes:
        """Describe the async file-read boundary without importing FastAPI."""
        ...


def _check_byte_limit(max_upload_bytes: int) -> None:
    """Reject invalid caller configuration before allocating or reading upload data."""
    if (
        isinstance(max_upload_bytes, bool)
        or not isinstance(max_upload_bytes, int)
        or max_upload_bytes <= 0
    ):
        raise ValueError("max_upload_bytes must be a positive integer")


async def validate_csv_upload(
    upload: AsyncByteReader, *, max_upload_bytes: int
) -> list[HospitalRow]:
    """Read a bounded upload and reuse the same parser as direct byte validation."""
    _check_byte_limit(max_upload_bytes)
    content = bytearray()
    # An exact-limit upload is valid; one extra byte is enough to detect overflow.
    # A short read is not necessarily EOF, so only empty bytes stop this loop.
    while len(content) <= max_upload_bytes:
        chunk = await upload.read(max_upload_bytes + 1 - len(content))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_upload_bytes:
            raise UploadTooLargeError(max_upload_bytes)
    return validate_csv_bytes(bytes(content), max_upload_bytes=max_upload_bytes)


def validate_csv_bytes(content: bytes, *, max_upload_bytes: int) -> list[HospitalRow]:
    """Validate the entire accepted CSV before any database or upstream side effect."""
    _check_byte_limit(max_upload_bytes)
    if len(content) > max_upload_bytes:
        raise UploadTooLargeError(max_upload_bytes)
    # utf-8-sig removes an optional BOM but otherwise remains strict UTF-8. Never
    # replace invalid bytes silently, since that could change hospital identity.
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CSVValidationError(
            [CSVValidationIssue(message="CSV must use UTF-8 encoding")]
        ) from None

    # The upload byte cap already bounds field size. Raise the process-wide CSV
    # parser capacity when needed so its default does not impose a lower limit.
    # Parsing here is synchronous in our single-event-loop application.
    if len(text) > csv.field_size_limit():
        csv.field_size_limit(len(text))

    # Let the CSV parser handle quotes/embedded newlines. Splitting on physical
    # lines or commas would corrupt quoted addresses and data-record numbering.
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    return _read_rows(reader, _read_header(reader))


def _read_header(reader: Iterator[list[str]]) -> list[str]:
    """Require unique supported columns before any record can be mapped."""
    try:
        header = next((record for record in reader if not _blank(record)), None)
    except csv.Error as error:
        raise CSVValidationError(
            [CSVValidationIssue(message=f"Malformed CSV header: {error}")]
        ) from None
    if header is None:
        raise CSVValidationError([CSVValidationIssue(message="CSV header is required")])

    columns = [column.strip() for column in header]
    issues = _header_issues(columns)
    if issues:
        raise CSVValidationError(issues)
    return columns


def _read_rows(reader: Iterator[list[str]], columns: list[str]) -> list[HospitalRow]:
    """Collect row errors while enforcing the fixed 1–20 hospital limit."""

    rows: list[HospitalRow] = []
    issues: list[CSVValidationIssue] = []
    record_number = 0
    try:
        records = (record for record in reader if not _blank(record))
        for record_number, record in enumerate(records, start=1):
            if record_number > MAX_HOSPITALS:
                issues.append(
                    CSVValidationIssue(
                        message=f"CSV must contain at most {MAX_HOSPITALS} hospitals",
                        row=record_number,
                    )
                )
                break
            try:
                rows.append(_parse_row(record, columns, record_number))
            except CSVValidationError as error:
                issues.extend(error.issues)
    except csv.Error as error:
        issues.append(
            CSVValidationIssue(message=f"Malformed CSV: {error}", row=record_number + 1)
        )
    if record_number == 0 and not issues:
        issues.append(
            CSVValidationIssue(message="CSV must contain at least one hospital")
        )
    # Valid rows may have been collected before a later error. Never return that
    # partial list: upload acceptance requires the complete CSV to be valid.
    if issues:
        raise CSVValidationError(issues)
    return rows


def _parse_row(record: list[str], columns: list[str], row_number: int) -> HospitalRow:
    """Validate one record's width/required values and normalize optional phone."""
    # Check width before mapping: zip must not silently discard or invent cells.
    if len(record) != len(columns):
        raise CSVValidationError(
            [
                CSVValidationIssue(
                    message=f"Expected {len(columns)} columns, received {len(record)}",
                    row=row_number,
                )
            ]
        )
    values = dict(zip(columns, (value.strip() for value in record), strict=True))
    issues = [
        CSVValidationIssue(
            message=f"{field.capitalize()} must not be empty",
            row=row_number,
            field=field,
        )
        for field in ("name", "address")
        if not values[field]
    ]
    if issues:
        raise CSVValidationError(issues)
    return HospitalRow(
        row_number=row_number,
        hospital=HospitalInput(
            values["name"], values["address"], values.get("phone") or None
        ),
    )


def _blank(record: list[str]) -> bool:
    """Apply one blank-record rule to both header discovery and data numbering."""
    return not any(value.strip() for value in record)


def _header_issues(columns: list[str]) -> list[CSVValidationIssue]:
    """Check header identity before mapping cells into named input fields."""
    # Count duplicates before converting names to a set or mapping row values;
    # those conversions would otherwise hide repeated headers by overwriting.
    issues = []
    for column, count in Counter(columns).items():
        if count > 1:
            issues.append(
                CSVValidationIssue(message="Duplicate CSV column", field=column or None)
            )
    for column in sorted(_REQUIRED_COLUMNS - set(columns)):
        issues.append(
            CSVValidationIssue(message="Required CSV column is missing", field=column)
        )
    for column in sorted(set(columns) - _ALLOWED_COLUMNS):
        issues.append(
            CSVValidationIssue(
                message=f"Unexpected CSV column: {column or '<empty>'}",
                field=column or None,
            )
        )
    return issues
