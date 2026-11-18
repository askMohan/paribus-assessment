"""Public bulk response models containing only the agreed assignment fields."""

from collections.abc import Sequence
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain import HospitalRow, RowState


class HospitalStatus(StrEnum):
    """Public row status strings, mapped explicitly from internal RowState."""

    # Input was accepted but has not begun creation.
    PENDING = "pending"
    # A creation attempt is underway and no outcome is confirmed yet.
    PROCESSING = "processing"
    # Creation is confirmed, but the whole batch is not yet confirmed active.
    CREATED = "created"
    # This row has a definite failure, counted in failed_hospitals.
    FAILED = "failed"
    # Creation may have succeeded; it must not be counted as a definite failure.
    UNKNOWN = "unknown"
    # Final success value shown in the assignment after activation confirmation.
    CREATED_AND_ACTIVATED = "created_and_activated"


# Keep storage vocabulary out of the external contract. This is the only mapping
# needed to present internal row states through the existing public status field.
_PUBLIC_ROW_STATUSES = {
    RowState.PENDING: HospitalStatus.PENDING,
    RowState.CREATING: HospitalStatus.PROCESSING,
    RowState.CREATED: HospitalStatus.CREATED,
    RowState.ACTIVATED: HospitalStatus.CREATED_AND_ACTIVATED,
    RowState.FAILED: HospitalStatus.FAILED,
    RowState.UNKNOWN: HospitalStatus.UNKNOWN,
}


class BatchAccepted(BaseModel):
    """Minimal 202 body identifying the saved batch; Location is an HTTP header."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID


class HospitalResult(BaseModel):
    """One public hospital outcome using only the assignment's four row fields."""

    model_config = ConfigDict(extra="forbid")

    row: int = Field(ge=1, le=20)
    hospital_id: int | None
    name: str = Field(min_length=1)
    status: HospitalStatus

    @model_validator(mode="after")
    def confirmed_creation_requires_id(self) -> Self:
        """Prevent a public success claim when no upstream hospital identity is known."""
        if (
            self.status
            in {HospitalStatus.CREATED, HospitalStatus.CREATED_AND_ACTIVATED}
            and self.hospital_id is None
        ):
            raise ValueError("A confirmed creation must have a hospital_id")
        return self


class BatchResult(BaseModel):
    """The assignment's seven-field comprehensive result, including live snapshots."""

    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    total_hospitals: int = Field(ge=1, le=20)
    processed_hospitals: int = Field(ge=0, le=20)
    failed_hospitals: int = Field(ge=0, le=20)
    processing_time_seconds: float = Field(ge=0, allow_inf_nan=False)
    batch_activated: bool
    hospitals: list[HospitalResult]

    @field_validator("processing_time_seconds")
    @classmethod
    def round_processing_time(cls, value: float) -> float:
        """Give polling and completed results a consistent millisecond precision."""
        return round(value, 3)

    @model_validator(mode="after")
    def results_match_rows(self) -> Self:
        """Validate the response as one coherent snapshot rather than independent
        fields.
        """
        if [hospital.row for hospital in self.hospitals] != list(
            range(1, self.total_hospitals + 1)
        ):
            raise ValueError("Results must include every input row once, in CSV order")

        processed = sum(
            hospital.status
            in {HospitalStatus.CREATED, HospitalStatus.CREATED_AND_ACTIVATED}
            for hospital in self.hospitals
        )
        failed = sum(
            hospital.status == HospitalStatus.FAILED for hospital in self.hospitals
        )
        if self.processed_hospitals != processed or self.failed_hospitals != failed:
            raise ValueError("Hospital counts must match the confirmed row outcomes")

        all_activated = all(
            hospital.status == HospitalStatus.CREATED_AND_ACTIVATED
            for hospital in self.hospitals
        )
        if self.batch_activated != all_activated:
            raise ValueError("batch_activated must match confirmed row activation")
        return self

    @classmethod
    def from_rows(
        cls,
        *,
        batch_id: UUID,
        total_hospitals: int,
        rows: Sequence[HospitalRow],
        processing_time_seconds: float,
        batch_activated: bool,
    ) -> Self:
        """Project saved state into only the documented public response fields."""
        return cls(
            batch_id=batch_id,
            total_hospitals=total_hospitals,
            processed_hospitals=sum(row.state.creation_confirmed for row in rows),
            failed_hospitals=sum(row.state == RowState.FAILED for row in rows),
            processing_time_seconds=processing_time_seconds,
            batch_activated=batch_activated,
            hospitals=[
                HospitalResult(
                    row=row.row_number,
                    hospital_id=row.hospital_id,
                    name=row.hospital.name,
                    status=_PUBLIC_ROW_STATUSES[row.state],
                )
                for row in sorted(rows, key=lambda item: item.row_number)
            ],
        )
