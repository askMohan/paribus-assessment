"""CSV acceptance, validation, and saved-result polling."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Response,
    UploadFile,
)

from app.api.dependencies import (
    accept_batch,
    get_processor,
    get_repository,
    get_settings,
)
from app.api.schemas import BatchAccepted, BatchResult
from app.config import Settings
from app.db.repository import BatchRepository
from app.domain import HospitalRow
from app.services.csv_validation import (
    CSVValidationError,
    UploadTooLargeError,
    validate_csv_upload,
)
from app.services.processor import BatchProcessor

router = APIRouter()

Repository = Annotated[BatchRepository, Depends(get_repository)]
Processor = Annotated[BatchProcessor, Depends(get_processor)]
Configuration = Annotated[Settings, Depends(get_settings)]


@router.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Report local application availability without calling the Directory."""
    return {"status": "ok"}


async def _validated_rows(file: UploadFile, settings: Settings) -> list[HospitalRow]:
    """Translate shared CSV problems consistently for both upload endpoints."""
    try:
        return await validate_csv_upload(
            file, max_upload_bytes=settings.max_upload_bytes
        )
    except UploadTooLargeError as error:
        raise HTTPException(413, detail=str(error)) from None
    except CSVValidationError as error:
        raise HTTPException(
            422, detail=[asdict(issue) for issue in error.issues]
        ) from None


@router.post(
    "/hospitals/bulk", status_code=202, response_model=BatchAccepted, tags=["Hospitals"]
)
async def upload_hospitals(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    response: Response,
    repository: Repository,
    processor: Processor,
    settings: Configuration,
) -> BatchAccepted:
    """Validate and save the whole manifest before scheduling background execution."""
    rows = await _validated_rows(file, settings)
    batch_id = uuid4()
    await repository.register_batch(
        batch_id, rows, max_in_flight_batches=settings.max_in_flight_batches
    )
    background_tasks.add_task(processor.process_batch, batch_id)
    return accept_batch(batch_id, response)


@router.post("/hospitals/bulk/validate", status_code=204, tags=["Hospitals"])
async def validate_hospitals(file: UploadFile, settings: Configuration) -> Response:
    """Validate the same CSV contract without persistence or external operations."""
    await _validated_rows(file, settings)
    return Response(status_code=204)


@router.get(
    "/hospitals/batch/{batch_id}/status", response_model=BatchResult, tags=["Hospitals"]
)
async def batch_status(batch_id: UUID, repository: Repository) -> BatchResult:
    """Project saved evidence into exactly the seven documented result fields."""
    batch = await repository.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, detail="Batch not found")
    elapsed = batch.processing_time_seconds
    if batch.attempt_started_at is not None:
        elapsed += max(
            0.0, (datetime.now(UTC) - batch.attempt_started_at).total_seconds()
        )
    return BatchResult.from_rows(
        batch_id=batch.batch_id,
        total_hospitals=batch.total_hospitals,
        rows=batch.rows,
        processing_time_seconds=elapsed,
        batch_activated=batch.batch_activated,
    )


@router.post(
    "/hospitals/batch/{batch_id}/resume",
    status_code=202,
    response_model=BatchAccepted,
    tags=["Hospitals"],
)
async def resume_batch(
    batch_id: UUID,
    background_tasks: BackgroundTasks,
    response: Response,
    repository: Repository,
    processor: Processor,
    settings: Configuration,
) -> BatchAccepted:
    """Claim eligible saved work once, retaining its UUID and confirmed IDs."""
    batch = await repository.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "Batch not found")
    if not batch.can_resume:
        raise HTTPException(409, "Batch has no eligible recovery work")
    if not await repository.claim_for_resume(
        batch_id, max_in_flight_batches=settings.max_in_flight_batches
    ):
        raise HTTPException(409, "Batch is already claimed or cannot be resumed")
    background_tasks.add_task(
        processor.process_batch,
        batch_id,
        reconcile_before_processing=batch.needs_reconciliation,
    )
    return accept_batch(batch_id, response)
