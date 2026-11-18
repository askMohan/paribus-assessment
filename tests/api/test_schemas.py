"""Exact public fields and consistent projections of saved row outcomes."""

from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.api.schemas import BatchAccepted, BatchResult, HospitalResult
from app.domain import FailureKind, HospitalInput, HospitalRow, RowState

BATCH_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


def sample_rows() -> list[HospitalRow]:
    """Build the same three fresh domain rows used throughout the walkthrough, keeping
    schema cases readable and independent of CSV parsing.
    """
    return [
        HospitalRow(
            row_number=1,
            hospital=HospitalInput(
                name="General Hospital", address="123 Main Street", phone="555-0101"
            ),
        ),
        HospitalRow(
            row_number=2,
            hospital=HospitalInput(
                name="City Care Hospital", address="45 Park Road", phone="555-0102"
            ),
        ),
        HospitalRow(
            row_number=3,
            hospital=HospitalInput(
                name="Sunrise Hospital", address="8 Lake Road", phone="555-0103"
            ),
        ),
    ]


def result_for(rows: list[HospitalRow], *, activated: bool = False) -> BatchResult:
    """Project a three-row test snapshot through the real public-result builder, using
    one fixed batch and duration to expose field/count/rounding behavior.
    """
    return BatchResult.from_rows(
        batch_id=BATCH_ID,
        total_hospitals=3,
        rows=rows,
        processing_time_seconds=8.61234,
        batch_activated=activated,
    )


def test_acceptance_serializes_only_batch_uuid() -> None:
    """Protect the minimal acceptance body: the polling URL belongs in the Location
    header, not an extra JSON field.
    """
    result = BatchAccepted(batch_id=BATCH_ID)
    assert result.model_dump(mode="json") == {"batch_id": str(BATCH_ID)}


@pytest.mark.parametrize("extra", [{"status": "QUEUED"}, {"status_url": "/status"}])
def test_acceptance_rejects_extra_fields(extra: dict[str, str]) -> None:
    """Reject accidental acceptance-response expansion rather than relying on callers
    to remember the agreed field set.
    """
    with pytest.raises(ValidationError):
        BatchAccepted.model_validate({"batch_id": BATCH_ID, **extra})


def test_completed_result_has_exact_documented_fields_in_csv_order() -> None:
    """Compare the complete documented result for out-of-order creation outcomes,
    proving serialization restores input order and exposes no internal fields.
    """
    rows = [
        replace(row, state=RowState.ACTIVATED, hospital_id=100 + row.row_number)
        for row in reversed(sample_rows())
    ]
    result = result_for(rows, activated=True)

    assert result.model_dump(mode="json") == {
        "batch_id": str(BATCH_ID),
        "total_hospitals": 3,
        "processed_hospitals": 3,
        "failed_hospitals": 0,
        "processing_time_seconds": 8.612,
        "batch_activated": True,
        "hospitals": [
            {
                "row": 1,
                "hospital_id": 101,
                "name": "General Hospital",
                "status": "created_and_activated",
            },
            {
                "row": 2,
                "hospital_id": 102,
                "name": "City Care Hospital",
                "status": "created_and_activated",
            },
            {
                "row": 3,
                "hospital_id": 103,
                "name": "Sunrise Hospital",
                "status": "created_and_activated",
            },
        ],
    }


def test_unknown_outcome_is_not_counted_as_a_definitive_failure() -> None:
    """Separate confirmed creation, definite failure, and uncertainty in one snapshot,
    including nullable IDs and hidden recovery classifications.
    """
    first, second, third = sample_rows()
    result = result_for(
        [
            replace(first, state=RowState.CREATED, hospital_id=101),
            replace(second, state=RowState.FAILED, failure_kind=FailureKind.RETRYABLE),
            replace(third, state=RowState.UNKNOWN, failure_kind=FailureKind.UNCERTAIN),
        ]
    )
    data = result.model_dump(mode="json")

    assert data["processed_hospitals"] == 1
    assert data["failed_hospitals"] == 1
    assert data["batch_activated"] is False
    assert [row["status"] for row in data["hospitals"]] == [
        "created",
        "failed",
        "unknown",
    ]
    assert [row["hospital_id"] for row in data["hospitals"]] == [101, None, None]
    assert all("failure_kind" not in row for row in data["hospitals"])


