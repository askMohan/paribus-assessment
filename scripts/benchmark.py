"""Run: .venv/bin/python -m scripts.benchmark --latency-ms 50"""

import argparse
import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter

import httpx

from app.config import Settings
from tests.simulated_directory import (
    SimulatedDirectory,
    directory_application,
    hospital_csv,
)


async def measure(
    concurrency: int, latency_seconds: float, database_path: Path
) -> dict:
    """Measure one full 20-record run with the same simulated request latency."""
    directory = SimulatedDirectory(expected_count=20, latency_seconds=latency_seconds)
    settings = Settings(sqlite_path=database_path, max_concurrent_requests=concurrency)
    with directory_application(directory, settings) as app:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://benchmark"
            ) as client,
        ):
            started = perf_counter()
            accepted = await client.post(
                "/hospitals/bulk", files={"file": ("benchmark.csv", hospital_csv(20))}
            )
            # ASGITransport waits for BackgroundTasks, so this spans completion,
            # not merely the time until the server sends its 202 response.
            elapsed = perf_counter() - started
            assert accepted.status_code == 202
            result = (await client.get(accepted.headers["Location"])).json()
            counts = Counter(method for method, _, _ in directory.requests)
            assert result["batch_activated"] and result["processed_hospitals"] == 20
            assert (
                len(directory.records) == counts["POST"] == 20 and counts["PATCH"] == 1
            )
            assert len({row["hospital_id"] for row in result["hospitals"]}) == 20
            assert directory.peak_active <= concurrency
            return {
                "concurrency": concurrency,
                "hospitals": 20,
                "latency_ms_per_request": latency_seconds * 1000,
                "elapsed_seconds": round(elapsed, 4),
                "max_active_requests": directory.peak_active,
                "request_counts": {
                    method: counts[method] for method in ("POST", "GET", "PATCH")
                },
            }


async def run(latency_ms: float) -> list[dict]:
    """Isolate every concurrency run in a fresh temporary SQLite file."""
    results = []
    with tempfile.TemporaryDirectory(prefix="hospital-benchmark-") as location:
        for concurrency in (1, 3, 5):
            result = await measure(
                concurrency, latency_ms / 1000, Path(location) / f"c{concurrency}.db"
            )
            results.append(result)
    for result in results:
        result["speedup_vs_concurrency_1"] = round(
            results[0]["elapsed_seconds"] / result["elapsed_seconds"], 2
        )
    return results


def main() -> None:
    """Print bounded simulation results; do not contact the deployed Directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latency-ms", type=float, default=50)
    args = parser.parse_args()
    if not 0 <= args.latency_ms <= 1000:
        parser.error("--latency-ms must be between 0 and 1000")
    print(json.dumps(asyncio.run(run(args.latency_ms)), indent=2))


if __name__ == "__main__":
    main()
