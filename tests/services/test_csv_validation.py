"""CSV boundaries, normalization, structured errors, and bounded upload reads."""

from codecs import BOM_UTF8

import pytest

from app.domain import HospitalInput, RowState
from app.services.csv_validation import (
    CSVValidationError,
    UploadTooLargeError,
    validate_csv_bytes,
    validate_csv_upload,
)

LIMIT = 1_048_576
SAMPLE = (
    b"name,address,phone\n"
    b"General Hospital,123 Main Street,555-0101\n"
    b"City Care Hospital,45 Park Road,555-0102\n"
    b"Sunrise Hospital,8 Lake Road,555-0103\n"
)


def validate(content: bytes):
    """Run byte validation with the normal configured-size budget so each test focuses
    on CSV behavior rather than repeating setup.
    """
    return validate_csv_bytes(content, max_upload_bytes=LIMIT)


def test_three_hospitals_become_ordered_pending_domain_rows() -> None:
    """Verify normalization returns complete fresh domain input, with stable row
    identities and no invented remote IDs or errors.
    """
    rows = validate(SAMPLE)

    assert [row.row_number for row in rows] == [1, 2, 3]
    assert [row.hospital for row in rows] == [
        HospitalInput("General Hospital", "123 Main Street", "555-0101"),
        HospitalInput("City Care Hospital", "45 Park Road", "555-0102"),
        HospitalInput("Sunrise Hospital", "8 Lake Road", "555-0103"),
    ]
    assert all(row.state == RowState.PENDING for row in rows)
    assert all(row.hospital_id is None and row.failure_kind is None for row in rows)


@pytest.mark.parametrize(
    ("content", "phone"),
    [
        (b"name,address\nGeneral,Main Street\n", None),
        (b"name,address,phone\nGeneral,Main Street,\n", None),
        (b"name,address,phone\nGeneral,Main Street,   \n", None),
        (b"name,address,phone\nGeneral,Main Street,00123\n", "00123"),
        (
            b"name,address,phone\nGeneral,Main Street,+91 (123) ext 4\n",
            "+91 (123) ext 4",
        ),
    ],
)
def test_phone_is_optional_and_remains_a_string(
    content: bytes, phone: str | None
) -> None:
    """Accept missing/empty optional phone values while preserving leading zeros and
    punctuation instead of applying numeric or format coercion.
    """
    assert validate(content)[0].hospital.phone == phone


def test_bom_header_order_and_surrounding_whitespace_are_supported() -> None:
    """Accept common spreadsheet-export variations without changing hospital values
    beyond the agreed surrounding-whitespace normalization.
    """
    content = BOM_UTF8 + (
        b" phone , address , name \r\n"
        b"  00123  ,  Main Street  ,  General Hospital  \r\n"
    )
    assert validate(content)[0].hospital == HospitalInput(
        name="General Hospital", address="Main Street", phone="00123"
    )


def test_quoted_commas_newlines_and_escaped_quotes_are_parsed_as_one_record() -> None:
    """Prove parsing uses CSV records rather than splitting physical lines or commas,
    preserving quoted address content.
    """
    content = (
        b'name,address\n"General, Hospital","123 Main Street\nBuilding ""A"""\n'
        b"City Care,Park Road\n"
    )
    rows = validate(content)

    assert rows[0].hospital == HospitalInput(
        name="General, Hospital", address='123 Main Street\nBuilding "A"'
    )
    assert [row.row_number for row in rows] == [1, 2]


def test_blank_records_are_ignored_without_gaps_in_data_row_numbers() -> None:
    """Keep the documented blank-record policy consistent with one-based result/error
    row numbering.
    """
    rows = validate(
        b"\n  \nname,address,phone\n\nGeneral,Main Street,\n,,\n  ,  , \n"
        b"City Care,Park Road,\n\n"
    )
    assert [row.row_number for row in rows] == [1, 2]


def test_identical_rows_are_not_silently_deduplicated() -> None:
    """Preserve user intent: equal hospital fields are not a guaranteed real-world
    identity or permission to drop an input row.
    """
    rows = validate(b"name,address\nGeneral,Main Street\nGeneral,Main Street\n")
    assert len(rows) == 2
    assert rows[0].hospital == rows[1].hospital
    assert [row.row_number for row in rows] == [1, 2]


