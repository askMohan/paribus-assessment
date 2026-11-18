"""Validated application settings, loaded from environment variables."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import get_type_hints
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable service limits and locations loaded at application construction."""

    # The supplied hospital system; our bulk API owns workflow, not this data.
    directory_api_base_url: str = "https://hospital-directory.onrender.com"
    # Relative to the startup directory; the lifecycle creates its parent folder.
    sqlite_path: Path = Path("data/hospitals.db")
    # Bound uploaded bytes as well as row count, since one field can be very large.
    max_upload_bytes: int = 1_048_576
    # One application-wide budget shared by creations, reads, and activation.
    max_concurrent_requests: int = 5
    # Pool capacity must cover the request budget to avoid needless pool waiting.
    http_max_connections: int = 5
    # Reuse idle connections instead of repeatedly setting up TCP/TLS sessions.
    http_max_keepalive_connections: int = 5
    # Connection establishment is separate from sending/receiving request data.
    http_connect_timeout_seconds: float = 10.0
    # Wait for a response-data chunk, not for the whole batch to finish.
    http_read_timeout_seconds: float = 30.0
    # Bound waits while transmitting a request-data chunk.
    http_write_timeout_seconds: float = 10.0
    # Bound waits inside HTTPX's pool; this is not a background-job queue timeout.
    http_pool_timeout_seconds: float = 5.0
    # Includes the first attempt and applies only when replay is established safe.
    max_request_attempts: int = 3
    # Admission count is batches currently queued/running, not rows per CSV.
    max_in_flight_batches: int = 20

    @property
    def http_attempt_timeout_seconds(self) -> float:
        """Bound one whole HTTP attempt as well as individual socket waits."""
        return sum(
            (
                self.http_connect_timeout_seconds,
                self.http_read_timeout_seconds,
                self.http_write_timeout_seconds,
                self.http_pool_timeout_seconds,
            )
        )

    def __post_init__(self) -> None:
        """Reject unusable settings before an application starts accepting uploads."""
        integer_fields = (
            "max_upload_bytes",
            "max_concurrent_requests",
            "http_max_connections",
            "http_max_keepalive_connections",
            "max_request_attempts",
            "max_in_flight_batches",
        )
        for field in integer_fields:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field.upper()} must be a positive integer")

        timeout_fields = (
            "http_connect_timeout_seconds",
            "http_read_timeout_seconds",
            "http_write_timeout_seconds",
            "http_pool_timeout_seconds",
        )
        for field in timeout_fields:
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field.upper()} must be a positive finite number")
        self._validate_http_limits()
        self._validate_locations()

    def _validate_http_limits(self) -> None:
        """Keep connection capacity and the overall attempt deadline consistent."""
        if not math.isfinite(self.http_attempt_timeout_seconds):
            raise ValueError("Combined HTTP timeout must be finite")

        # Reject contradictory budgets early instead of discovering them as pool
        # timeouts under load. Idle connections cannot exceed the total pool.
        if self.http_max_connections < self.max_concurrent_requests:
            raise ValueError(
                "HTTP_MAX_CONNECTIONS must be at least MAX_CONCURRENT_REQUESTS"
            )
        if self.http_max_keepalive_connections > self.http_max_connections:
            raise ValueError(
                "HTTP_MAX_KEEPALIVE_CONNECTIONS must not exceed HTTP_MAX_CONNECTIONS"
            )

    def _validate_locations(self) -> None:
        """Reject unusable upstream URLs and database file paths at startup."""
        url = self.directory_api_base_url
        try:
            parsed = urlsplit(url)
            # Accessing port also validates its syntax and range.
            parsed.port
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                "DIRECTORY_API_BASE_URL must be an HTTP(S) URL without a query or fragment"
            )

        if not self.sqlite_path.name or self.sqlite_path.name == "..":
            raise ValueError("SQLITE_PATH must identify a database file")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Parse uppercase environment overrides; omitted fields keep their defaults.

        Pass an isolated mapping in tests. Normal startup reads os.environ;
        .env files are not loaded automatically.
        """
        source = os.environ if environ is None else environ
        overrides = {}
        for name, value_type in get_type_hints(cls).items():
            key = name.upper()
            if key not in source:
                continue
            raw = source[key].strip()
            if name == "directory_api_base_url":
                raw = raw.rstrip("/")
            if name == "sqlite_path" and not raw:
                raise ValueError("SQLITE_PATH must identify a database file")
            try:
                overrides[name] = value_type(raw)
            except ValueError:
                raise ValueError(f"Invalid {key}") from None
        return cls(**overrides)
