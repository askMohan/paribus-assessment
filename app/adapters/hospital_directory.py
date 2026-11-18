"""HTTP-independent interface for the external Hospital Directory API."""

from typing import Protocol
from uuid import UUID

from app.domain import DirectoryHospital, HospitalInput


class HospitalDirectoryAdapter(Protocol):
    """Implementations return domain data or raise DirectoryOperationError."""

    async def create_hospital(
        self, hospital: HospitalInput, batch_id: UUID
    ) -> DirectoryHospital:
        """Request one creation while keeping the workflow independent of transport
        details.
        """
        ...

    async def get_hospitals_by_batch(self, batch_id: UUID) -> list[DirectoryHospital]:
        """Fetch evidence needed to reconcile creation and confirm activation."""
        ...

    async def activate_batch(self, batch_id: UUID) -> None:
        """Ask the Directory to activate a batch already judged ready by the processor."""
        ...
