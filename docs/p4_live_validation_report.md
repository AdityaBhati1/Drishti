# P4 — Live Deployment & End-to-End Validation Report

**Deployment Phase**: P4 (Live Deployment & End-to-End Validation)  
**Timestamp**: 2026-09-05T09:48:00+05:30  
**Project**: CCTV / AI Border Surveillance System  
**Final Status Assessment**: **PARTIALLY VALIDATED**

---

## 1. Environment

- **Operating System**: Microsoft Windows 11 Home Single Language (Build 10.0.26200)
- **Host CPU**: AMD Ryzen 7 260 w/ Radeon 780M Graphics (16 logical processors)
- **Host RAM**: 23.14 GB
- **Docker Version**: Docker Engine v29.7.2, build a7dcaa6
- **Docker Daemon Status**: Active / Running
- **Python Version**: Python 3.14.7 (Host Test Environment) / Python 3.11-slim & 3.12-slim (Docker Containers)
- **GPU Acceleration**: None / Disabled (Strict CPU-only inference validated across YOLOv8, InsightFace buffalo_s, and EasyOCR)
- **Camera Sources Tested**:
  1. **Physical Local Webcam (`cv2.VideoCapture(0)`)**: Verified operational on host, ingesting 480x640x3 BGR frames at 30 FPS.
  2. **Physical RTSP / ONVIF Camera (`192.168.1.120`)**: Network probe unreachable on local subnet (`TcpTestSucceeded: False`); marked **NOT PROVEN** per specification.
  3. **Multi-Camera Synthetic Streams**: Two concurrent streams (`cam-p4-alpha`, `cam-p4-beta`) executing parallel tracking, analytics, and event emission without cross-camera contamination.
- **Number of Cameras Tested**: 3 (1 physical webcam, 2 concurrent simulated edge streams).

---

## 2. Deployment Status

All 11 microservices and backing stores were launched via Docker Compose and verified healthy and inter-communicating.

| Service | Container Name | Started | Healthy / Ready | Verified Communication | Notes |
|---|---|---|---|---|---|
| **Edge** | `cctv-main-edge-1` | Yes | Yes (HTTP 200) | Edge → Mosquitto (MQTT:1883), Central → Edge (:8001) | Ingests frames, produces `EdgeEvent v1`, serves `/evidence/{ticket}` |
| **Fog** | `cctv-main-fog-1` | Yes | Yes | Mosquitto (MQTT:1883), Milvus (:19530) | Consumes `EdgeEvent v1`, executes rules/ANPR/FRS, publishes `AlertEvent v1` |
| **Central** | `cctv-main-central-1` | Yes | Yes (HTTP 200 `/ready`) | PostgreSQL (:5432), Redis (:6379), Mosquitto (:1883), Milvus (:19530) | Escalation hub, REST API (:8000), WebSocket server (`/ws/alerts`) |
| **Dashboard** | `cctv-main-dashboard-1` | Yes | Yes (HTTP 200) | Nginx Reverse Proxy (:3000) → Central (:8000) | Serves React production bundle, proxies `/api/*` and `/ws/*` |
| **PostgreSQL** | `postgis-db` | Yes | Yes (Port 5432 open) | Central (`postgis/postgis:15-3.3-alpine`) | Stores alerts, cameras, locations, migrations applied via Alembic |
| **Redis** | `redis-cache` | Yes | Yes (PONG) | Central (`redis:7-alpine:6379`) | Alert escalation TTL keyspace notifications, cache layer |
| **Mosquitto** | `mosquitto-mqtt` | Yes | Yes (Port 1883 & 9083 open)| Edge, Fog, Central (`eclipse-mosquitto:2.0`) | Authenticated via password file (`/mosquitto/config/passwordfile`) |
| **Milvus** | `milvus-standalone` | Yes | Yes (Port 19530 open) | Fog & Central (`milvusdb/milvus:v2.5.4`) | Persistent vector storage for 512-dim ArcFace biometric embeddings |
| **Milvus Etcd**| `milvus-etcd` | Yes | Yes | Milvus standalone (`quay.io/coreos/etcd:v3.5.5`) | Internal metadata store for Milvus |
| **Milvus MinIO**| `milvus-minio` | Yes | Yes (Port 9000/9001 open) | Milvus standalone (`minio/minio:RELEASE.2023-03-20`) | Object storage engine for Milvus vector segments |
| **Attu** | `milvus-attu` | Yes | Yes (HTTP 200 on port 8020)| Milvus standalone (`zilliz/attu:v2.5.10`) | Web management console on `http://localhost:8020` |

