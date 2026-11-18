"""Configuration defaults, override parsing, and startup-rejection behavior."""

from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app


def test_defaults_match_planned_service_limits() -> None:
    """Check that an empty environment yields the agreed service defaults, so setup and
    later runtime limits cannot drift unnoticed.
    """
    settings = Settings.from_env({})

    assert settings.directory_api_base_url == "https://hospital-directory.onrender.com"
    assert settings.sqlite_path == Path("data/hospitals.db")
    assert settings.max_upload_bytes == 1_048_576
    assert settings.max_concurrent_requests == 5
    assert settings.http_max_connections == 5
    assert settings.http_max_keepalive_connections == 5
    assert (
        settings.http_connect_timeout_seconds,
        settings.http_read_timeout_seconds,
        settings.http_write_timeout_seconds,
        settings.http_pool_timeout_seconds,
    ) == (10.0, 30.0, 10.0, 5.0)
    assert settings.max_request_attempts == 3
    assert settings.max_in_flight_batches == 20
    assert settings.http_attempt_timeout_seconds == 55


def test_combined_timeout_cannot_overflow_even_when_individual_values_are_finite() -> (
    None
):
    """Reject an infinite overall deadline before opening application resources."""
    with pytest.raises(ValueError, match="Combined HTTP timeout"):
        Settings(http_connect_timeout_seconds=1e308, http_read_timeout_seconds=1e308)


def test_environment_overrides_are_typed_and_unspecified_settings_keep_defaults(
    tmp_path: Path,
) -> None:
    """Check typed environment overrides and fallback behavior together, including a
    database path isolated from project data.
    """
    settings = Settings.from_env(
        {
            "DIRECTORY_API_BASE_URL": "https://example.test/",
            "SQLITE_PATH": str(tmp_path / "custom.db"),
            "MAX_UPLOAD_BYTES": "2048",
            "MAX_CONCURRENT_REQUESTS": "3",
            "HTTP_MAX_CONNECTIONS": "6",
            "HTTP_MAX_KEEPALIVE_CONNECTIONS": "4",
            "HTTP_READ_TIMEOUT_SECONDS": "45.5",
            "MAX_REQUEST_ATTEMPTS": "2",
            "MAX_IN_FLIGHT_BATCHES": "10",
        }
    )

    assert settings.directory_api_base_url == "https://example.test"
    assert settings.sqlite_path == tmp_path / "custom.db"
    assert settings.max_upload_bytes == 2048
    assert settings.max_concurrent_requests == 3
    assert settings.http_max_connections == 6
    assert settings.http_max_keepalive_connections == 4
    assert settings.http_read_timeout_seconds == 45.5
    assert settings.http_connect_timeout_seconds == 10.0
    assert settings.max_request_attempts == 2
    assert settings.max_in_flight_batches == 10


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_UPLOAD_BYTES", "0"),
        ("MAX_UPLOAD_BYTES", "1.5"),
        ("MAX_CONCURRENT_REQUESTS", "-1"),
        ("MAX_CONCURRENT_REQUESTS", "not-a-number"),
        ("MAX_REQUEST_ATTEMPTS", "0"),
        ("MAX_IN_FLIGHT_BATCHES", "0"),
        ("HTTP_CONNECT_TIMEOUT_SECONDS", "0"),
        ("HTTP_READ_TIMEOUT_SECONDS", "nan"),
        ("HTTP_WRITE_TIMEOUT_SECONDS", "inf"),
        ("HTTP_POOL_TIMEOUT_SECONDS", "-1"),
        ("HTTP_READ_TIMEOUT_SECONDS", "not-a-number"),
        ("DIRECTORY_API_BASE_URL", "ftp://example.test"),
        ("DIRECTORY_API_BASE_URL", "https://"),
        ("DIRECTORY_API_BASE_URL", "https://example.test:invalid"),
        ("DIRECTORY_API_BASE_URL", "https://example.test/path with spaces"),
        ("DIRECTORY_API_BASE_URL", "https://example.test?mode=test"),
        ("SQLITE_PATH", " "),
        ("SQLITE_PATH", "."),
    ],
)
def test_invalid_environment_settings_fail_early(name: str, value: str) -> None:
    """Reject an explicit invalid setting instead of silently replacing it with a
    default or starting an unsafe application.
    """
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"HTTP_MAX_CONNECTIONS": "4"}, "MAX_CONCURRENT_REQUESTS"),
        ({"HTTP_MAX_KEEPALIVE_CONNECTIONS": "6"}, "HTTP_MAX_KEEPALIVE_CONNECTIONS"),
    ],
)
def test_inconsistent_connection_limits_are_rejected(
    overrides: dict[str, str], message: str
) -> None:
    """Check relationships between concurrency, total pool size, and idle capacity
    rather than validating each number in isolation.
    """
    with pytest.raises(ValueError, match=message):
        Settings.from_env(overrides)


def test_application_loads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the application factory actually uses process configuration when no
    Settings object is injected.
    """
    monkeypatch.setenv("MAX_CONCURRENT_REQUESTS", "3")
    application = create_app()

    assert application.state.settings.max_concurrent_requests == 3


def test_invalid_process_environment_prevents_application_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure invalid deployment configuration fails before the application can accept
    hospital uploads.
    """
    monkeypatch.setenv("MAX_CONCURRENT_REQUESTS", "0")

    with pytest.raises(ValueError, match="MAX_CONCURRENT_REQUESTS"):
        create_app()
