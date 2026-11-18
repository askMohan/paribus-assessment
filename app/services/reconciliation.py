"""Recover only hospital identities supported by consistent batch evidence."""

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from app.adapters.hospital_directory import HospitalDirectoryAdapter
from app.db.repository import BatchRepository
from app.domain import (
    Batch,
    DirectoryHospital,
    DirectoryOperationError,
    HospitalRow,
    RowState,
    hospital_key,
)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Internal decision evidence; it never expands the public response schema."""

    resolved: bool
    all_active: bool = False
    error: str | None = None


def _validate_records(
    batch: Batch, records: list[DirectoryHospital]
) -> dict[int, DirectoryHospital]:
    """Check known identities and membership before adopting any new evidence."""
    by_id = {}
    for record in records:
        if (
            type(record.hospital_id) is not int
            or type(record.active) is not bool
            or record.creation_batch_id != batch.batch_id
            or record.hospital_id in by_id
        ):
            raise ValueError("Invalid batch membership or duplicate hospital identity")
        by_id[record.hospital_id] = record
    for row in batch.rows:
        if row.hospital_id is not None:
            record = by_id.get(row.hospital_id)
            if (
                record is None
                or hospital_key(record) != hospital_key(row.hospital)
                or (row.state == RowState.ACTIVATED and not record.active)
            ):
                raise ValueError("Known hospital identity is missing or inconsistent")
    return by_id


def _match_unknown(
    batch: Batch, records: dict[int, DirectoryHospital]
) -> list[tuple[HospitalRow, DirectoryHospital]]:
    """Respect input multiplicity and keep incomplete identical groups unresolved."""
    known_ids = {row.hospital_id for row in batch.rows if row.hospital_id is not None}
    unknown = defaultdict(list)
    protected = set()
    for row in batch.rows:
        key = hospital_key(row.hospital)
        if row.state == RowState.UNKNOWN:
            unknown[key].append(row)
        elif row.hospital_id is None:
            protected.add(key)
    candidates = defaultdict(list)
    for identifier, record in records.items():
        if identifier not in known_ids:
            candidates[hospital_key(record)].append(record)
    for key, matches in candidates.items():
        if key in protected or len(matches) > len(unknown[key]):
            raise ValueError("Unexpected or ambiguously attributable hospital records")
    return [
        (row, record)
        for key, rows in unknown.items()
        if len(candidates[key]) == len(rows)
        for row, record in zip(
            rows,
            sorted(candidates[key], key=lambda item: item.hospital_id),
            strict=True,
        )
    ]


class BatchReconciler:
    """Read once per decision; the adapter owns the bounded GET retry budget."""

    def __init__(
        self, repository: BatchRepository, directory: HospitalDirectoryAdapter
    ) -> None:
        """Reuse application-owned persistence and typed external operations."""
        self._repository = repository
        self._directory = directory

    async def reconcile(self, batch_id: UUID) -> ReconciliationResult:
        """Preserve uncertainty when lookup is absent, inconsistent, or incomplete."""
        batch = await self._repository.mark_unconfirmed(
            batch_id, "Creation ended without confirmed evidence"
        )
        if batch is None:
            return ReconciliationResult(False, error="Batch is missing")
        try:
            records = _validate_records(
                batch, await self._directory.get_hospitals_by_batch(batch_id)
            )
            recoveries = _match_unknown(batch, records)
            unknown_count = sum(row.state == RowState.UNKNOWN for row in batch.rows)
            resolved = len(recoveries) == unknown_count
            all_active = bool(records) and all(
                record.active for record in records.values()
            )
            if any(record.active for record in records.values()) and not (
                all_active and resolved and len(records) == batch.total_hospitals
            ):
                raise ValueError("Activation evidence is incomplete or inconsistent")
        except (DirectoryOperationError, ValueError) as error:
            return ReconciliationResult(False, error=str(error))
        for row, record in recoveries:
            if not await self._repository.record_row_outcome(
                batch_id, row.row_number, hospital_id=record.hospital_id
            ):
                raise RuntimeError("Recovered identity could not be checkpointed")
        return ReconciliationResult(
            resolved,
            all_active,
            None
            if resolved
            else "Some creation outcomes remain unknown; absence does not prove rejection",
        )