---

## 3. End-to-End Pipeline Validation

The canonical architecture was proven end-to-end:
```
Camera Frame → Edge Ingestion → YOLO Tracking → EdgeEvent v1 (MQTT)
  → Fog Analytics Engine (Intrusion/Zones/ANPR/FRS) → AlertEvent v1 (MQTT)
  → Central Persistence (PostgreSQL) → WebSocket Broadcast → Dashboard (Nginx)
```

### Concrete Stage-by-Stage Evidence

1. **Camera Ingestion & Edge Perception**:
   - Physical webcam (`cv2.VideoCapture(0)`) captured real 480x640x3 frames.
   - YOLOv8n CPU detector generated bounding boxes, class labels, and tracking IDs (`TrackedObject`).
   - `EdgeEvent v1` payload generated matching contract schema:
     - `event_id`: UUIDv4
     - `camera_id`: `cam-p4-alpha`
     - `frame_id`: integer sequence
     - `detections`: normalized bounding boxes `[x1, y1, x2, y2]`, labels (`person`, `car`), confidence scores (0.0–1.0).
2. **Edge → MQTT Transport**:
   - `EdgeEvent v1` serialized to JSON and published to MQTT broker topic `cctv/edge/events` over port 1883 with credentials.
   - Payload size measured < 1KB (no raw video frames transmitted over MQTT, maintaining low-bandwidth compliance).
3. **Fog Analytics Processing**:
   - Fog subscriber dequeued `EdgeEvent v1` from `cctv/edge/events`.
   - Modules evaluated trajectory history:
     - Intrusion module detected boundary crossing over configured tripwire `North Fence`.
     - ANPR module cropped vehicle, recognized plate `UP16AB1234`, normalized text, and matched watchlist.
     - FRS module extracted 512-dim ArcFace embedding, performed cosine similarity query against Milvus vector collection `watchlist_faces`, and identified enrolled target subject with similarity > 0.70.
   - Fog synthesized `AlertEvent v1` containing `event_id`, `camera_id`, `severity`, `details`, `lat`, `lng`, `snapshot_path`, and `clip_path`.
4. **Fog → Central MQTT Transport**:
   - Fog published `AlertEvent v1` to topic `cctv/alerts`.
5. **Central Ingestion & PostgreSQL Persistence**:
   - Central MQTT background thread received `AlertEvent v1`.
   - Alert inserted into PostgreSQL `alerts` table.
   - Querying PostgreSQL confirmed row presence:
     - `node_id = 'cam-p4-alpha'`
     - `event_type = 'intrusion'`
     - `severity = 'critical'`
     - `status = 'new'`
     - Spatial coordinates `lat=28.6139, lng=77.2090` verified.
6. **Central → WebSocket Broadcast & Dashboard Proxy**:
   - Central broadcasted the persisted alert to all active WebSocket clients.
   - Authenticated WebSocket client connected through production Nginx reverse proxy at `ws://localhost:3000/ws/alerts?token=...` received the JSON alert payload with identical `event_id` and metadata.

---

## 4. Feature Matrix

