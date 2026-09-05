# Canonical architecture

The supported production path is:

```text
RTSP/Webcam → Edge (detect + track) → MQTT EdgeEvent v1
           → Fog (rules/recognition) → MQTT AlertEvent v1
           → Central (persist + WebSocket) → Dashboard
```

`shared/events.py` owns the versioned contracts and `shared/config.py` owns
environment-backed shared settings. Camera/site configuration is server-owned;
backend services must not read frontend source files.

## Database Architecture Decision: PostgreSQL & SQLite Hybrid Model

The platform adopts a hybrid database strategy:
- **Central / Multi-Camera Hub**: PostgreSQL (+ PostGIS) is the canonical database for production Central clusters. It provides high-throughput concurrent write handling (safe against replayed events and concurrent worker threads), spatial indexing (`GIST` on `geom`), and multi-camera aggregation across observation posts.
- **Standalone Edge / Border Observation Post (BOP)**: SQLite is supported for standalone or resource-constrained edge deployments where external database servers are infeasible. In SQLite mode, spatial columns and PostGIS triggers are safely bypassed while maintaining identical schema structures and SQL queries.
- `central/database.py` dynamically identifies the engine dialect (`postgresql` vs. `sqlite`) and initializes tables accordingly.

## Event Pipeline & Boundary Ingestion

- **Edge Node (`edge/main.py`)**: Runs object detection and tracking on RTSP/camera feeds, emitting versioned `EdgeEvent` payloads over MQTT (`surveillance/edge/events.v1`).
- **Fog Node (`fog/main.py`)**: Evaluates temporal rules (e.g. loitering/dwell time) on active tracks. If a track remains continuous past `loitering_seconds`, an `AlertEvent` is emitted. Tracks unseen beyond `track_expiry_seconds` are marked as departed. Repeated alerts on persisting tracks respect a configurable cooldown period.
- **Central Node (`central/main.py`)**: Ingests `AlertEvent` payloads via MQTT or HTTP `POST /api/alerts`. Ingestion is replay-safe and idempotent based on `event_id`. Persisted alerts are broadcast thread-safely to active WebSocket connections (`/ws/alerts`).
- **Dashboard (`dashboard/src/App.jsx`)**: Connects to the WebSocket feed, updates live alerts without polling, visualizes alert pins on the tactical map, and allows operators to acknowledge alerts.

## Quarantined experiments
 
- `cctv-backend/` is a standalone browser-upload AI experiment. It is not part
  of the supported production topology.
- `scratch/` contains exploratory scripts, not the test suite.
 
Canonical facial recognition (FRS) and vector search are fully integrated into
`fog/analytics.py` (`VectorGallery` and `FacialRecognitionModule`) and run inside the Fog pipeline.

The dashboard defaults to the Central/Fog cluster view. The old browser upload
scanner is compiled out unless `VITE_ENABLE_EXPERIMENTAL_BROWSER_AI=true` is
set deliberately.