@pytest.mark.parametrize("count", [1, 20])
def test_record_count_boundaries_are_accepted(count: int) -> None:
    """Exercise the accepted lower and upper hospital-count boundaries rather than only
    the three-record example.
    """
    content = b"name,address\n" + b"General,Main Street\n" * count
    assert len(validate(content)) == count


def test_twenty_first_nonblank_record_rejects_the_entire_upload() -> None:
    """Reject the explicit assignment limit before acceptance and count data records
    rather than blank physical lines.
    """
    content = b"name,address\n" + b"General,Main Street\n\n" * 21
    with pytest.raises(CSVValidationError) as caught:
        validate(content)
    assert caught.value.issues[-1].row == 21
    assert "at most 20" in caught.value.issues[-1].message


@pytest.mark.parametrize(
    "content",
    [b"", BOM_UTF8, b"\n  \n", b"name,address\n", b"name,address\n\n,\n"],
)
def test_empty_or_header_only_uploads_are_rejected(content: bytes) -> None:
    """Require at least one hospital; encoding markers, blank input, and a header alone
    do not create useful batch work.
    """
    with pytest.raises(CSVValidationError):
        validate(content)


@pytest.mark.parametrize(
    ("header", "field", "message"),
    [
        ("address", "name", "missing"),
        ("name", "address", "missing"),
        ("name,address,name", "name", "Duplicate"),
        ("name,address, name ", "name", "Duplicate"),
        ("name,address,email", "email", "Unexpected"),
        ("name,address,", None, "Unexpected"),
        ("Name,address", "Name", "Unexpected"),
    ],
)
def test_missing_duplicate_and_unexpected_headers_are_rejected(
    header: str, field: str | None, message: str
) -> None:
    """Validate column identity before mapping records, avoiding missing required input
    or silent duplicate-column overwrites.
    """
    with pytest.raises(CSVValidationError) as caught:
        validate((header + "\n").encode())
    assert any(
        issue.field == field and message in issue.message
        for issue in caught.value.issues
    )


@pytest.mark.parametrize(
    "record", ["General,Main Street", "General,Main Street,555,extra"]
)
def test_nonblank_records_must_have_the_declared_number_of_cells(record: str) -> None:
    """Reject extra/missing cells instead of truncating input or confusing a
    structurally absent cell with an allowed empty phone.
    """
    with pytest.raises(CSVValidationError) as caught:
        validate(("name,address,phone\n" + record + "\n").encode())
    assert caught.value.issues[0].row == 1
    assert "columns" in caught.value.issues[0].message


def test_all_row_and_required_field_errors_are_reported_without_partial_success() -> (
    None
):
    """Collect independent row/field issues while ensuring a valid middle row never
    escapes as a partially accepted upload.
    """
    content = (
        b"name,address,phone\n"
        b" ,Main Street,\n"
        b"City Care,  ,\n"
        b"General,Main Street,\n"
        b" , ,phone\n"
        b"Malformed,Main Street,phone,extra\n"
    )
    with pytest.raises(CSVValidationError) as caught:
        validate(content)

    assert [(issue.row, issue.field) for issue in caught.value.issues] == [
        (1, "name"),
        (2, "address"),
        (4, "name"),
        (4, "address"),
        (5, None),
    ]


@pytest.mark.parametrize(
    ("content", "row"),
    [
        (b'"name,address\n', None),
        (b'name,address\n"General,Main Street\n', 1),
        (b'name,address\n"General"x,Main Street\n', 1),
        (b'name,address\nGeneral,Main Street\n"City,Park Road', 2),
    ],
)
def test_malformed_quoting_is_a_structured_validation_error(
    content: bytes, row: int | None
) -> None:
    """Expose unrecoverable parser errors with the appropriate logical data-row
    identity, including separate header-error handling.
    """
    with pytest.raises(CSVValidationError) as caught:
        validate(content)
    assert caught.value.issues[-1].row == row
    assert "Malformed CSV" in caught.value.issues[-1].message


