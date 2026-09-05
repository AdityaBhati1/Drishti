"""Canonical Multi-Camera Edge service for real camera detections and tracking.

Runs isolated CameraWorker threads per configured active camera with:
- Independent stream connection & auto-reconnect logic
- Independent bounded frame queues dropping stale frames for zero latency buildup
- Independent YOLO models with isolated tracking state (no cross-camera track ID collision)
- Independent FPS pacing and evidence recording
- Centralized MQTT publication and dynamic camera configuration polling

No synthetic detections are emitted when a camera or model is unavailable.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import paho.mqtt.client as mqtt
from ultralytics import YOLO
import yaml

from shared.config import PROJECT_ROOT, settings
from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent
from ingestion.onvif.resolver import resolve_camera_source, find_camera_in_yaml
from ingestion.onvif.security import sanitize_url

try:
    from edge.evidence import EvidenceRecorder, EvidenceServer
except ImportError:
    from evidence import EvidenceRecorder, EvidenceServer

logger = logging.getLogger("edge")
CAMERA_ID = os.getenv("CAMERA_ID", "all")
RTSP_URL = os.getenv("RTSP_URL", "0")
CONFIDENCE = float(os.getenv("DETECTION_CONFIDENCE", "0.45"))
CROP_LABELS = {"car", "truck", "bus", "motorcycle", "person"}


def _resolve_model_path() -> str:
    env_path = os.getenv("YOLO_MODEL_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    candidates = [
        PROJECT_ROOT / "edge" / "yolov8n.pt",
        PROJECT_ROOT / "yolov8n.pt",
        Path("edge/yolov8n.pt"),
        Path("yolov8n.pt"),
    ]
    for c in candidates:
        if c.exists():
            return str(c.resolve())
    if env_path:
        return env_path
    return str(PROJECT_ROOT / "edge" / "yolov8n.pt")


MODEL_PATH = _resolve_model_path()


def event_from_result(camera_id: str, frame_id: int, result, frame: Optional[np.ndarray] = None) -> EdgeEvent:
    """Build canonical EdgeEvent from Ultralytics tracking results."""
    detections: list[Detection] = []
    h, w = (frame.shape[:2]) if frame is not None else (0, 0)
    for box in result.boxes:
        class_id = int(box.cls[0])
        label = str(result.names[class_id])
        raw_xy = box.xyxy[0]
        xy_list = raw_xy.tolist() if hasattr(raw_xy, "tolist") else list(raw_xy)
        coordinates = [float(value) for value in xy_list]
        track_id = str(int(box.id[0])) if box.id is not None else None

        crop_base64 = None
        if frame is not None and label.lower() in CROP_LABELS:
            x1, y1 = max(0, int(coordinates[0])), max(0, int(coordinates[1]))
            x2, y2 = min(w, int(coordinates[2])), min(h, int(coordinates[3]))
            if (x2 - x1) > 10 and (y2 - y1) > 10:
                crop = frame[y1:y2, x1:x2]
                ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                if ok:
                    crop_base64 = base64.b64encode(buf).decode("utf-8")

        detections.append(Detection(
            label=label,
            confidence=float(box.conf[0]),
            bbox=BoundingBox(x1=coordinates[0], y1=coordinates[1], x2=coordinates[2], y2=coordinates[3]),
            track_id=track_id,
            crop_base64=crop_base64,
        ))
    return EdgeEvent(camera_id=camera_id, frame_id=frame_id, detections=detections)


def resolve_capture_source_for_camera(camera_id: str, cam_config: Optional[dict[str, Any]] = None) -> int | str:
    """Resolve the active video capture source (ONVIF stream, RTSP, or local device) for a specific camera."""
    cam = cam_config
    if not cam:
        cam = find_camera_in_yaml(camera_id, settings.cameras_yaml_path)
    if not cam:
        cam = find_camera_in_yaml(camera_id, settings.config_yaml_path)

    if cam:
        stream_uri, meta = resolve_camera_source(cam, default_rtsp_url=RTSP_URL)
        if stream_uri:
            source_type = meta.get("type", "onvif" if "source" in cam else "configured")
            logger.info("[%s] Ingestion source resolved: %s (type=%s)",
                        camera_id, sanitize_url(stream_uri), source_type)
            return int(stream_uri) if str(stream_uri).isdigit() else stream_uri

    source: int | str = int(RTSP_URL) if RTSP_URL.isdigit() else RTSP_URL
    logger.info("[%s] Ingestion source from environment: %s", camera_id, sanitize_url(str(source)))
    return source


def resolve_capture_source() -> int | str:
    """Backward compatibility resolver for single-camera global CAMERA_ID."""
    return resolve_capture_source_for_camera(CAMERA_ID)


def open_capture_for_camera(camera_id: str, cam_config: Optional[dict[str, Any]] = None) -> cv2.VideoCapture:
    """Instantiate and open cv2.VideoCapture for a specific camera with Docker/TCP bridge support."""
    source = resolve_capture_source_for_camera(camera_id, cam_config)

    # If source is local device index 0 but /dev/video0 is not present (e.g. container on Windows host),
    # check if host camera streamer bridge is available at host.docker.internal:8085
    if (source == 0 or str(source) == "0") and not os.path.exists("/dev/video0"):
        host_bridge_url = "http://host.docker.internal:8085/video_feed"
        logger.info("[%s] Local /dev/video0 absent in container; checking host camera bridge at %s",
                    camera_id, host_bridge_url)
        cap = cv2.VideoCapture(host_bridge_url)
        if cap.isOpened():
            logger.info("[%s] Connected successfully to host camera bridge at %s", camera_id, host_bridge_url)
            return cap
        cap.release()

    # Enforce TCP transport for RTSP streams to avoid UDP timeout/packet drops
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://")):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

    return cv2.VideoCapture(source)


def open_capture() -> cv2.VideoCapture:
    """Backward compatibility open_capture for single-camera global CAMERA_ID."""
    return open_capture_for_camera(CAMERA_ID)


def resolve_target_fps_for_camera(camera_id: str, cam_config: Optional[dict[str, Any]] = None) -> float:
    """Resolve target frame rate from camera YAML config or environment."""
    cam = cam_config
    if not cam:
        cam = find_camera_in_yaml(camera_id, settings.cameras_yaml_path)
    if not cam:
        cam = find_camera_in_yaml(camera_id, settings.config_yaml_path)
    if cam and ("target_fps" in cam or "fps" in cam):
        try:
            return float(cam.get("target_fps", cam.get("fps")))
        except (ValueError, TypeError):
            pass
    try:
        return float(os.getenv("EDGE_TARGET_FPS", "10.0"))
    except (ValueError, TypeError):
        return 10.0


def resolve_target_fps() -> float:
    """Backward compatibility target FPS resolver for single-camera global CAMERA_ID."""
    return resolve_target_fps_for_camera(CAMERA_ID)


def discover_active_cameras(
    cameras_yaml_path: str = settings.cameras_yaml_path,
    config_yaml_path: str = settings.config_yaml_path,
) -> list[dict[str, Any]]:
    """Discover all enabled/active cameras from cameras.yaml and config.yaml.

    Never creates duplicate camera entries.
    """
    cameras: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for path in [cameras_yaml_path, config_yaml_path]:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                for cam in data.get("cameras", []):
                    if not isinstance(cam, dict):
                        continue
                    cam_id = str(cam.get("id") or cam.get("camera_id") or "").strip()
                    if not cam_id or cam_id in seen_ids:
                        continue
                    status = str(cam.get("status", "active")).strip().lower()
                    if status in ("inactive", "disabled"):
                        continue
                    seen_ids.add(cam_id)
                    cameras.append(cam)
        except Exception as exc:
            logger.warning("Error reading cameras config from %s: %s", path, exc)

    return cameras


class CameraWorker:
    """Independently managed camera ingestion and tracking worker."""

    def __init__(
        self,
        camera_id: str,
        cam_config: Optional[dict[str, Any]],
        mqtt_client: mqtt.Client,
        model_path: str = MODEL_PATH,
        confidence: float = CONFIDENCE,
    ):
        self.camera_id = camera_id
        self.cam_config = cam_config or {}
        self.mqtt_client = mqtt_client
        self.model_path = model_path
        self.confidence = confidence
        self.target_fps = resolve_target_fps_for_camera(self.camera_id, self.cam_config)
        self.frame_interval = 1.0 / self.target_fps if self.target_fps > 0 else 0.1

        self.evidence_recorder = EvidenceRecorder(camera_id=self.camera_id, target_fps=self.target_fps)
        self.stop_event = threading.Event()

        # Bounded frame queue: maximum 2 frames. Ingestion thread discards oldest frame if full.
        self._frame_queue: queue.Queue[tuple[int, float, np.ndarray]] = queue.Queue(maxsize=2)
        self._capture_thread: Optional[threading.Thread] = None
        self._inference_thread: Optional[threading.Thread] = None

        self.status = "initializing"
        self._model: Optional[YOLO] = None
        self.frame_id = 0
        self.dropped_frames = 0
        self.processed_frames = 0
        self.last_inference_time_ms = 0.0

    def start(self) -> None:
        """Start isolated capture and inference threads."""
        logger.info("[%s] Starting CameraWorker (target FPS: %.1f, interval: %.3fs)",
                    self.camera_id, self.target_fps, self.frame_interval)
        self.stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name=f"Capture-{self.camera_id}",
            daemon=True,
        )
        self._inference_thread = threading.Thread(
            target=self._inference_loop,
            name=f"Inference-{self.camera_id}",
            daemon=True,
        )
        self._capture_thread.start()
        self._inference_thread.start()

    def stop(self) -> None:
        """Stop worker and release camera and evidence resources."""
        logger.info("[%s] Stopping CameraWorker...", self.camera_id)
        self.stop_event.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2.0)
        if self._inference_thread and self._inference_thread.is_alive():
            self._inference_thread.join(timeout=2.0)
        try:
            self.evidence_recorder.close()
        except Exception as exc:
            logger.debug("[%s] Error closing evidence recorder: %s", self.camera_id, exc)
        self.status = "stopped"
        logger.info("[%s] CameraWorker stopped.", self.camera_id)

    def _capture_loop(self) -> None:
        """Dedicated network stream capture loop with bounded queue and auto-reconnect."""
        cap: Optional[cv2.VideoCapture] = None
        local_frame_id = 0

        while not self.stop_event.is_set():
            if cap is None or not cap.isOpened():
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                logger.info("[%s] Opening camera capture stream...", self.camera_id)
                cap = open_capture_for_camera(self.camera_id, self.cam_config)
                if not cap.isOpened():
                    self.status = "reconnecting"
                    logger.warning("[%s] Camera stream unavailable; retrying in 3 seconds", self.camera_id)
                    for _ in range(30):
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.1)
                    continue
                else:
                    self.status = "active"
                    logger.info("[%s] Camera stream opened successfully.", self.camera_id)

            try:
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.status = "reconnecting"
                    logger.warning("[%s] Stream returned no frame; reconnecting in 3 seconds", self.camera_id)
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    for _ in range(30):
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.1)
                    continue

                local_frame_id += 1
                now_ts = time.time()

                # Bounded queue: discard oldest frame if full to prevent latency buildup
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                        self.dropped_frames += 1
                    except queue.Empty:
                        pass
                self._frame_queue.put((local_frame_id, now_ts, frame))

            except Exception as exc:
                logger.error("[%s] Error in capture loop: %s", self.camera_id, exc)
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
                cap = None
                time.sleep(1.0)

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _inference_loop(self) -> None:
        """Isolated YOLO inference and tracking loop."""
        try:
            self._model = YOLO(self.model_path)
            logger.info("[%s] Loaded dedicated YOLO model from %s", self.camera_id, self.model_path)
        except Exception:
            logger.exception("[%s] Unable to load YOLO model; worker cannot run inference", self.camera_id)
            return

        while not self.stop_event.is_set():
            loop_start = time.time()
            try:
                item = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                continue

            frame_id, now_ts, frame = item
            self.frame_id = frame_id
            self.evidence_recorder.add_frame(frame, timestamp=now_ts, frame_id=frame_id)

            t_inf_start = time.time()
            try:
                result = self._model.track(frame, persist=True, conf=self.confidence, verbose=False)[0]
                self.last_inference_time_ms = (time.time() - t_inf_start) * 1000.0
                event = event_from_result(self.camera_id, frame_id, result, frame=frame)
                info = self.mqtt_client.publish(settings.mqtt_edge_topic, event.model_dump_json())
                self.processed_frames += 1

                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    logger.error("[%s] Failed to publish EdgeEvent %s: MQTT rc=%s",
                                 self.camera_id, event.event_id, info.rc)
                elif event.detections:
                    logger.info(
                        "[%s] Frame %d: %d detection(s) -> %s (track IDs: %s); published EdgeEvent %s (inf: %.1fms)",
                        self.camera_id,
                        frame_id,
                        len(event.detections),
                        [f"{d.label}:{d.confidence:.2f}" for d in event.detections],
                        [d.track_id for d in event.detections],
                        event.event_id,
                        self.last_inference_time_ms,
                    )
                elif frame_id % 50 == 0:
                    logger.info("[%s] Frame %d processed (no targets detected; inf: %.1fms, dropped: %d)",
                                self.camera_id, frame_id, self.last_inference_time_ms, self.dropped_frames)

            except Exception as exc:
                logger.error("[%s] Inference error on frame %d: %s", self.camera_id, frame_id, exc)

            # Frame rate pacing to respect target FPS
            elapsed = time.time() - loop_start
            sleep_time = self.frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


class EdgeService:
    """Multi-camera Edge service managing CameraWorkers, MQTT, and Evidence Server."""

    def __init__(self):
        self.workers: Dict[str, CameraWorker] = {}
        self.stop_event = threading.Event()
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="edge-service")
        self.mqtt_client.on_message = self._on_mqtt_message
        if settings.mqtt_username and settings.mqtt_password:
            self.mqtt_client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

        self._config_mtimes: Dict[str, float] = {}
        self._last_config_check = time.time()
        self.evidence_server: Optional[EvidenceServer] = None

    def _on_mqtt_message(self, client, userdata, msg) -> None:
        """Route incoming AlertEvents to the target camera's evidence recorder."""
        try:
            if msg and msg.payload:
                alert = AlertEvent.model_validate_json(msg.payload)
                worker = self.workers.get(alert.camera_id)
                if worker:
                    worker.evidence_recorder.handle_alert(alert)
        except Exception as exc:
            logger.debug("Edge ignored non-AlertEvent on %s: %s", getattr(msg, "topic", "unknown"), exc)

    def _start_evidence_server(self) -> None:
        if os.getenv("EDGE_EVIDENCE_SERVER_ENABLED", "true").lower() in ("true", "1", "yes"):
            try:
                self.evidence_server = EvidenceServer(
                    storage_dir=settings.snapshots_dir,
                    host="0.0.0.0",
                    port=settings.edge_evidence_port,
                    token=settings.evidence_token,
                )
                self.evidence_server.start()
                logger.info("Edge Evidence HTTP server started on port %d", settings.edge_evidence_port)
            except Exception as exc:
                logger.warning("Could not start Edge evidence server: %s", exc)

    def _check_and_update_cameras(self) -> None:
        """Check for configuration modifications and start/stop workers dynamically."""
        active_cams = discover_active_cameras()
        active_ids = {c.get("id") or c.get("camera_id") for c in active_cams if c.get("id") or c.get("camera_id")}

        # Check if single-camera override is explicitly requested
        single_override = os.getenv("CAMERA_ID_OVERRIDE", "").strip()
        if not single_override:
            cid_env = os.getenv("CAMERA_ID", "").strip()
            if cid_env and cid_env.lower() not in ("all", "all_cameras", "*", "", "cam-main-entrance"):
                single_override = cid_env

        if single_override:
            active_ids = {single_override}
            active_cams = [c for c in active_cams if (c.get("id") == single_override or c.get("camera_id") == single_override)]
            if not active_cams:
                active_cams = [{"id": single_override, "status": "active"}]

        # Start newly added cameras
        for cam in active_cams:
            cid = cam.get("id") or cam.get("camera_id")
            if cid and cid not in self.workers:
                worker = CameraWorker(
                    camera_id=cid,
                    cam_config=cam,
                    mqtt_client=self.mqtt_client,
                    model_path=MODEL_PATH,
                    confidence=CONFIDENCE,
                )
                self.workers[cid] = worker
                worker.start()

        # Stop removed / deactivated cameras
        for cid in list(self.workers.keys()):
            if cid not in active_ids:
                logger.info("Camera %s is no longer active; stopping worker", cid)
                w = self.workers.pop(cid)
                w.stop()

    def run(self) -> None:
        """Main service loop."""
        logger.info("Starting Edge multi-camera service...")
        self._start_evidence_server()

        # Connect MQTT
        try:
            self.mqtt_client.connect(settings.mqtt_broker, settings.mqtt_port, 60)
            self.mqtt_client.subscribe(settings.mqtt_fog_alert_topic)
            self.mqtt_client.loop_start()
            logger.info("Edge MQTT client connected to %s:%d", settings.mqtt_broker, settings.mqtt_port)
        except Exception as exc:
            logger.exception("Failed to connect Edge MQTT client: %s", exc)

        # Initial camera worker launch
        self._check_and_update_cameras()
        logger.info("Active camera workers (%d): %s", len(self.workers), list(self.workers.keys()))

        try:
            while not self.stop_event.is_set():
                time.sleep(5.0)
                # Periodic dynamic config check
                self._check_and_update_cameras()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Gracefully stop all camera workers, servers, and MQTT client."""
        logger.info("Shutting down Edge service...")
        self.stop_event.set()
        for cid, worker in list(self.workers.items()):
            try:
                worker.stop()
            except Exception as exc:
                logger.error("Error stopping worker %s: %s", cid, exc)
        self.workers.clear()

        if self.evidence_server:
            try:
                self.evidence_server.stop()
            except Exception:
                pass

        try:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        except Exception:
            pass
        logger.info("Edge service shutdown complete.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    service = EdgeService()

    def _signal_handler(sig, frame):
        logger.info("Caught signal %s; stopping Edge service...", sig)
        service.stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    service.run()


if __name__ == "__main__":
    main()
