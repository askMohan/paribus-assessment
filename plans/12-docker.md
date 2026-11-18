# 12 - Docker and Compose

Prerequisite: [11](11-integration-and-performance.md). Next: [13](13-documentation-and-examples.md).

## Outcome

The application runs in one container with one worker and a persistent SQLite volume.

## Files to create or update during implementation

`Dockerfile`, `docker-compose.yml`, `.dockerignore`, and `.env.example`.

## Tasks

- [x] **12.01** Use the interpreter version selected in step 01 and install only runtime dependencies in the application image.
- [x] **12.02** Copy the needed application/configuration files; exclude local databases, secrets, virtual environments, caches, and generated outputs from the image context.
- [x] **12.03** Start Uvicorn bound to the container interface with exactly one worker and support the deployment provider's configured port.
- [x] **12.04** Put SQLite at a configurable writable data path outside the application source directory.
- [x] **12.05** Define one application service in Compose and mount a named volume for the SQLite data directory. Do not add Redis or a separate database/worker service.
- [x] **12.06** Add example environment values matching actual configuration names, limits, and timeouts, without credentials or fake required settings.
- [x] **12.07** Use the health route for a container health check if the image has a suitable lightweight mechanism.
- [x] **12.08** Record build/start/stop commands and distinguish stopping a container from explicitly removing its data volume.

## Verification

- [x] Build and start the container; confirm `/health` and generated API documentation are reachable.
- [x] Validate a CSV through the container without creating live upstream hospitals.
- [x] Verify persisted registration/state survives container recreation while the same named volume remains; use a local simulated upstream if processing is needed.
- [x] Confirm the startup command has one worker, the data directory is writable, and no queue service was introduced.

## Completion notes

Completed with four small files: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, and `.env.example`. No application code or dependencies changed.

- The image uses Python 3.12.14 slim and installs only `requirements.txt`. It copies only application source and runs as the non-root `app` user. `/data` is writable by that user; SQLite defaults to `/data/hospitals.db`.
- Uvicorn binds `0.0.0.0`, explicitly uses one worker, and reads `PORT` (default 8000). The shell uses `exec` so Uvicorn receives container shutdown signals. The health check uses Python's standard library to call `/health` on the configured port; no curl/system packages are installed.
- Compose has one `api` service and one named `hospital_data` volume mounted at `/data`. It loads `.env`, propagates the configured port, and publishes that same port. A new ignored `.env` was copied from the example because none existed; existing configuration is never overwritten by implementation.
- The build context is allowlisted to runtime requirements and application source, with caches, bytecode, environment files, and SQLite files explicitly excluded. Tests, benchmark files, PDFs, contracts, local databases, and development requirements are not copied into the image.

### Usage

Run from the project root. On first setup only, copy `.env.example` to `.env` if `.env` does not exist. The application itself does not read dotenv files; Compose supplies those values as environment variables.

```bash
cp .env.example .env  # First setup only; keep an existing .env.
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Health and docs are available at `http://localhost:8000/health` and `http://localhost:8000/docs` with the defaults. Change `PORT` in `.env` to use another port. Keep `SQLITE_PATH` under `/data` to use the mounted persistent directory.

Validate without creating upstream hospitals:

```bash
printf 'name,address\nSample Hospital,Sample Address\n' | \
  curl -i -F 'file=@-;filename=hospitals.csv' \
  http://localhost:8000/hospitals/bulk/validate
```

Stop while retaining SQLite data:

```bash
docker compose down
```

To explicitly delete the Compose data volume and its SQLite data, use `docker compose down --volumes`. Recreating the container with `docker compose up -d --force-recreate` retains the existing named volume.

### Verification

- `docker compose -p paribus-plan12-verify config --quiet` passed, and `docker compose -p paribus-plan12-verify build` built successfully using the pinned interpreter and runtime packages.
- Started with a nondefault `PORT=54081` and `up -d --wait`; the image health check reported healthy. `/health`, `/docs`, and `/openapi.json` returned `200`; valid CSV validation returned empty `204`.
- Runtime inspection confirmed Python 3.12.14, non-root UID 1000, writable `/data`, and PID 1 running Uvicorn with `--workers 1`. Pytest/development requirements, `.env`, tests, and contracts are absent from the runtime image. A fresh image contains no local SQLite database.
- Seeded a batch only in the isolated verification volume through the existing ORM repository: one created ID, one unconfirmed creation, one pending row, and two seconds of checkpointed duration. Recreated the container and confirmed the same UUID, inputs, ID 101, and saved duration survived. Startup marked the batch `INTERRUPTED`, converted only the unconfirmed row to `UNKNOWN`, and left the other rows `CREATED`/`PENDING` without automatic replay.
- No live Directory requests were made. The isolated verification container/network/volume were removed afterward; the existing project SQLite file was not used.
- Packaging was verified through real builds and container checks; no duplicate Dockerfile-structure unit tests were added. The application source and the existing 277-test baseline are unchanged.

Step 13 remains unstarted.