def test_invalid_encoding_is_rejected_without_replacement_characters() -> None:
    """Reject undecodable input instead of replacing bytes and potentially changing
    hospital identity during import.
    """
    with pytest.raises(CSVValidationError) as caught:
        validate(b"name,address\nGeneral,\xffStreet\n")
    assert "UTF-8" in caught.value.issues[0].message


def test_byte_limit_is_inclusive_and_checked_before_decoding() -> None:
    """Distinguish a valid exact-cap upload from a one-byte overflow, even when that
    extra byte would also be invalid UTF-8.
    """
    assert len(validate_csv_bytes(SAMPLE, max_upload_bytes=len(SAMPLE))) == 3
    with pytest.raises(UploadTooLargeError) as caught:
        validate_csv_bytes(SAMPLE + b"\xff", max_upload_bytes=len(SAMPLE))
    assert caught.value.max_upload_bytes == len(SAMPLE)


def test_byte_limit_counts_utf8_bytes_rather_than_characters() -> None:
    """Enforce the actual memory/upload budget for multibyte hospital text rather than
    counting decoded characters.
    """
    content = "name,address\nHôpital,Rue Émile\n".encode()
    with pytest.raises(UploadTooLargeError):
        validate_csv_bytes(content, max_upload_bytes=len(content.decode("utf-8")))
    assert validate(content)[0].hospital.name == "Hôpital"


def test_bounded_large_field_does_not_get_an_undocumented_lower_length_limit() -> None:
    """Ensure the standard parser's default field capacity does not reject otherwise
    valid input below our explicit byte cap.
    """
    address = "A" * 150_000
    rows = validate(("name,address\nGeneral," + address + "\n").encode())
    assert rows[0].hospital.address == address


class ChunkedUpload:
    """Short-read upload with a cursor, allowing actual consumption to be checked."""

    def __init__(self, content: bytes, chunk_size: int = 11) -> None:
        """Prepare an upload double that can return short chunks and record actual
        consumption, without a real HTTP request.
        """
        self.content = content
        self.chunk_size = chunk_size
        self.position = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        """Honor both the validator's requested bound and a smaller simulated transport
        chunk, exposing EOF through empty bytes.
        """
        assert size > 0, "Uploads must never be read without a bound"
        self.read_sizes.append(size)
        end = min(
            self.position + size, self.position + self.chunk_size, len(self.content)
        )
        chunk = self.content[self.position : end]
        self.position = end
        return chunk


@pytest.mark.asyncio
async def test_upload_reader_handles_short_reads_and_an_exact_size_limit() -> None:
    """Verify the upload helper continues through short reads and still accepts a
    complete file exactly at the byte cap.
    """
    upload = ChunkedUpload(SAMPLE)
    rows = await validate_csv_upload(upload, max_upload_bytes=len(SAMPLE))

    assert len(rows) == 3
    assert upload.position == len(SAMPLE)
    assert all(0 < size <= len(SAMPLE) + 1 for size in upload.read_sizes)


@pytest.mark.asyncio
async def test_upload_reader_stops_after_limit_plus_one_byte() -> None:
    """Confirm an oversized source is not consumed entirely; one extra byte is
    sufficient evidence for rejection.
    """
    upload = ChunkedUpload(SAMPLE * 100)
    limit = len(SAMPLE)
    with pytest.raises(UploadTooLargeError):
        await validate_csv_upload(upload, max_upload_bytes=limit)
    assert upload.position == limit + 1


@pytest.mark.asyncio
async def test_upload_reader_and_bytes_validator_report_the_same_row_errors() -> None:
    """Keep upload and dry-run validation consistent by proving they share
    parsing/error behavior.
    """
    content = b"name,address\nGeneral,\n"
    with pytest.raises(CSVValidationError) as direct:
        validate(content)
    with pytest.raises(CSVValidationError) as uploaded:
        await validate_csv_upload(ChunkedUpload(content), max_upload_bytes=LIMIT)
    assert uploaded.value.issues == direct.value.issues


def test_invalid_byte_limit_is_a_configuration_error() -> None:
    """Keep invalid service configuration distinct from user CSV errors; it must not be
    presented as an ordinary malformed row.
    """
    with pytest.raises(ValueError, match="positive integer"):
        validate_csv_bytes(SAMPLE, max_upload_bytes=0)
