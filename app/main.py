"""FastAPI application factory and lifecycle."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.httpx_hospital_directory import (
    HttpxHospitalDirectoryAdapter,
    create_directory_client,
)
from app.api.routes import router
from app.config import Settings
from app.db.database import Database
from app.db.repository import BatchCapacityError, BatchRepository
from app.services.processor import BatchProcessor

logger = logging.getLogger(__name__)


async def storage_error(request: Request, error: SQLAlchemyError) -> JSONResponse:
    """Keep storage failures private and handle them consistently across routes."""
    logger.error(
        "Storage request failed path=%s",
        request.url.path,
        exc_info=(type(error), error, error.__traceback__),
    )
    return JSONResponse(
        status_code=500, content={"detail": "Unable to access batch storage"}
    )


async def capacity_error(request: Request, error: BatchCapacityError) -> JSONResponse:
    """Reject excess work before any background job is accepted."""
    return JSONResponse(
        status_code=503, content={"detail": "Batch processing capacity is full"}
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Own the ORM resources and shared Directory client for the running application."""
    settings = application.state.settings
    async with (
        Database(settings.sqlite_path) as database,
        create_directory_client(settings) as client,
    ):
        application.state.repository = BatchRepository(database)
        recovered = await application.state.repository.recover_interrupted_batches()
        if recovered:
            logger.warning("Interrupted batches recovered count=%s", recovered)
        application.state.directory_adapter = HttpxHospitalDirectoryAdapter(
            client, settings, asyncio.Semaphore(settings.max_concurrent_requests)
        )
        application.state.processor = BatchProcessor(
            application.state.repository, application.state.directory_adapter
        )
        logger.info("Hospital bulk processing application started")
        try:
            yield
        finally:
            logger.info("Hospital bulk processing application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct an isolated FastAPI instance with configuration and lifecycle wiring."""
    settings = settings if settings is not None else Settings.from_env()
    application = FastAPI(
        title="Hospital Bulk Processing API",
        version="0.1.0",
        lifespan=lifespan,
        exception_handlers={
            SQLAlchemyError: storage_error,
            BatchCapacityError: capacity_error,
        },
    )
    application.state.settings = settings

    application.include_router(router)
    return application
