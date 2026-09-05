# CCTV AI Surveillance System

AI-powered multi-camera surveillance system with real-time detection, tracking, intrusion detection, ANPR, facial recognition, alerts, and evidence capture.

## Architecture

```text
Camera → Edge → MQTT → Fog → MQTT → Central → Dashboard
```

## Requirements

* Docker + Docker Compose
* Python 3
* Physical webcam or RTSP/HTTP/HTTPS IP camera
* macOS, Linux, or Windows

## Run

### Windows

```bat
run.bat
```

Or manually:

```bat
python scripts\host_camera_streamer.py
docker compose --profile pipeline up -d
```

Dashboard:

```text
http://localhost:3000
```

### macOS / Linux

Make the script executable once:

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

## IP Cameras

IP cameras can be added using a direct stream URL:

```text
rtsp://user:password@camera-ip:554/stream
```

Supported schemes:

* `rtsp://`
* `rtsps://`
* `http://`
* `https://`

ONVIF cameras can also be discovered through the dashboard.

## Stop

```bash
docker compose down
```

If the host camera streamer is running separately, stop that process as well.

## Configuration

Camera and module settings are managed through the project's YAML configuration files and dashboard configuration interface.

## Notes

* Processing is designed to run locally/edge-first.
* CPU-only operation is supported.
* No cloud service is required.
* Physical IP-camera/ONVIF compatibility depends on the camera and network configuration.