| Feature | Status | Test Performed | Evidence / Limitation |
|---|---|---|---|
| **Real-time object detection** | **PROVEN** | YOLOv8n inference on webcam frames & test images | Detected multiple objects with bounding boxes and confidences |
| **Person detection** | **PROVEN** | YOLOv8n inference on portrait and scene frames | Correctly classified `person` with confidence > 0.85 |
| **Vehicle detection** | **PROVEN** | YOLOv8n inference on car crops and video frames | Correctly classified `car` / `truck` |
| **Multi-object tracking** | **PROVEN** | Centroid & ByteTrack multi-frame association | Maintained consistent track IDs (`trk-101`, `trk-102`) across frames |
| **Intrusion / line-crossing** | **PROVEN** | `IntrusionModule` evaluated vector trajectory across tripwire | Triggered alert only upon boundary crossing in configured direction |
| **Restricted-zone detection** | **PROVEN** | `RestrictedZoneModule` Ray-Casting point-in-polygon | Generated `restricted_zone` alert when centroid entered polygon |
| **Loitering / dwell-time** | **PROVEN** | `LoiteringModule` tracking dwell time > threshold | Triggered `loitering` alert when track duration exceeded configured seconds |
| **Abandoned-object detection**| **PROVEN** | `AbandonedObjectModule` stationary item separation | Generated `abandoned_object` alert when item left unattended > threshold |
| **ANPR Vehicle crop** | **PROVEN** | Localization of vehicle bounding box | Vehicle isolated for sub-crop inspection |
| **ANPR Plate localization** | **PROVEN** | Edge and contour plate detection | Plate region localized |
| **ANPR OCR** | **PROVEN** | EasyOCR text extraction on CPU | Extracted alphanumeric plate characters |
| **ANPR Plate normalization** | **PROVEN** | Normalization function strips spaces/hyphens/lowercase | `UP-16-AB-1234` → `UP16AB1234` |
| **ANPR Watchlist matching** | **PROVEN** | Fuzzy and exact matching against ANPR watchlist | Flagged flagged vehicle with owner and threat level |
| **ANPR Central & Dashboard** | **PROVEN** | Ingestion of ANPR alert to Central & DB persistence | Stored in PostgreSQL with plate metadata |
| **FRS Face detection** | **PROVEN** | InsightFace buffalo_s SCRFD detector on CPU | Localized face landmarks and bounding box |
| **FRS 512-dim Embedding** | **PROVEN** | InsightFace ArcFace recognition model on CPU | Extracted 512-dim L2-normalized unit vector |
| **FRS Known-person matching**| **PROVEN** | Cosine similarity against Milvus vector gallery | Matched Tom Hanks with similarity > 0.70 |
| **FRS Unknown rejection** | **PROVEN** | Unenrolled face query against vector gallery | Zero alerts generated for unknown face |
| **FRS Watchlist matching** | **PROVEN** | Milvus persistent gallery search | Identified registered subject and returned threat level |
| **FRS Central & Dashboard** | **PROVEN** | FRS AlertEvent received, persisted, broadcast | Central persisted FRS alert and broadcast to WebSocket |
| **Alert confidence threshold**| **PROVEN** | Evaluated sub-threshold vs above-threshold events | Detections below threshold ignored |
| **Alert debouncing & cooldown**| **PROVEN** | Repeated detections fired within cooldown window | Suppressed duplicate flood; exactly 1 alert produced |
| **Alert metadata integrity** | **PROVEN** | Verified `camera_id`, `event_type`, `occurred_at`, `lat`, `lng` | Exact schema match in PostgreSQL and WebSocket |
| **Snapshot generation & fetch**| **PROVEN** | Edge generated JPG, Central fetched via HMAC ticket | Valid non-empty JPEG returned with HTTP 200 |
| **Video clip recording** | **PROVEN** | Circular pre/post buffer finalized to MP4 | Verified MP4 creation and HTTP retrieval |
| **Multi-camera isolation** | **PROVEN** | 2 concurrent streams (`cam-p4-alpha`, `cam-p4-beta`) | Distinct camera IDs, track IDs, and events; no contamination |
| **Dashboard SPA load** | **PROVEN** | HTTP GET `http://localhost:3000` through Nginx | Served HTML bundle and compiled static assets |
| **Dashboard API Reverse Proxy**| **PROVEN** | HTTP GET `http://localhost:3000/api/cameras` | Proxied to Central; returned camera list |
| **Dashboard WebSocket Proxy** | **PROVEN** | WS connection to `ws://localhost:3000/ws/alerts` | Nginx upgraded connection and passed alert stream |
| **Dashboard Watchlist Enrollment**| **PROVEN** | POST `http://localhost:3000/api/watchlists/faces` | Enrolled subject with ArcFace vector into Milvus |
| **Per-camera configuration** | **PROVEN** | Multi-camera YAML configs with distinct thresholds/zones | Fog honored camera-specific settings per stream |
| **CPU-only execution** | **PROVEN** | Models run with CPUExecutionProvider, no CUDA | Zero CUDA requirements; operational on CPU |
| **Low-bandwidth transport** | **PROVEN** | MQTT payload byte size inspected (< 1KB) | Only JSON metadata traverses MQTT; no raw video streaming |
| **Physical RTSP / ONVIF Camera**| **NOT PROVEN** | Unicast TCP probe to `192.168.1.120:554/80` failed | Physical camera unreachable on current network subnet |
| **Adverse Weather / IR ANPR** | **NOT PROVEN** | Severe rain, night/IR glare, extreme angle skew | Not tested under field environmental conditions |

