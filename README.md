# DRISHTI

**AI-Powered Multi-Camera Surveillance System**

Drishti is a modular AI surveillance platform for real-time monitoring, detection, tracking, identification, and alert generation across multiple cameras.

## Architecture

```text
Camera
   ↓
Edge
   ↓
MQTT — EdgeEvent v1
   ↓
Fog
   ↓
MQTT — AlertEvent v1
   ↓
Central
   ↓
PostgreSQL + WebSocket
   ↓
Dashboard
```

## Features

- Real-time object detection
- Person & vehicle detection
- Multi-object tracking
- Intrusion & line-crossing detection
- Restricted-zone detection
- Loitering detection
- Abandoned-object detection
- Automatic Number Plate Recognition (ANPR)
- OCR & license-plate watchlists
- Facial recognition
- Known / unknown face identification
- Face watchlists
- Multi-camera monitoring
- Real-time alerts
- Alert history & search
- Snapshots & video evidence
- Per-camera configuration
- Configurable confidence thresholds
- Dwell-time & alert cooldowns
- Low-bandwidth edge processing
- CPU-only operation
- Docker deployment
- RTSP / RTSPs / HTTP / HTTPS streams
- ONVIF camera discovery
- Local webcam support

## Tech Stack

### Edge

- Python
- OpenCV
- YOLO
- MQTT

### Fog

- Python
- EasyOCR
- InsightFace / ArcFace
- Milvus
- MQTT

### Central

- FastAPI
- PostgreSQL
- Redis
- WebSockets

### Dashboard

- React
- Vite
- Tailwind CSS

### Deployment

- Docker
- Docker Compose

## Getting Started

### Requirements

- Docker & Docker Compose
- Python 3
- Node.js
- Webcam or IP camera
- Windows, macOS, or Linux

### Windows

Start the system with:

```bat
run.bat
```

Or manually:

```bat
python scripts\host_camera_streamer.py
docker compose --profile pipeline up -d
```

Open:

```text
http://localhost:3000
```

### macOS / Linux

First time:

```bash
chmod +x run.sh
```

Then:

```bash
./run.sh
```

Or manually:

```bash
python3 scripts/host_camera_streamer.py &
docker compose --profile pipeline up -d
```

Dashboard:

```text
http://localhost:3000
```

## Camera Sources

Drishti supports direct camera streams:

- `rtsp://`
- `rtsps://`
- `http://`
- `https://`

ONVIF-compatible cameras can also be discovered and configured through the dashboard.

Camera configuration can be managed through the dashboard, with settings persisted in YAML.

## Dashboard

The dashboard provides:

- Multi-camera live monitoring
- Camera focus switching
- Object tracking overlays
- Face recognition results
- ANPR results
- Real-time alert feed
- Alert history
- Camera configuration
- Camera enable / disable
- Camera editing & removal
- Face & plate watchlists
- Evidence viewing

Historical alerts can be cleared from the dashboard without deleting camera configurations, watchlists, snapshots, or video evidence.

## Project Structure

```text
DRISHTI/
├── central/          # Central API & persistence
├── fog/              # Analytics & alert generation
├── edge/             # Camera ingestion & detection
├── ingestion/        # Camera & ONVIF handling
├── dashboard/        # React dashboard
├── config/           # Runtime configuration
├── scripts/          # Utility scripts
├── tests/            # Automated tests
├── snapshots/        # Runtime evidence
├── docker-compose.yml
├── run.bat
├── run.sh
└── README.md
```

## Configuration

Camera and module settings can be configured through the dashboard and YAML configuration files.

Runtime configuration and credentials should remain local.

Do not commit:

```text
.env
camera credentials
registered face data
snapshots
video evidence
database files
model weights
runtime logs
```

## Privacy & Security

Drishti is designed for local processing and does not require a cloud service.

Authentication is enabled for Central APIs, WebSockets, MQTT, and evidence access.

Sensitive runtime data is kept outside the source repository.

## Validation

The system has been tested across:

- Edge → Fog → Central event flow
- Multi-camera tracking
- RTSP stream ingestion
- ONVIF discovery pipeline
- ANPR
- Facial recognition
- Evidence capture
- Real-time dashboard updates
- Camera management
- Alert clearing
- Docker deployment
- CPU-only inference

Physical camera coverage and environmental conditions such as extreme darkness, IR glare, severe blur, and highly oblique license plates still require further field validation.

## Future Scope

- Dedicated plate detection models
- Improved adverse-weather ANPR
- Hardware-accelerated inference
- Larger-scale camera deployments
- Advanced operator roles & permissions
- Long-term analytics and reporting
- Improved edge resource optimization

## License

Add your preferred license here.

---

**Drishti — Smarter Vision. Safer Surveillance.**
