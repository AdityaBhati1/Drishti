# Central Backend Server

The Central server runs as a FastAPI REST API gateway, processing incoming alerts from the Fog nodes, saving events to PostgreSQL/PostGIS databases, performing TTL checking in Redis for deduplication and rate-limiting, and managing active WebSocket channels.

## Key Features
- **FastAPI HTTP Endpoint (`POST /api/alerts`)**: Processes incoming JSON events containing spatial-temporal locations, classification targets, and facial detection details.
- **FastAPI HTTP Endpoint (`GET /api/alerts`)**: Returns the latest 50 alert events.
- **WebSocket Manager (`/ws/alerts`)**: Broadcasters events to the web dashboard interface.
- **Postgres Database Handler**: Saves alert records. Implements native PostgreSQL triggers to automate `geom` column conversion for PostGIS spatial searches.
- **Redis TTL Rate Limiting**: Deduplicates alerts (filters out similar events within 10 seconds) and tracks incident frequencies using cache expiration timers.

- `DATABASE_URL`: SQL Connection URL (e.g. `postgresql://cctv_admin:${POSTGRES_PASSWORD}@localhost:5432/cctv_db` or SQLite in development).
- `CENTRAL_API_KEY`: API authentication key / token for operational endpoints and WebSockets.
- `REDIS_HOST`: Host address of Redis server (default: `localhost`).
- `REDIS_PORT`: Port of Redis server (default: `6379`).