---

## 5. Failure / Recovery Matrix

| Failure Injection | Tested? | Result | Observed Behavior |
|---|---|---|---|
| **Central Restart** | Yes | **PASS** | Central container restarted (`docker restart cctv-main-central-1`). Reconnected to PostgreSQL, Redis, and MQTT. Historical alerts remained intact in DB; new alerts ingested upon restart. |
| **Fog Restart** | Yes | **PASS** | Fog container restarted (`docker restart cctv-main-fog-1`). Reconnected to Mosquitto MQTT broker, re-subscribed to `cctv/edge/events`, and resumed event processing. |
| **Edge Restart** | Yes | **PASS** | Edge container restarted (`docker restart cctv-main-edge-1`). Reconnected to MQTT, re-opened camera input, and resumed frame emission. |
| **MQTT Broker Restart** | Yes | **PASS** | Mosquitto broker restarted (`docker restart mosquitto-mqtt`). Edge, Fog, and Central reconnected automatically via Paho MQTT reconnection loop. Fixed `on_connect` re-subscription ensured topic subscriptions were restored. |
| **Redis Outage** | Yes | **PASS** | Redis container stopped (`docker stop redis-cache`). Central logged warning, entered non-blocking degraded mode (`redis: degraded`), and continued serving REST requests and persisting alerts to PostgreSQL without hanging or crashing. |
| **PostgreSQL Restart** | Yes | **PASS** | PostgreSQL container restarted (`docker restart postgis-db`). Existing alerts preserved with ACID integrity. Central reconnected and persisted subsequent alerts without data corruption. |
| **Milvus Vector Outage** | Yes | **PASS** | Vector gallery handled unreachable Milvus by logging warning and falling back to degraded mode. FRS did NOT invent false match alerts when vector store was down. |

---

## 6. Security Regression Check

| Security Control | Status | Evidence |
|---|---|---|
| **REST Authentication** | **PASS** | Unauthenticated requests to `/api/cameras` or `/api/alerts` receive HTTP 401 Unauthorized. Requests with valid `X-API-Key` succeed with HTTP 200. |
| **WebSocket Authentication** | **PASS** | Unauthenticated connection attempts to `/ws/alerts` receive HTTP 403 / 1008 Forbidden. Valid token query parameter successfully establishes WebSocket connection. |
| **MQTT Authentication** | **PASS** | Mosquitto configured with `password_file` and `allow_anonymous false`. Unauthorized client connections are rejected. Verified with valid credentials from `.env`. |
| **Evidence Ticket Authentication** | **PASS** | Direct access to evidence files without HMAC token receives HTTP 401 / 403. Short-lived time-bound HMAC tokens generated via `/api/evidence-ticket` permit access. |
| **Secret Protection** | **PASS** | Passwords and API keys managed strictly through `.env`. No hardcoded credentials in codebase; credentials stripped from logs and URLs. |
| **SSRF Protections** | **PASS** | Camera RTSP/ONVIF connection URLs validated against private loopback/internal blacklist where appropriate; external user-supplied inputs sanitized. |

---

## 7. Remaining Limitations

To ensure absolute transparency and engineering rigor, the following distinctions are documented:

1. **Hardware & Camera Ingestion**:
   - *Validated*: Physical host webcam (`cv2.VideoCapture(0)`) validated with live frame acquisition and YOLO tracking.
   - *Limitation*: A physical commercial ONVIF/RTSP camera at `192.168.1.120` was not reachable on the host's subnet. Hence, physical ONVIF validation is marked **NOT PROVEN**.
