"""Access application-owned services and reuse the batch response contract."""

from uuid import UUID

from fastapi import Request, Response

from app.api.schemas import BatchAccepted
from app.config import Settings
from app.db.repository import BatchRepository
from app.services.processor import BatchProcessor


def get_repository(request: Request) -> BatchRepository:
    """Reuse the application repository without opening a request-owned session."""
    return request.app.state.repository


def get_processor(request: Request) -> BatchProcessor:
    """Keep background processing bound to application-lifetime resources."""
    return request.app.state.processor


def get_settings(request: Request) -> Settings:
    """Use the settings validated when this application was constructed."""
    return request.app.state.settings


def accept_batch(batch_id: UUID, response: Response) -> BatchAccepted:
    """Put the polling path in Location and keep the body to one UUID field."""
    response.headers["Location"] = f"/hospitals/batch/{batch_id}/status"
    return BatchAccepted(batch_id=batch_id)
