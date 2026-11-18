"""Application lifespan, local health/docs, and instance isolation checks."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_health_and_api_documentation_with_initialized_database(
    tmp_path: Path,
) -> None:
    """Exercise lifespan startup, local HTTP health/docs, and database shutdown
    together without contacting the Directory.
    """
    database_path = tmp_path / "data" / "hospitals.db"
    application = create_app(Settings(sqlite_path=database_path))
    assert not database_path.exists()

    async with application.router.lifespan_context(application):
        assert database_path.exists()
        repository = application.state.repository
        assert await repository.count_in_flight_batches() == 0
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

            assert (await client.get("/docs")).status_code == 200
            schema = (await client.get("/openapi.json")).json()
            assert "/health" in schema["paths"]

    assert database_path.exists()
    with pytest.raises(RuntimeError, match="not open"):
        await repository.count_in_flight_batches()


def test_application_instances_keep_separate_settings(tmp_path: Path) -> None:
    """Protect application-factory isolation so one test or application instance cannot
    replace another instance's database location.
    """
    first = create_app(Settings(sqlite_path=tmp_path / "first.db"))
    second = create_app(Settings(sqlite_path=tmp_path / "second.db"))

    assert first.state.settings.sqlite_path == tmp_path / "first.db"
    assert second.state.settings.sqlite_path == tmp_path / "second.db"