2. **Dashboard Visual Interaction**:
   - *Validated*: The production React dashboard bundle was built (`npm run build`), served via Nginx in Docker (`cctv-main-dashboard-1`), and programmatically validated for HTTP 200, API proxying, and WebSocket event distribution.
   - *Limitation*: Playwright headless browser driver binary download failed with an upstream CDN 404 from azureedge in this environment. Interactive GUI screenshots could not be taken via automated browser subagent; dashboard validation was completed programmatically via HTTP/WebSocket requests.
3. **Environmental Edge Cases**:
   - *Validated*: Real facial recognition on standard portrait/scene datasets, license plate OCR on cropped test footage, and tripwire geometric analytics.
   - *Limitation*: Field conditions including pitch-black infrared glare, heavy monsoon rain, high-speed vehicle motion blur, and extreme acute viewing angles (>60 degrees) were not tested.

---

## 8. Final Assessment

### Assessment: **PARTIALLY VALIDATED**

**Rationale**:
The system is **architecturally sound, deployable, and fully functional across all 11 Docker containers and backing stores**. All 109 automated unit and integration tests pass with 0 failures. The entire pipeline (`Camera → Edge → MQTT → Fog → MQTT → Central → PostgreSQL → Dashboard`) was verified with live data flowing through authenticated MQTT, PostgreSQL persistence, and WebSocket distribution.

The status is designated **PARTIALLY VALIDATED** rather than `READY` solely because:
1. Physical ONVIF hardware on `192.168.1.120` could not be physically contacted over the current local network.
2. Field-level adverse weather / infrared camera conditions remain to be validated in an actual physical perimeter deployment.

---

## 9. File / Code Change Report

### 1. Exact Files Modified
- `requirements.txt`: Added missing `httpx==0.28.1` required by `central/main.py`.
- `central/Dockerfile`: Added `libgl1` and `libglib2.0-0` system dependencies for OpenCV headless execution in Debian.
- `fog/Dockerfile`: Replaced deprecated `libgl1-mesa-glx` with `libgl1`.
- `edge/Dockerfile`: Replaced deprecated `libgl1-mesa-glx` with `libgl1`.
- `docker-compose.yml`: Corrected Attu port mapping from `8020:80` to `8020:3000`; injected `MILVUS_HOST: milvus` into Central service environment.
- `central/main.py`: Fixed Redis PubSub socket timeout handling to use `get_message(timeout=1.0)` with `(redis.exceptions.TimeoutError, TimeoutError)` handling; moved MQTT topic subscriptions into `on_connect` callback for broker restart resilience.
- `fog/main.py`: Moved MQTT topic subscriptions into `on_connect` callback for Mosquitto broker restart resilience.
- `fog/analytics.py`: Added deletion of prior subject entries by name in `VectorGallery.enroll` before inserting into Milvus to ensure metadata updates replace stale records without duplicates.
- `tests/test_frs_pipeline.py`: Added Milvus test collection cleanup in `setUp` and `tearDown` for test isolation.
- `tests/test_p4_live_validation.py`: Improved polling resilience for PostgreSQL persistence and WebSocket broadcast delivery during high test concurrency.

### 2. Exact Files Created
- `.env`: Deployment environment secrets and service configurations.
- `tests/test_p4_live_validation.py`: 17 live deployment integration tests validating containers, MQTT, PostgreSQL, Redis, Milvus, Nginx dashboard proxy, WebSockets, evidence retrieval, CPU inference, and recovery.
- `docs/p4_live_validation_report.md`: This comprehensive validation report.

### 3. Tests Run
- Full Test Suite: `python -m unittest discover -s tests -p "test_*.py"`
  - **Result**: `Ran 109 tests in 34.245s. OK (109 passed, 0 failed, 0 errors)`
- Live Integration Suite: `python -m unittest tests/test_p4_live_validation.py`
  - **Result**: `Ran 17 tests in 12.414s. OK (17 passed, 0 failed, 0 errors)`
- Frontend Production Build: `npm run build` (inside `dashboard/`)
  - **Result**: `Built in 2.97s. Zero errors.`
- Docker Deployment Status:
  - **Result**: All 11 containers `Up` and running. Central `/health` returns `{"status":"ok","service":"central"}` and `/ready` returns `status: ready, database: connected, redis: connected, frs_persistent_storage: True`.
