# Development setup

## Supported runtime

- Python 3.11.x (the repository `.python-version` is authoritative)
- Node.js 20 LTS or newer
- Docker Desktop / Docker Engine with Compose v2 for the full local stack

Python 3.14 is intentionally not supported yet: the native ML dependency set is
not validated for it. Install the project dependencies in an isolated virtual
environment:

```text
python3.11 -m venv .venv
# Activate .venv using your shell's normal command.
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

On Windows, `py -3.11 -m venv .venv` is the equivalent. On macOS and Linux,
install FFmpeg through the platform package manager when RTSP support from the
OpenCV wheel is insufficient. Docker development avoids host FFmpeg variation.

## Configuration and startup

Copy `.env.example` to `.env`, replace development passwords, and start:

```text
docker compose up --build
```

Central is exposed at `http://localhost:8000`, its liveness endpoint is
`/health`, and readiness is `/ready`. The dashboard is exposed at
`http://localhost:3000`.

## Database migrations

The Central service uses Alembic for database migrations, supporting both PostgreSQL (+ PostGIS)
and SQLite:

```text
# Run migrations up to latest schema
python -m alembic upgrade head

# Roll back all migrations
python -m alembic downgrade base
```

Central automatically runs `alembic upgrade head` upon startup in `init_db()`.

## Runtime status notes

- **Docker Runtime**: When the host Docker daemon is stopped, container runtime validation is **NOT PROVEN**. Build manifests and Compose specifications are kept aligned to Python 3.11 and Milvus 2.5.4.
- **Physical Cameras**: Physical ONVIF camera discovery and live streams are verified via synthetic packet simulation and mock RTSP sources.

Physical Edge/Fog workloads are opt-in because they require a camera and model
assets:

```text
docker compose --profile pipeline up --build
```

For dashboard-only work:

```text
npm --prefix dashboard ci
npm --prefix dashboard run dev
```

The browser-upload `cctv-backend` is experimental and is intentionally not
started by default; it can be started with `--profile experimental-ai`.
