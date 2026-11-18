# 01 - Project setup and configuration

Prerequisite: [working agreement](README.md). Next: [02](02-contracts-and-states.md).

## Outcome

A minimal FastAPI application runs locally, exposes health, and loads configuration. No hospital-processing features are implemented in this step.

## Files to create or update during implementation

`app/__init__.py`, `app/main.py`, `app/config.py`, dependency files, `.gitignore`, and `tests/api/test_health.py`.

## Tasks

- [x] **01.01** Select and record a supported Python interpreter meeting the assignment's Python 3.8+ requirement; use the same version locally and later in Docker.
- [x] **01.02** Create the `app/` and `tests/` package structure without generating later-step placeholder implementations.
- [x] **01.03** Declare minimal runtime dependencies: FastAPI, Uvicorn, HTTPX, multipart support, and an async SQLite driver such as aiosqlite. Add pytest and async-test support as development dependencies. Pin compatible versions when implementing.
- [x] **01.04** Add configuration for the upstream base URL, SQLite path, upload byte limit, shared concurrency, connection-pool sizes, HTTP timeouts, retry limit, and unfinished-batch admission limit.
- [x] **01.05** Use the planned defaults: concurrency 5; connect/read/write/pool timeouts 10/30/10/5 seconds; at most 3 eligible retry attempts. Choose and record practical byte/admission limits; these are service bounds, not new CSV business rules.
- [x] **01.06** Validate invalid configuration early, including nonpositive limits and inconsistent pool/concurrency settings.
- [x] **01.07** Add an application factory or equivalent testable application creation function. Provide lifecycle hooks for resources implemented in later steps.
- [x] **01.08** Add `GET /health` and keep its response minimal.
- [x] **01.09** Ignore virtual environments, caches, local environment files, generated benchmark output, and SQLite database/journal files. Preserve uploaded assignment sources.

## Verification

- [x] Start the application with one worker; confirm health and generated API documentation are reachable.
- [x] Add a health-route test and a configuration validation test. No database or live upstream calls should be required.
- [x] Record the startup command, interpreter version, and test result below.

## Configuration guide

Settings are implemented in `app/config.py` using the standard library. Defaults apply when environment variables are absent; explicit invalid values stop application creation. Settings are loaded once per application instance, so restart the server after changing them.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DIRECTORY_API_BASE_URL` | `https://hospital-directory.onrender.com` | Base address for the supplied Directory API |
| `SQLITE_PATH` | `data/hospitals.db` | Local database file; relative paths are relative to the startup working directory |
| `MAX_UPLOAD_BYTES` | `1048576` | Maximum uploaded CSV size in bytes: 1 MiB |
| `MAX_CONCURRENT_REQUESTS` | `5` | Shared maximum simultaneous upstream HTTP requests across batches |
| `HTTP_MAX_CONNECTIONS` | `5` | Maximum connections in the HTTP client's pool; must be at least concurrency |
| `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `5` | Maximum idle connections retained for reuse; must not exceed the pool maximum |
| `HTTP_CONNECT_TIMEOUT_SECONDS` | `10` | Maximum wait to establish an upstream connection |
| `HTTP_READ_TIMEOUT_SECONDS` | `30` | Maximum wait for a response-data chunk; not a total batch duration |
| `HTTP_WRITE_TIMEOUT_SECONDS` | `10` | Maximum wait while sending a request-data chunk |
| `HTTP_POOL_TIMEOUT_SECONDS` | `5` | Maximum wait to acquire an HTTP connection from the pool; not a background-task queue timeout |
| `MAX_REQUEST_ATTEMPTS` | `3` | Total eligible attempts, including the first; does not authorize unsafe POST retries |
| `MAX_IN_FLIGHT_BATCHES` | `20` | Admission limit for accepted unfinished batches; distinct from 20 hospitals per CSV |

These settings are defined and validated in step 01. SQLite initialization is connected through SQLAlchemy ORM in completed step 04. External HTTP, concurrency, and eligible retries are connected in completed step 05. Upload acceptance and batch admission are connected in later steps. The 20-hospital CSV rule is not an environment override.

The HTTPX Directory adapter consumes the upstream URL, HTTP pool/timeout limits, concurrency, and retry settings. One client and one semaphore are shared across batches. In addition to socket timeouts, each network attempt has an overall deadline equal to the sum of the four configured timeouts (55 seconds by default), excluding local semaphore waiting. Retry sleeps are bounded to 30 seconds; a longer `Retry-After` returns a classified error with the hint instead of retrying early. These policies add no configuration variables or public response fields.

Example override:

```bash
MAX_CONCURRENT_REQUESTS=3 HTTP_READ_TIMEOUT_SECONDS=45 \
  .venv/bin/python -m uvicorn app.main:create_app --factory --workers 1
```

Read process environment variables directly. `.env` files are not automatically loaded by this implementation; use shell exports or deployment environment configuration. An example environment file belongs to the later packaging step.

## Completion notes

Completed on 2026-11-18. Reused the user's existing `.venv` with Python 3.12.14 and recorded that version in `.python-version`.

Added the app factory, lifecycle boundary, minimal health endpoint, validated immutable settings, runtime/development dependency pins, ignore rules, and focused tests. Standard-library settings avoid adding a separate configuration dependency. Runtime dependencies and transitive dependencies are pinned in `requirements.txt`; `requirements-dev.txt` includes the complete runtime and test dependency set.

Dependency commands used:

```bash
uv pip compile --python .venv/bin/python --no-header --output-file requirements.txt - <<'EOF'
fastapi
uvicorn
httpx
python-multipart
aiosqlite
EOF

uv pip compile --python .venv/bin/python --no-header --output-file requirements-dev.txt - <<'EOF'
-r requirements.txt
pytest
pytest-asyncio
EOF

uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Run from the repository root:

```bash
.venv/bin/python -m uvicorn app.main:create_app --factory --workers 1
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

Verification results:

- Final test run: **26 passed**, no warnings, in 0.61 seconds.
- Dependency check: all 25 installed packages compatible.
- Started the real single-worker server on an automatically assigned local port using `--host 127.0.0.1 --port 0`; `/health`, `/docs`, and `/openapi.json` all returned `200`.
- Health response was exactly `{"status": "ok"}`. Stopped the temporary server cleanly after verification.
- Tests used the existing async HTTPX dependency; pytest's async fixture-loop scope is explicitly configured.
- No project database was created and no Hospital Directory API requests were sent. Steps 02-14 remain unstarted.
