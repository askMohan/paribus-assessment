"""Transport-independent failure decisions and preservation of creation evidence."""

import pytest

from app.domain import (
    DirectoryOperation,
    DirectoryOperationError,
    FailureEvidence,
    FailureKind,
    HospitalInput,
    HospitalRow,
    RowState,
    classify_directory_failure,
)


@pytest.mark.parametrize(
    ("operation", "evidence", "expected"),
    [
        (
            DirectoryOperation.CREATE_HOSPITAL,
            FailureEvidence.NOT_SENT,
            FailureKind.RETRYABLE,
        ),
        (
            DirectoryOperation.CREATE_HOSPITAL,
            FailureEvidence.VALIDATION_REJECTED,
            FailureKind.PERMANENT,
        ),
        (
            DirectoryOperation.CREATE_HOSPITAL,
            FailureEvidence.RETRYABLE_REJECTION,
            FailureKind.RETRYABLE,
        ),
        (
            DirectoryOperation.CREATE_HOSPITAL,
            FailureEvidence.UNKNOWN_OUTCOME,
            FailureKind.UNCERTAIN,
        ),
        (
            DirectoryOperation.GET_BATCH,
            FailureEvidence.UNKNOWN_OUTCOME,
            FailureKind.RETRYABLE,
        ),
        (
            DirectoryOperation.GET_BATCH,
            FailureEvidence.VALIDATION_REJECTED,
            FailureKind.PERMANENT,
        ),
        (
            DirectoryOperation.ACTIVATE_BATCH,
            FailureEvidence.NOT_SENT,
            FailureKind.RETRYABLE,
        ),
        (
            DirectoryOperation.ACTIVATE_BATCH,
            FailureEvidence.UNKNOWN_OUTCOME,
            FailureKind.UNCERTAIN,
        ),
    ],
)
def test_failure_classification_requires_operation_and_evidence(
    operation: DirectoryOperation, evidence: FailureEvidence, expected: FailureKind
) -> None:
    """Check recovery categories from operation plus established evidence, including
    why a lost GET differs from a lost creation or activation reply.
    """
    assert classify_directory_failure(operation, evidence) == expected


def test_activation_error_preserves_successful_creation_evidence() -> None:
    """Check the transport-independent error interface and demonstrate that activation
    uncertainty does not erase a confirmed hospital creation.
    """
    row = HospitalRow(
        row_number=1,
        hospital=HospitalInput(name="General Hospital", address="123 Main Street"),
        state=RowState.CREATED,
        hospital_id=101,
    )
    error = DirectoryOperationError(
        operation=DirectoryOperation.ACTIVATE_BATCH,
        evidence=FailureEvidence.UNKNOWN_OUTCOME,
        message="Activation response was lost",
    )

    with pytest.raises(DirectoryOperationError) as caught:
        raise error

    assert caught.value.operation == DirectoryOperation.ACTIVATE_BATCH
    assert caught.value.failure_kind == FailureKind.UNCERTAIN
    assert str(caught.value) == "Activation response was lost"
    assert row.state.creation_confirmed
    assert row.hospital_id == 101
    assert row.failure_kind is None