def test_pending_and_in_flight_rows_do_not_count_as_created() -> None:
    """Ensure accepted or started work is not advertised as a successful creation
    before confirmation arrives.
    """
    first, second, third = sample_rows()
    result = result_for([first, replace(second, state=RowState.CREATING), third])

    assert result.processed_hospitals == result.failed_hospitals == 0
    assert [row.status for row in result.hospitals] == [
        "pending",
        "processing",
        "pending",
    ]


def test_activation_pending_or_failed_does_not_count_as_failed_hospitals() -> None:
    """Keep successful creates visible when activation has not succeeded; a batch error
    must not inflate the row-failure count.
    """
    rows = [
        replace(row, state=RowState.CREATED, hospital_id=100 + row.row_number)
        for row in sample_rows()
    ]
    result = result_for(rows)

    assert result.processed_hospitals == 3
    assert result.failed_hospitals == 0
    assert not result.batch_activated
    assert all(row.status == "created" for row in result.hospitals)


def test_success_after_resume_excludes_historical_failure_from_current_counts() -> None:
    """Compare before/after snapshots so a recovered row leaves the current failed
    count instead of being counted as both successful and failed.
    """
    rows = [
        replace(row, state=RowState.CREATED, hospital_id=100 + row.row_number)
        for row in sample_rows()
    ]
    failed = replace(
        rows[1],
        state=RowState.FAILED,
        hospital_id=None,
        failure_kind=FailureKind.RETRYABLE,
    )
    before_resume = result_for([rows[0], failed, rows[2]])
    after_resume = result_for(rows)

    assert before_resume.failed_hospitals == 1
    assert after_resume.failed_hospitals == 0
    assert after_resume.processed_hospitals == 3
    assert rows[1].failure_kind is None


@pytest.mark.parametrize("elapsed", [-1.0, float("nan"), float("inf")])
def test_elapsed_time_must_be_nonnegative_and_finite(elapsed: float) -> None:
    """Reject impossible timing values that would make comprehensive results invalid or
    meaningless to clients.
    """
    with pytest.raises(ValidationError):
        BatchResult.from_rows(
            batch_id=BATCH_ID,
            total_hospitals=3,
            rows=sample_rows(),
            processing_time_seconds=elapsed,
            batch_activated=False,
        )


@pytest.mark.parametrize("extra", [{"progress_percentage": 0}, {"status": "QUEUED"}])
def test_comprehensive_result_rejects_extra_fields(extra: dict[str, object]) -> None:
    """Prevent an internal state or percentage from being added to the agreed seven-
    field public result.
    """
    data = result_for(sample_rows()).model_dump()
    with pytest.raises(ValidationError):
        BatchResult.model_validate({**data, **extra})


def test_row_result_rejects_internal_failure_fields() -> None:
    """Keep recovery details private even if a caller tries to pass them into a public
    row model.
    """
    with pytest.raises(ValidationError):
        HospitalResult.model_validate(
            {
                "row": 1,
                "hospital_id": None,
                "name": "General Hospital",
                "status": "unknown",
                "failure_kind": "uncertain",
            }
        )


def test_confirmed_creation_requires_a_known_hospital_id() -> None:
    """Reject a creation-success label when no confirmed upstream identity supports
    that claim.
    """
    with pytest.raises(ValidationError, match="hospital_id"):
        result_for([replace(row, state=RowState.CREATED) for row in sample_rows()])


def test_counts_cannot_contradict_row_outcomes() -> None:
    """Ensure callers cannot claim successful counts while all actual row results
    remain pending.
    """
    data = result_for(sample_rows()).model_dump()
    with pytest.raises(ValidationError, match="counts"):
        BatchResult.model_validate({**data, "processed_hospitals": 3})


def test_activation_cannot_be_claimed_for_pending_rows() -> None:
    """Reject false completion even when a caller explicitly supplies batch_activated
    as true.
    """
    with pytest.raises(ValidationError, match="activation"):
        result_for(sample_rows(), activated=True)


@pytest.mark.parametrize("case", ["missing", "duplicate"])
def test_every_input_row_must_be_represented_once(case: str) -> None:
    """Detect missing and duplicate row projections instead of silently changing the
    accepted manifest size.
    """
    rows = sample_rows()
    invalid_rows = rows[:2] if case == "missing" else [rows[0], rows[0], rows[2]]
    with pytest.raises(ValidationError, match="every input row"):
        result_for(invalid_rows)
