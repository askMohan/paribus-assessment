"""Internal workflow types, independent of HTTP and database libraries."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class BatchState(StrEnum):
    """Internal lifecycle of a whole CSV upload, saved in SQLite."""

    # Input is saved and accepted, but an execution attempt has not started.
    QUEUED = "QUEUED"
    # One caller has claimed the batch and may create eligible hospital rows.
    PROCESSING = "PROCESSING"
    # All local creations are confirmed; the Directory activation is being handled.
    ACTIVATING = "ACTIVATING"
    # Activation is confirmed, so every result may be reported as activated.
    COMPLETED = "COMPLETED"
    # A definite row/batch failure stopped this attempt; successes are retained.
    FAILED = "FAILED"
    # A remote write may have succeeded; inspect upstream evidence before replay.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    # A previous process stopped mid-attempt; recovery must inspect saved intent.
    INTERRUPTED = "INTERRUPTED"


class RowState(StrEnum):
    """What is known about one CSV row's remote creation and activation."""

    # Validated input exists, but no creation attempt has been started for it.
    PENDING = "PENDING"
    # Creation intent was saved before POST; this is not proof of remote success.
    CREATING = "CREATING"
    # A hospital ID is confirmed, but batch activation is still outstanding.
    CREATED = "CREATED"
    # Batch activation is confirmed for this successful creation.
    ACTIVATED = "ACTIVATED"
    # The attempt definitely failed; failure_kind decides whether retry is safe.
    FAILED = "FAILED"
    # The request may have committed remotely despite a missing/invalid response.
    UNKNOWN = "UNKNOWN"

    @property
    def creation_confirmed(self) -> bool:
        """Identify outcomes that count as successfully processed hospitals."""
        return self in {RowState.CREATED, RowState.ACTIVATED}


class FailureKind(StrEnum):
    """Evidence-based recovery category, kept internal rather than returned by API."""

    # Evidence establishes no creation occurred, so a bounded retry is permitted.
    RETRYABLE = "retryable"
    # The input was explicitly rejected; repeating it unchanged will not fix it.
    PERMANENT = "permanent"
    # Remote success is possible; reconciliation must precede another mutation.
    UNCERTAIN = "uncertain"


RESUMABLE_BATCH_STATES = (
    BatchState.FAILED,
    BatchState.RECONCILIATION_REQUIRED,
    BatchState.INTERRUPTED,
)


def can_create_hospital(state: RowState, failure_kind: FailureKind | None) -> bool:
    """Create only unsent input or a failure established safe to retry."""
    return state == RowState.PENDING or (
        state == RowState.FAILED and failure_kind == FailureKind.RETRYABLE
    )


class DirectoryOperation(StrEnum):
    """The external operation that failed, used to interpret retry evidence."""

    # Creates one hospital, so replay without evidence can create duplicates.
    CREATE_HOSPITAL = "create_hospital"
    # Reads batch evidence and can be retried after transient transport failures.
    GET_BATCH = "get_hospitals_by_batch"
    # Makes the batch active; a lost reply requires checking activation evidence.
    ACTIVATE_BATCH = "activate_batch"


class FailureEvidence(StrEnum):
    """Facts supplied by the adapter, independent of HTTPX exception classes."""

    # Connection/pool acquisition failed before the mutation could be sent.
    NOT_SENT = "not_sent"
    # The API explicitly rejected validation without creating the hospital.
    VALIDATION_REJECTED = "validation_rejected"
    # A transient rejection is documented/proven to have made no remote change.
    RETRYABLE_REJECTION = "retryable_rejection"
    # A timeout, interrupted request, or bad reply leaves remote state unresolved.
    UNKNOWN_OUTCOME = "unknown_outcome"


def classify_directory_failure(
    operation: DirectoryOperation, evidence: FailureEvidence
) -> FailureKind:
    """Choose a recovery category without assuming a failed reply means a failed write."""
    if evidence == FailureEvidence.VALIDATION_REJECTED:
        return FailureKind.PERMANENT
    if evidence in {FailureEvidence.NOT_SENT, FailureEvidence.RETRYABLE_REJECTION}:
        return FailureKind.RETRYABLE
    if operation == DirectoryOperation.GET_BATCH:
        return FailureKind.RETRYABLE
    return FailureKind.UNCERTAIN


class DirectoryOperationError(Exception):
    """HTTP-independent failure exposed by the Directory adapter."""

    def __init__(
        self,
        operation: DirectoryOperation,
        evidence: FailureEvidence,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Carry classified adapter failure information without leaking HTTPX types."""
        super().__init__(message)
        self.operation = operation
        self.evidence = evidence
        self.failure_kind = classify_directory_failure(operation, evidence)
        # A server delay hint never proves that replaying a mutation is safe.
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class HospitalInput:
    """Immutable hospital values normalized and validated by the CSV parser."""

    name: str
    address: str
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class DirectoryHospital:
    """Typed upstream evidence, insulated from HTTPX responses and raw JSON."""

    hospital_id: int
    name: str
    address: str
    creation_batch_id: UUID | None
    active: bool
    phone: str | None = None


def hospital_key(
    hospital: HospitalInput | DirectoryHospital,
) -> tuple[str, str, str | None]:
    """Match trimmed values exactly; preserve case, punctuation, and phone zeros."""
    return (
        hospital.name.strip(),
        hospital.address.strip(),
        (hospital.phone or "").strip() or None,
    )


@dataclass(frozen=True, slots=True)
class HospitalRow:
    """Immutable snapshot of one saved input record and its current outcome."""

    row_number: int
    hospital: HospitalInput
    state: RowState = RowState.PENDING
    hospital_id: int | None = None
    failure_kind: FailureKind | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    """Batch metadata and rows read together from one database transaction."""

    batch_id: UUID
    state: BatchState
    total_hospitals: int
    batch_activated: bool
    processing_time_seconds: float
    attempt_started_at: datetime | None
    last_error: str | None
    rows: tuple[HospitalRow, ...]

    @property
    def can_resume(self) -> bool:
        """Allow paused work with a safe next action; permanent-only failure stops."""
        if self.state not in RESUMABLE_BATCH_STATES or self.batch_activated:
            return False
        return (
            self.creation_complete
            or self.has_unknown_creations
            or any(
                can_create_hospital(row.state, row.failure_kind) for row in self.rows
            )
        )

    @property
    def has_unknown_creations(self) -> bool:
        """Unknown or abandoned in-flight work needs evidence before another POST."""
        return any(
            row.state in {RowState.UNKNOWN, RowState.CREATING} for row in self.rows
        )

    @property
    def needs_reconciliation(self) -> bool:
        """Preserve recovery context before an atomic claim changes state to QUEUED."""
        return (
            self.state in {BatchState.RECONCILIATION_REQUIRED, BatchState.INTERRUPTED}
            or self.has_unknown_creations
            or self.creation_complete
        )

    @property
    def creation_complete(self) -> bool:
        """Known IDs permit activation recovery without recreating hospitals."""
        return len(self.rows) == self.total_hospitals and all(
            row.state == RowState.CREATED and row.hospital_id is not None
            for row in self.rows
        )

    @property
    def processed_hospitals(self) -> int:
        """Derive the current successful-creation count instead of persisting a second
        counter.
        """
        return sum(row.state.creation_confirmed for row in self.rows)

    @property
    def failed_hospitals(self) -> int:
        """Count current definite row failures without confusing them with activation
        errors.
        """
        return sum(row.state == RowState.FAILED for row in self.rows)
