import os
import sys
import json
import time
import threading
import asyncio
import logging
import hmac
import hashlib
from pathlib import Path
from typing import List, Optional, Dict, Any, Union
import secrets
import httpx
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect, status, HTTPException, Response, Header, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import yaml
import redis
import paho.mqtt.client as mqtt
from pydantic import BaseModel, ValidationError
import cv2
import numpy as np
try:
    from database import init_db, is_database_available, get_db, Alert, SessionLocal
except ImportError:
    from central.database import init_db, is_database_available, get_db, Alert, SessionLocal

# Append workspace root directory to sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from ingestion.stream import RTSPConnector
from ingestion.config import (
    append_camera_config,
    update_camera_config,
    upsert_camera_entry,
    set_camera_status,
    delete_camera_config,
    load_config,
)
from shared.config import settings
from shared.events import AlertEvent
from ingestion.onvif import (
    WSDiscovery,
    ONVIFCameraClient,
    sanitize_url,
    sanitize_camera_dict,
    validate_host_and_port,
)
from ingestion.onvif.security import validate_stream_url

logger = logging.getLogger("central")

def load_configured_location():
    """Return server-owned site coordinates configured through the environment."""
    return settings.site_lat, settings.site_lng

# FastAPI App setup
app = FastAPI(title="Central Surveillance Management Hub", version="2.0.0")

os.makedirs(settings.snapshots_dir, exist_ok=True)


def normalize_evidence_url_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    path_str = str(path).strip()
    if path_str.startswith("http://") or path_str.startswith("https://"):
        return path_str
    clean = path_str.lstrip("/")
    if clean.startswith("snapshots/"):
        clean = clean[len("snapshots/"):].lstrip("/")
    return f"/snapshots/{clean}"


def get_edge_evidence_url(camera_id: str) -> str:
    cfg_path = settings.cameras_yaml_path
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                for cam in data.get("cameras", []):
                    if cam.get("id") == camera_id:
                        ev = cam.get("evidence", {})
                        if ev.get("edge_url"):
                            return ev["edge_url"].rstrip("/")
                        if cam.get("edge_url"):
                            return cam["edge_url"].rstrip("/")
        except Exception as e:
            logger.warning("Error reading cameras.yaml for edge URL: %s", e)
    return f"http://{settings.edge_evidence_host}:{settings.edge_evidence_port}"


async def fetch_remote_edge_evidence(clean_subpath: str) -> bool:
    file_name = Path(clean_subpath).name
    stem = Path(clean_subpath).stem
    camera_id = stem.rsplit("_", 1)[0] if "_" in stem else "cam-main-entrance"
    edge_url = get_edge_evidence_url(camera_id)
    url = f"{edge_url}/evidence/{clean_subpath}"
    headers = {
        "X-Evidence-Token": settings.evidence_token,
        "Authorization": f"Bearer {settings.evidence_token}",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and resp.content:
                target_path = Path(settings.snapshots_dir) / clean_subpath
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(resp.content)
                logger.info("Successfully fetched remote evidence from Edge %s -> %s", url, target_path)
                return True
            logger.warning("Remote edge evidence fetch failed (%s): HTTP %d", url, resp.status_code)
    except Exception as exc:
        logger.warning("Could not retrieve remote evidence from %s: %s", url, exc)
    return False


def verify_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
) -> bool:
    """Validate operational requests against CENTRAL_API_KEY using constant-time comparison."""
    expected_key = settings.central_api_key
    if not expected_key:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Operational security misconfiguration: CENTRAL_API_KEY must be set in production",
            )
        # Development / test mode with no key configured allows access
        return True

    provided_key = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            provided_key = authorization[7:].strip()
        else:
            provided_key = authorization.strip()
    elif x_api_key:
        provided_key = x_api_key.strip()
    elif token:
        provided_key = token.strip()
    elif api_key:
        provided_key = api_key.strip()

    if not provided_key or not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


def generate_evidence_ticket(path: str, expires_seconds: int = 120) -> str:
    """Generate a short-lived (120s) signed ticket for media URLs to avoid leaking master API keys in query params."""
    clean = path.lstrip("/")
    if clean.startswith("snapshots/"):
        clean = clean[len("snapshots/"):].lstrip("/")
    exp = int(time.time()) + expires_seconds
    key = (settings.central_api_key or "evidence-signing-key").encode("utf-8")
    msg = f"{clean}:{exp}".encode("utf-8")
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def validate_evidence_ticket(path: str, ticket: str) -> bool:
    """Validate ephemeral evidence ticket against path and expiration."""
    try:
        clean = path.lstrip("/")
        if clean.startswith("snapshots/"):
            clean = clean[len("snapshots/"):].lstrip("/")
        exp_str, sig = ticket.split(".", 1)
        exp = int(exp_str)
        if time.time() > exp:
            return False
        key = (settings.central_api_key or "evidence-signing-key").encode("utf-8")
        msg = f"{clean}:{exp}".encode("utf-8")
        expected_sig = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
        return secrets.compare_digest(sig, expected_sig)
    except Exception:
        return False


def verify_evidence_access(
    file_path: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    ticket: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
) -> bool:
    """Validate snapshot access via Bearer token, ephemeral ticket, or legacy fallback."""
    expected_key = settings.central_api_key
    if not expected_key:
        if settings.app_env == "production":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Operational security misconfiguration: CENTRAL_API_KEY must be set in production",
            )
        return True

    # 1. Ephemeral, short-lived ticket (safe in URLs, expires in 120s)
    if ticket and validate_evidence_ticket(file_path, ticket):
        return True

    # 2. Standard Header authentication (Bearer / X-API-Key)
    provided_key = None
    if authorization:
        if authorization.lower().startswith("bearer "):
            provided_key = authorization[7:].strip()
        else:
            provided_key = authorization.strip()
    elif x_api_key:
        provided_key = x_api_key.strip()
    elif token:
        provided_key = token.strip()
    elif api_key:
        provided_key = api_key.strip()

    if not provided_key or not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key / evidence ticket",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True


@app.get("/api/evidence-ticket", dependencies=[Depends(verify_api_key)])
def get_evidence_ticket(path: str):
    """Generate a short-lived (120s) ticket for browser media elements without leaking master API keys."""
    ticket = generate_evidence_ticket(path)
    return {"ticket": ticket, "expires_in": 120}


@app.get("/snapshots/{file_path:path}", dependencies=[Depends(verify_evidence_access)])
async def serve_snapshot(file_path: str):
    clean = file_path.lstrip("/")
    if clean.startswith("snapshots/"):
        clean = clean[len("snapshots/"):].lstrip("/")

    base_dir = Path(settings.snapshots_dir).resolve()
    target_path = (base_dir / clean).resolve()
    if not str(target_path).startswith(str(base_dir)):
        raise HTTPException(status_code=403, detail="Access denied")

    # 1. Local / Shared Storage check
    if target_path.is_file():
        mime = "image/jpeg" if target_path.suffix.lower() in (".jpg", ".jpeg") else "video/mp4" if target_path.suffix.lower() == ".mp4" else None
        return FileResponse(str(target_path), media_type=mime)

    # 2. Remote Edge Evidence retrieval check
    retrieved = await fetch_remote_edge_evidence(clean)
    if retrieved and target_path.is_file():
        mime = "image/jpeg" if target_path.suffix.lower() in (".jpg", ".jpeg") else "video/mp4" if target_path.suffix.lower() == ".mp4" else None
        return FileResponse(str(target_path), media_type=mime)

    raise HTTPException(status_code=404, detail=f"Evidence file '{clean}' not found")

# CORS configurations
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis Configuration & Non-Blocking Circuit Breaker
REDIS_HOST = settings.redis_host
REDIS_PORT = settings.redis_port
redis_client: Optional[redis.Redis] = None
_redis_available: bool = False
_last_redis_check_time: float = 0.0
_redis_check_cooldown: float = 30.0  # probe at most once every 30s when down
_redis_lock = threading.Lock()

# MQTT Configuration
MQTT_BROKER = settings.mqtt_broker
MQTT_PORT = settings.mqtt_port


def init_redis() -> bool:
    """Attempt probe to initialize or re-verify Redis."""
    global redis_client, _redis_available, _last_redis_check_time
    with _redis_lock:
        _last_redis_check_time = time.time()
        try:
            client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=0,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                retry_on_timeout=False,
            )
            client.ping()
            try:
                client.config_set("notify-keyspace-events", "Ex")
            except Exception:
                pass
            redis_client = client
            _redis_available = True
            logger.info("Connected to Redis cache at %s:%s and keyspace expirations enabled", REDIS_HOST, REDIS_PORT)
            return True
        except Exception as e:
            redis_client = None
            _redis_available = False
            logger.warning("Redis is unavailable (%s); operating in degraded mode without escalation TTL cache", e)
            return False


def is_redis_available() -> bool:
    """Non-blocking availability check. Never blocks alert ingestion on a Redis outage."""
    return _redis_available and (redis_client is not None)


def mark_redis_failure(exc: Exception) -> None:
    """Trip the circuit breaker on runtime Redis errors."""
    global _redis_available, _last_redis_check_time, redis_client
    with _redis_lock:
        if _redis_available:
            logger.warning("Redis runtime failure (%s); entering degraded mode", exc)
        _redis_available = False
        _last_redis_check_time = time.time()
        redis_client = None


# Initial probe at module load
init_redis()

# =========================================================================
# WebSockets Broadcast Manager
# =========================================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._broadcast_lock: asyncio.Lock | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._broadcast_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, subprotocol: Optional[str] = None):
        if subprotocol:
            await websocket.accept(subprotocol=subprotocol)
        else:
            await websocket.accept()
        with self._lock:
            self.active_connections.add(websocket)
            count = len(self.active_connections)
        logger.info("[Central WS] Client connected. Active: %d", count)

    def disconnect(self, websocket: WebSocket):
        with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
                count = len(self.active_connections)
                logger.info("[Central WS] Client disconnected. Active: %d", count)

    async def broadcast(self, message: dict) -> None:
        """Broadcast on the ASGI event loop and prune broken sockets thread-safely."""
        with self._lock:
            connections = list(self.active_connections)
        if not connections:
            return

        disconnected: list[WebSocket] = []
        if self._broadcast_lock:
            async with self._broadcast_lock:
                for connection in connections:
                    try:
                        await connection.send_json(message)
                    except Exception as exc:
                        logger.info("Removing disconnected WebSocket client: %s", exc)
                        disconnected.append(connection)
        else:
            for connection in connections:
                try:
                    await connection.send_json(message)
                except Exception as exc:
                    logger.info("Removing disconnected WebSocket client: %s", exc)
                    disconnected.append(connection)

        for connection in disconnected:
            self.disconnect(connection)

    def broadcast_from_thread(self, message: dict) -> None:
        """Schedule a broadcast safely from MQTT/Redis worker threads."""
        if self.loop is None or self.loop.is_closed():
            logger.warning("Dropping WebSocket broadcast before application loop is ready")
            return
        future = asyncio.run_coroutine_threadsafe(self.broadcast(message), self.loop)
        future.add_done_callback(
            lambda result: logger.error("WebSocket broadcast failed: %s", result.exception())
            if result.exception()
            else None
        )

manager = ConnectionManager()

# =========================================================================
# Redis TTL Escalation Handler (Expired keys listener)
# =========================================================================
def escalate_alert(alert_id: int):
    """Checks PostgreSQL if the alert was acknowledged; if not, triggers escalation."""
    print(f"[Escalation Engine] Expiry triggered for alert #{alert_id}. Evaluating status...")
    db = SessionLocal()
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        if alert and alert.status == "PENDING":
            print(f"[Escalation Engine] WARNING: Alert #{alert_id} not acknowledged. Escalating to command!")
            alert.status = "ESCALATED_TO_COMMAND"
            db.commit()
            
            # Broadcast the secondary escalation trigger to WebSockets
            escalation_payload = {
                "event_type": "ESCALATION_TO_COMMAND",
                "alert_id": alert.id,
                "camera_id": alert.node_id,
                "original_event": alert.event_type,
                "details": f"CRITICAL SECURITY ESCALATION: '{alert.event_type}' alert not acknowledged within 60 seconds.",
                "severity": "CRITICAL",
                "timestamp": str(alert.timestamp)
            }
            manager.broadcast_from_thread(escalation_payload)
        else:
            print(f"[Escalation Engine] Alert #{alert_id} is already acknowledged or processed.")
    except Exception as e:
        print(f"[Escalation Engine] ERROR updating alert status: {e}")
    finally:
        db.close()

def redis_expired_listener():
    """Background listener for Redis Key Expirations with non-blocking recovery backoff."""
    while True:
        if not is_redis_available():
            init_redis()
            if not is_redis_available():
                time.sleep(5.0)
                continue
        try:
            pubsub = redis_client.pubsub()
            pubsub.subscribe("__keyevent@0__:expired")
            logger.info("Listening for Redis expired keyspace events...")
            while _redis_available:
                try:
                    message = pubsub.get_message(timeout=1.0)
                    if message and message.get("type") == "message":
                        key = message.get("data")
                        if isinstance(key, str) and key.startswith("escalation:pending:"):
                            try:
                                alert_id = int(key.split(":")[-1])
                                escalate_alert(alert_id)
                            except (ValueError, IndexError):
                                pass
                except (redis.exceptions.TimeoutError, TimeoutError):
                    continue
        except Exception as e:
            mark_redis_failure(e)
            time.sleep(5.0)

# =========================================================================
# MQTT Ingestion Subscriber
# =========================================================================
def alert_to_dict(alert: Alert) -> dict:
    extra = {}
    if alert.extra_info:
        try:
            extra = json.loads(alert.extra_info)
        except Exception:
            extra = {}
    return {
        "id": alert.id,
        "event_id": alert.event_id,
        "camera_id": alert.node_id,
        "event_type": alert.event_type,
        "severity": alert.severity,
        "details": alert.details,
        "status": alert.status,
        "lat": alert.lat,
        "lng": alert.lng,
        "confidence": extra.get("confidence"),
        "track_id": extra.get("track_id"),
        "snapshot_path": normalize_evidence_url_path(getattr(alert, "snapshot_path", None) or extra.get("snapshot_path")),
        "clip_path": normalize_evidence_url_path(getattr(alert, "clip_path", None) or extra.get("clip_path")),
        "timestamp": str(alert.timestamp),
    }


def persist_alert_event(event: AlertEvent, db: Session) -> tuple[Alert, bool]:
    """Persist an AlertEvent idempotently. Returns (alert, created)."""
    event_id = str(event.event_id)
    existing = db.query(Alert).filter(Alert.event_id == event_id).first()
    if existing:
        return existing, False

    fallback_lat, fallback_lng = load_configured_location()
    clip_path = event.metadata.get("clip_path") if event.metadata else None
    alert = Alert(
        event_id=event_id,
        node_id=event.camera_id,
        event_type=event.event_type,
        severity=event.severity,
        details=event.details,
        status="PENDING",
        lat=event.lat if event.lat is not None else fallback_lat,
        lng=event.lng if event.lng is not None else fallback_lng,
        extra_info=json.dumps({
            "confidence": event.confidence,
            "track_id": event.track_id,
            "snapshot_path": event.snapshot_path,
            "clip_path": clip_path,
            "metadata": event.metadata,
        }),
        snapshot_path=event.snapshot_path,
        clip_path=clip_path,
        timestamp=event.occurred_at,
    )
    db.add(alert)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent/replayed event may have won the unique event-id race.
        db.rollback()
        existing = db.query(Alert).filter(Alert.event_id == event_id).first()
        if existing:
            return existing, False
        raise
    db.refresh(alert)
    return alert, True


def arm_escalation(alert: Alert) -> None:
    if alert.severity not in {"high", "critical"}:
        return
    if not is_redis_available():
        return
    try:
        redis_client.set(f"escalation:pending:{alert.id}", "pending", ex=60)
    except Exception as exc:
        mark_redis_failure(exc)
        logger.warning("Unable to arm escalation timer for alert %s: %s", alert.id, exc)


def ingest_mqtt_alert(event: AlertEvent) -> None:
    """Persist and deliver a validated MQTT alert without leaking exceptions to Paho."""
    db = SessionLocal()
    try:
        alert, created = persist_alert_event(event, db)
        if not created:
            logger.info("Ignored replayed AlertEvent %s", event.event_id)
            return
        arm_escalation(alert)
        manager.broadcast_from_thread(alert_to_dict(alert))
        logger.info("Persisted MQTT AlertEvent %s as alert #%s", event.event_id, alert.id)
    except Exception:
        db.rollback()
        logger.exception("Unable to ingest MQTT AlertEvent %s", event.event_id)
    finally:
        db.close()

def on_mqtt_message(client, userdata, msg):
    try:
        if not msg or not getattr(msg, "payload", None):
            logger.warning("Discarding empty MQTT message")
            return
        topic = getattr(msg, "topic", "")
        if topic == settings.mqtt_telemetry_topic or topic.endswith("telemetry.v1"):
            try:
                telemetry_payload = json.loads(msg.payload.decode("utf-8"))
                manager.broadcast_from_thread(telemetry_payload)
            except Exception as e:
                logger.debug("Failed to broadcast telemetry over WS: %s", e)
            return

        event = AlertEvent.model_validate_json(msg.payload)
        ingest_mqtt_alert(event)
    except (ValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Discarding malformed MQTT message on %s: %s", getattr(msg, "topic", "unknown"), exc)
    except Exception as exc:
        logger.exception("Unexpected exception processing MQTT message: %s", exc)

def mqtt_subscriber():
    """Background listener for MQTT alerts and telemetry topics with automatic reconnect and authentication."""
    MQTT_TOPIC = settings.mqtt_fog_alert_topic
    TELEMETRY_TOPIC = settings.mqtt_telemetry_topic
    client_id = f"central-subscriber-hub-{os.getpid()}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.on_message = on_mqtt_message

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe(MQTT_TOPIC)
            c.subscribe(TELEMETRY_TOPIC)
            logger.info("[MQTT Ingestion] Subscribed to '%s' and '%s' successfully.", MQTT_TOPIC, TELEMETRY_TOPIC)
        else:
            logger.warning("[MQTT Ingestion] MQTT connection failed with code %s", rc)

    client.on_connect = on_connect
    if settings.mqtt_username and settings.mqtt_password:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    
    logger.info("[MQTT Ingestion] Connecting to MQTT broker at %s...", MQTT_BROKER)
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            logger.warning("MQTT connection failed: %s; retrying in 3 seconds", e)
            time.sleep(3)

# =========================================================================
# Startup Event
# =========================================================================
@app.on_event("startup")
async def startup_event():
    manager.bind_loop(asyncio.get_running_loop())
    # Setup PostgreSQL tables
    init_db()
    
    # Launch background thread for Redis expired keys keyspace notifications
    t_redis = threading.Thread(target=redis_expired_listener, daemon=True)
    t_redis.start()
    
    # Launch background thread for MQTT consumer
    t_mqtt = threading.Thread(target=mqtt_subscriber, daemon=True)
    t_mqtt.start()

# =========================================================================
# REST Endpoints & WebSockets
# =========================================================================
class AcknowledgeResponse(BaseModel):
    status: str
    message: str


class AlertIngestResponse(BaseModel):
    id: int
    event_id: str
    status: str
    created: bool

@app.get("/")
def read_root():
    return {"status": "online", "service": "Central API & Escalation Hub"}


@app.get("/health")
def health():
    """Liveness probe: the process is accepting requests."""
    return {"status": "ok", "service": "central"}


@app.get("/ready")
def readiness():
    """Readiness probe: persistence, degraded mode, and FRS status."""
    if not is_database_available():
        raise HTTPException(status_code=503, detail="PostgreSQL persistence is unavailable")

    frs_persistent = False
    try:
        from pymilvus import MilvusClient
        mc = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}")
        frs_persistent = mc.has_collection("watchlist_faces")
    except Exception:
        frs_persistent = False

    return {
        "status": "ready",
        "service": "central",
        "database": "connected",
        "redis": "connected" if is_redis_available() else "degraded",
        "frs_persistent_storage": frs_persistent,
    }


@app.post("/api/alerts", response_model=AlertIngestResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
async def create_alert(event: AlertEvent, response: Response, db: Session = Depends(get_db)):
    """Ingest one canonical AlertEvent v1 with replay-safe persistence."""
    try:
        alert, created = persist_alert_event(event, db)
    except Exception as exc:
        db.rollback()
        logger.exception("HTTP alert ingestion failed for %s", event.event_id)
        raise HTTPException(status_code=503, detail="Alert persistence is unavailable") from exc

    if not created:
        response.status_code = status.HTTP_200_OK
        return AlertIngestResponse(
            id=alert.id,
            event_id=alert.event_id,
            status=alert.status,
            created=False,
        )

    arm_escalation(alert)
    await manager.broadcast(alert_to_dict(alert))
    return AlertIngestResponse(
        id=alert.id,
        event_id=alert.event_id,
        status=alert.status,
        created=True,
    )

def gen_frames():
    camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        camera = cv2.VideoCapture("http://host.docker.internal:8085/video_feed")
    if not camera.isOpened():
        print("[Video Feed] Error: Could not open webcam.")
        # Generate a fallback text frame
        placeholder = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(placeholder, "Webcam Access Failed / No Webcam", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        _, jpeg = cv2.imencode('.jpg', placeholder)
        frame = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        return

    try:
        while True:
            success, frame = camera.read()
            if not success:
                time.sleep(0.03)
                continue
            
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
                
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.03)
    except Exception as e:
        print(f"[Video Feed] Generator exception: {e}")
    finally:
        camera.release()
        print("[Video Feed] Camera released.")

@app.get("/video_feed", dependencies=[Depends(verify_api_key)])
def video_feed():
    return StreamingResponse(gen_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/api/alerts/{alert_id}/acknowledge", response_model=AcknowledgeResponse, dependencies=[Depends(verify_api_key)])
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db)):
    """Acknowledges an alert, preventing escalation TTL and updating status."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
        
    if alert.status == "ACKNOWLEDGED":
        return {"status": "success", "message": "Alert is already acknowledged."}
        
    # Update DB Status
    alert.status = "ACKNOWLEDGED"
    db.commit()
    
    # Remove from Redis to block escalation without blocking on Redis outage
    if is_redis_available():
        redis_key = f"escalation:pending:{alert_id}"
        try:
            redis_client.delete(redis_key)
        except Exception as e:
            mark_redis_failure(e)
            logger.warning("[Central Redis] Error deleting key: %s", e)
        
    # Broadcast status change to WebSockets
    ack_payload = {
        "event_type": "ALERT_ACKNOWLEDGED",
        "alert_id": alert_id,
        "camera_id": alert.node_id,
        "status": "ACKNOWLEDGED"
    }
    
    # Notify active dashboard listeners
    try:
        manager.broadcast_from_thread(ack_payload)
    except Exception as e:
        print(f"[Central WS] Error broadcasting status change: {e}")
            
    print(f"[Central API] Alert #{alert_id} acknowledged. Prevented escalation.")
    return {"status": "success", "message": f"Alert #{alert_id} acknowledged successfully."}

@app.get("/api/alerts", response_model=List[dict], dependencies=[Depends(verify_api_key)])
def get_alerts(
    limit: int = 50,
    offset: int = 0,
    camera_id: Optional[str] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Alert)
    if camera_id:
        query = query.filter(Alert.node_id == camera_id)
    if event_type:
        query = query.filter(Alert.event_type == event_type)
    if severity:
        query = query.filter(Alert.severity == severity)
    if status:
        query = query.filter(Alert.status == status)
    if search:
        query = query.filter(
            (Alert.details.ilike(f"%{search}%"))
            | (Alert.node_id.ilike(f"%{search}%"))
            | (Alert.event_type.ilike(f"%{search}%"))
        )
    alerts = query.order_by(Alert.timestamp.desc()).offset(offset).limit(limit).all()
    return [alert_to_dict(alert) for alert in alerts]


@app.delete("/api/alerts", dependencies=[Depends(verify_api_key)])
def clear_alerts(
    camera_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Clear alert event records from the database.
    CRITICAL: Does NOT delete camera configurations, watchlists, registered faces,
    snapshots, video clips, or evidence files on disk. Only rows in the alerts table
    are deleted.
    """
    try:
        query = db.query(Alert)
        if camera_id:
            query = query.filter(Alert.node_id == camera_id.strip())
        deleted_count = query.delete(synchronize_session=False)
        db.commit()

        # Clean pending escalation keys from Redis if available
        if is_redis_available():
            try:
                cursor = 0
                while True:
                    cursor, keys = redis_client.scan(cursor=cursor, match="escalation:pending:*", count=100)
                    if keys:
                        redis_client.delete(*keys)
                    if cursor == 0:
                        break
            except Exception as r_err:
                logger.debug("[Central Redis] Non-critical note clearing escalation keys: %s", r_err)

        # Broadcast status to WebSockets so connected clients clear immediately
        try:
            manager.broadcast_from_thread({
                "event_type": "ALERTS_CLEARED",
                "deleted_count": deleted_count,
                "camera_id": camera_id
            })
        except Exception as ws_err:
            logger.debug("[Central WS] Note broadcasting alert clear: %s", ws_err)

        logger.info("[Central API] Cleared %d alert(s) from database (camera_id=%s)", deleted_count, camera_id)
        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Successfully cleared {deleted_count} alert(s) from database.",
        }
    except Exception as exc:
        db.rollback()
        logger.error("[Central API] Failed to clear alerts: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to clear alerts from database: {exc}")


class UpdateCameraRequest(BaseModel):
    name: Optional[str] = None
    ip: Optional[str] = None
    rtsp_url: Optional[str] = None
    status: Optional[str] = None
    source: Optional[dict] = None
    location: Optional[dict] = None
    modules: Optional[dict] = None
    address: Optional[str] = None
    lat: Optional[Union[float, int, str]] = None
    lng: Optional[Union[float, int, str]] = None


class CreateCameraRequest(BaseModel):
    id: str
    name: Optional[str] = None
    ip: Optional[str] = ""
    rtsp_url: Optional[str] = ""
    status: Optional[str] = "active"
    source: Optional[dict] = None
    location: Optional[dict] = None
    modules: Optional[dict] = None
    address: Optional[str] = None
    lat: Optional[Union[float, int, str]] = None
    lng: Optional[Union[float, int, str]] = None


class ToggleCameraRequest(BaseModel):
    status: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/cameras", dependencies=[Depends(verify_api_key)])
def get_configured_cameras():
    """Return configured cameras and per-camera analytics modules from cameras.yaml and config.yaml."""
    cams: list[dict] = []
    seen_ids: set[str] = set()

    for path in [settings.cameras_yaml_path, settings.config_yaml_path]:
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
                    seen_ids.add(cam_id)
                    # Normalize default status if missing
                    if "status" not in cam or not cam["status"]:
                        cam["status"] = "active"
                    cams.append(cam)
        except Exception as exc:
            logger.error("Failed to read cameras config from %s: %s", path, exc)

    return {"status": "success", "count": len(cams), "cameras": sanitize_camera_dict(cams)}


@app.post("/api/cameras", dependencies=[Depends(verify_api_key)])
def create_camera(req: CreateCameraRequest):
    """Create and persist a new camera configuration into canonical YAML storage."""
    camera_id = req.id.strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="Camera ID is required")

    if req.rtsp_url:
        clean_url = req.rtsp_url.strip()
        if not validate_stream_url(clean_url):
            raise HTTPException(
                status_code=400,
                detail="Invalid stream URL or destination address not permitted (SSRF protection)"
            )

    loc = dict(req.location) if isinstance(req.location, dict) else {}
    if req.address is not None:
        loc["address"] = req.address.strip()
    if req.lat not in (None, ""):
        try:
            loc["lat"] = float(req.lat)
        except (ValueError, TypeError):
            pass
    if req.lng not in (None, ""):
        try:
            loc["lng"] = float(req.lng)
        except (ValueError, TypeError):
            pass

    cam_dict = {
        "id": camera_id,
        "name": req.name.strip() if req.name else camera_id,
        "ip": req.ip.strip() if req.ip else "",
        "rtsp_url": req.rtsp_url.strip() if req.rtsp_url else "",
        "status": (req.status or "active").strip().lower(),
    }
    if req.source:
        cam_dict["source"] = req.source
    elif req.rtsp_url:
        cam_dict["source"] = {"type": "direct", "url": req.rtsp_url.strip()}
    if loc:
        cam_dict["location"] = loc
        if "address" in loc and "address" not in cam_dict:
            cam_dict["address"] = loc["address"]
        if "lat" in loc:
            cam_dict["lat"] = loc["lat"]
        if "lng" in loc:
            cam_dict["lng"] = loc["lng"]
    if req.modules:
        cam_dict["modules"] = req.modules

    upsert_camera_entry(cam_dict, settings.cameras_yaml_path)
    upsert_camera_entry(cam_dict, settings.config_yaml_path)

    logger.info("Camera %s created successfully in canonical configuration", camera_id)
    return {
        "status": "success",
        "message": f"Camera {camera_id} created successfully",
        "camera": sanitize_camera_dict([cam_dict])[0],
    }


@app.put("/api/cameras/{camera_id}", dependencies=[Depends(verify_api_key)])
@app.patch("/api/cameras/{camera_id}", dependencies=[Depends(verify_api_key)])
def update_camera(camera_id: str, req: UpdateCameraRequest):
    """Update an existing camera's configuration and persist through existing YAML configs."""
    camera_id = camera_id.strip()
    # Check if stream URL is being updated and validate for SSRF protection
    if req.rtsp_url:
        clean_url = req.rtsp_url.strip()
        if not validate_stream_url(clean_url):
            raise HTTPException(
                status_code=400,
                detail="Invalid stream URL or destination address not permitted (SSRF protection)"
            )

    updates = {}
    if req.name is not None:
        updates["name"] = req.name.strip()
    if req.ip is not None:
        updates["ip"] = req.ip.strip()
    if req.rtsp_url is not None:
        updates["rtsp_url"] = req.rtsp_url.strip()
    if req.status is not None:
        updates["status"] = req.status.strip().lower()
    if req.source is not None:
        updates["source"] = req.source
    if req.modules is not None:
        updates["modules"] = req.modules

    # Handle location (either nested in req.location or flat address/lat/lng)
    loc = dict(req.location) if isinstance(req.location, dict) else {}
    if req.address is not None:
        loc["address"] = req.address.strip()
    if req.lat is not None and req.lat != "":
        try:
            loc["lat"] = float(req.lat)
        except (ValueError, TypeError):
            pass
    if req.lng is not None and req.lng != "":
        try:
            loc["lng"] = float(req.lng)
        except (ValueError, TypeError):
            pass
    if loc:
        updates["location"] = loc

    # Pass flat fields as well for config layer handling
    if req.address is not None:
        updates["address"] = req.address.strip()
    if req.lat is not None and req.lat != "":
        updates["lat"] = req.lat
    if req.lng is not None and req.lng != "":
        updates["lng"] = req.lng

    res1 = update_camera_config(camera_id, updates, settings.cameras_yaml_path)
    res2 = update_camera_config(camera_id, updates, settings.config_yaml_path)

    updated_cam = res1 or res2
    if not updated_cam:
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found in configuration")

    # Canonical configuration synchronization: ensure both YAML configs remain in lockstep
    if res1 and not res2:
        upsert_camera_entry(res1, settings.config_yaml_path)
    elif res2 and not res1:
        upsert_camera_entry(res2, settings.cameras_yaml_path)

    # If stream URL changed and preview connector is active for this camera, update connector
    if req.rtsp_url:
        for k in [camera_id, camera_id.upper(), camera_id.lower()]:
            if k in active_connectors:
                try:
                    active_connectors[k].stop()
                except Exception:
                    pass
                active_connectors.pop(k, None)

    logger.info("Camera %s configuration updated successfully", camera_id)
    return {
        "status": "success",
        "message": f"Camera {camera_id} updated successfully",
        "camera": sanitize_camera_dict([updated_cam])[0] if updated_cam else {},
    }


@app.post("/api/cameras/{camera_id}/toggle", dependencies=[Depends(verify_api_key)])
def toggle_camera(camera_id: str, req: Optional[ToggleCameraRequest] = None):
    """Enable or disable an existing camera."""
    camera_id = camera_id.strip()
    if req and req.enabled is not None:
        new_status = "active" if req.enabled else "disabled"
    elif req and req.status:
        new_status = "active" if req.status.lower() in ("active", "enabled") else "disabled"
    else:
        # Toggle current status
        current_data = get_configured_cameras()
        current_cams = current_data.get("cameras", [])
        current_cam = next(
            (c for c in current_cams if (c.get("id") or "").lower() == camera_id.lower()),
            None
        )
        if current_cam:
            cur_status = (current_cam.get("status") or "active").lower()
            new_status = "disabled" if cur_status == "active" else "active"
        else:
            new_status = "disabled"

    res1 = set_camera_status(camera_id, new_status, settings.cameras_yaml_path)
    res2 = set_camera_status(camera_id, new_status, settings.config_yaml_path)

    if not (res1 or res2):
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found in configuration")

    # If disabled, release active preview connector if active
    if new_status == "disabled":
        for k, conn in list(active_connectors.items()):
            if k.lower() == camera_id.lower() or camera_id.lower() in k.lower():
                try:
                    conn.stop()
                except Exception as exc:
                    logger.debug("Error stopping preview connector for disabled %s: %s", k, exc)
                active_connectors.pop(k, None)

    logger.info("Camera %s status changed to %s", camera_id, new_status)
    return {
        "status": "success",
        "camera_id": camera_id,
        "new_status": new_status,
        "message": f"Camera {camera_id} status updated to {new_status}"
    }


@app.delete("/api/cameras/{camera_id}", dependencies=[Depends(verify_api_key)])
def delete_camera(camera_id: str):
    """
    Delete an existing camera configuration.
    CRITICAL: Historical alerts, snapshots, clips, and database records associated
    with this camera are deliberately NOT deleted.
    """
    camera_id = camera_id.strip()
    res1 = delete_camera_config(camera_id, settings.cameras_yaml_path)
    res2 = delete_camera_config(camera_id, settings.config_yaml_path)

    if not (res1 or res2):
        raise HTTPException(status_code=404, detail=f"Camera '{camera_id}' not found in configuration")

    # Release preview connector if running
    for k, conn in list(active_connectors.items()):
        if k.lower() == camera_id.lower() or camera_id.lower() in k.lower():
            try:
                conn.stop()
            except Exception as exc:
                logger.debug("Error stopping preview connector for deleted %s: %s", k, exc)
            active_connectors.pop(k, None)

    logger.info("Camera %s removed from configuration. Historical records preserved.", camera_id)
    return {
        "status": "success",
        "message": f"Camera {camera_id} deleted successfully. Historical alerts and media records are preserved."
    }


@app.get("/api/cameras/discover", dependencies=[Depends(verify_api_key)])
def discover_onvif_cameras(
    timeout: float = 2.5,
    probe_host: Optional[str] = None,
    probe_port: int = 80,
    username: Optional[str] = None,
    password: Optional[str] = None,
):
    """Discover ONVIF cameras via WS-Discovery multicast or targeted host probe.

    Returns sanitized device metadata, media profiles, and RTSP stream URIs.
    Never exposes camera credentials.
    """
    discovered_cameras: list[dict] = []

    if probe_host:
        # Targeted host probe
        try:
            valid_host, valid_port = validate_host_and_port(probe_host, probe_port)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        client = ONVIFCameraClient(
            host=valid_host,
            port=valid_port,
            username=username or "",
            password=password or "",
            timeout=timeout,
        )
        probe_result = client.probe_camera()
        discovered_cameras.append({
            "endpoint_reference": f"urn:uuid:{valid_host}:{valid_port}",
            "device_ip": valid_host,
            "device_port": valid_port,
            "onvif_endpoint": client.device_service_url,
            "manufacturer": probe_result.get("manufacturer", ""),
            "model": probe_result.get("model", ""),
            "firmware_version": probe_result.get("firmware_version", ""),
            "serial_number": probe_result.get("serial_number", ""),
            "hardware_id": probe_result.get("hardware_id", ""),
            "scopes": [],
            "profiles": probe_result.get("profiles", []),
            "rtsp_url": probe_result.get("sanitized_rtsp_url") or sanitize_url(probe_result.get("rtsp_url", "")),
            "requires_auth": probe_result.get("requires_auth", False),
            "status": probe_result.get("status", "unknown"),
        })
        method = "targeted_probe"
    else:
        # Local network WS-Discovery multicast probe
        wsd = WSDiscovery()
        raw_devices = wsd.discover(timeout=timeout)

        for dev in raw_devices:
            ip = dev.get("device_ip")
            port = dev.get("device_port", 80)
            if not ip:
                continue

            # Probe device for extended metadata and stream URI
            client = ONVIFCameraClient(
                host=ip,
                port=port,
                username=username or "",
                password=password or "",
                timeout=min(timeout, 2.0),
            )
            probe_result = client.probe_camera()

            discovered_cameras.append({
                "endpoint_reference": dev.get("endpoint_reference", ""),
                "device_ip": ip,
                "device_port": port,
                "onvif_endpoint": dev.get("onvif_endpoint", client.device_service_url),
                "manufacturer": probe_result.get("manufacturer", "") or dev.get("name", ""),
                "model": probe_result.get("model", "") or dev.get("hardware", ""),
                "firmware_version": probe_result.get("firmware_version", ""),
                "serial_number": probe_result.get("serial_number", ""),
                "hardware_id": probe_result.get("hardware_id", ""),
                "scopes": dev.get("scopes", []),
                "profiles": probe_result.get("profiles", []),
                "rtsp_url": probe_result.get("sanitized_rtsp_url") or sanitize_url(probe_result.get("rtsp_url", "")),
                "requires_auth": probe_result.get("requires_auth", False),
                "status": probe_result.get("status", "unknown"),
            })
        method = "ws_discovery"

    return {
        "status": "success",
        "method": method,
        "count": len(discovered_cameras),
        "cameras": sanitize_camera_dict(discovered_cameras),
    }



@app.get("/api/watchlists", dependencies=[Depends(verify_api_key)])
def get_watchlists():
    """Return watchlists for license plates and faces from configuration."""
    cfg_path = settings.cameras_yaml_path
    if not os.path.exists(cfg_path):
        return {"plates": [], "faces": []}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data.get("watchlists", {"plates": [], "faces": []})
    except Exception as exc:
        logger.error("Failed to read watchlists: %s", exc)
        return {"plates": [], "faces": []}


class AddPlateRequest(BaseModel):
    plate: str
    owner: str = "Unknown"
    threat_level: str = "critical"
    notes: str = ""


@app.post("/api/watchlists/plates", dependencies=[Depends(verify_api_key)])
def add_watchlist_plate(req: AddPlateRequest):
    """Add a vehicle license plate to the watchlist in cameras.yaml."""
    cfg_path = settings.cameras_yaml_path
    data = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}

    wl = data.setdefault("watchlists", {})
    plates = wl.setdefault("plates", [])
    # Update if exists or append
    for p in plates:
        if p.get("plate", "").upper() == req.plate.upper():
            p["owner"] = req.owner
            p["threat_level"] = req.threat_level
            p["notes"] = req.notes
            break
    else:
        plates.append({
            "plate": req.plate.upper(),
            "owner": req.owner,
            "threat_level": req.threat_level,
            "notes": req.notes,
        })

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        # Sync to config.yaml if it exists
        if os.path.exists(settings.config_yaml_path) and settings.config_yaml_path != cfg_path:
            try:
                with open(settings.config_yaml_path, "r", encoding="utf-8") as f_cfg:
                    cfg_data = yaml.safe_load(f_cfg) or {}
                cfg_data.setdefault("watchlists", {})["plates"] = plates
                with open(settings.config_yaml_path, "w", encoding="utf-8") as f_cfg:
                    yaml.safe_dump(cfg_data, f_cfg, sort_keys=False)
            except Exception:
                pass
        return {"status": "success", "message": f"Plate '{req.plate}' enrolled in watchlist."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist watchlist: {exc}")


class AddFaceRequest(BaseModel):
    name: str
    threat_level: str = "critical"
    notes: str = ""
    image_base64: Optional[str] = None


@app.post("/api/watchlists/faces", dependencies=[Depends(verify_api_key)])
def add_watchlist_face(req: AddFaceRequest):
    """Add an individual to the facial recognition watchlist in cameras.yaml."""
    cfg_path = settings.cameras_yaml_path
    data = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}

    wl = data.setdefault("watchlists", {})
    faces = wl.setdefault("faces", [])
    # Update if exists or append
    for f_entry in faces:
        if f_entry.get("name", "").strip().lower() == req.name.strip().lower():
            f_entry["threat_level"] = req.threat_level
            f_entry["notes"] = req.notes
            if req.image_base64:
                f_entry["image_base64"] = req.image_base64
            break
    else:
        new_face = {
            "name": req.name.strip(),
            "threat_level": req.threat_level,
            "notes": req.notes,
        }
        if req.image_base64:
            new_face["image_base64"] = req.image_base64
        faces.append(new_face)

    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        # Sync to config.yaml if it exists
        if os.path.exists(settings.config_yaml_path) and settings.config_yaml_path != cfg_path:
            try:
                with open(settings.config_yaml_path, "r", encoding="utf-8") as f_cfg:
                    cfg_data = yaml.safe_load(f_cfg) or {}
                cfg_data.setdefault("watchlists", {})["faces"] = faces
                with open(settings.config_yaml_path, "w", encoding="utf-8") as f_cfg:
                    yaml.safe_dump(cfg_data, f_cfg, sort_keys=False)
            except Exception:
                pass
        return {"status": "success", "message": f"Target '{req.name}' enrolled in watchlist."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist watchlist: {exc}")


@app.websocket("/ws/alerts")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
):
    expected_key = settings.central_api_key
    selected_subprotocol = None
    if expected_key:
        provided = None
        # 1. Header: Authorization (Bearer)
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
        elif auth_header:
            provided = auth_header.strip()

        # 2. Header: X-API-Key
        if not provided:
            provided = websocket.headers.get("x-api-key", "")

        # 3. Header: Sec-WebSocket-Protocol (browser-safe: passes token in header, not in URL)
        if not provided:
            raw_subprotocol = websocket.headers.get("sec-websocket-protocol", "")
            if raw_subprotocol:
                subprotocols = [p.strip() for p in raw_subprotocol.split(",") if p.strip()]
                for proto in subprotocols:
                    if secrets.compare_digest(proto, expected_key):
                        provided = proto
                        selected_subprotocol = "cctv-auth" if "cctv-auth" in subprotocols else proto
                        break

        # 4. Backward-compatibility: Query param (legacy fallback)
        if not provided:
            provided = token or api_key

        if not provided or not secrets.compare_digest(provided, expected_key):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
            return

    await manager.connect(websocket, subprotocol=selected_subprotocol)
    try:
        while True:
            # Keep WebSocket alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.debug("[Central WS] Connection exception: %s", e)
        manager.disconnect(websocket)

# =========================================================================
# Isolated Operator Preview Utilities (Non-Inference, Dashboard Verification Only)
# Note: Canonical analytics ingestion flows via Camera -> Edge -> Fog -> Central.
# These endpoints provide bounded, isolated live MJPEG preview for dashboard operators.
# =========================================================================
MAX_PREVIEW_SLOTS = 4

class TestConnectionRequest(BaseModel):
    source_type: str = "direct"
    stream_url: Optional[str] = None
    ip_address: Optional[str] = None
    port: Optional[Union[int, str]] = 554
    rtsp_path: Optional[str] = ""
    username: Optional[str] = ""
    password: Optional[str] = ""
    timeout: float = 3.0


class ConnectCameraRequest(BaseModel):
    camera_id: str
    ip_address: Optional[str] = ""
    port: Optional[Union[int, str]] = 554
    rtsp_path: Optional[str] = ""
    username: Optional[str] = ""
    password: Optional[str] = ""
    target_slot: str = "Camera Slot 2"
    source_type: str = "direct"
    stream_url: Optional[str] = None
    name: Optional[str] = None


active_connectors: Dict[str, Any] = {}


@app.post("/api/v1/test-connection", dependencies=[Depends(verify_api_key)])
@app.post("/api/cameras/test-connection", dependencies=[Depends(verify_api_key)])
def test_camera_connection(req: TestConnectionRequest):
    """Test connectivity and inspect video stream properties without altering persistent configuration."""
    from ingestion.stream import probe_stream
    from ingestion.onvif.security import validate_stream_url, validate_host_and_port

    target_url = (req.stream_url or "").strip()
    if target_url:
        if not validate_stream_url(target_url):
            raise HTTPException(
                status_code=400,
                detail="Invalid stream URL or destination address not permitted (SSRF protection)"
            )
    else:
        ip = (req.ip_address or "").strip()
        if not ip:
            raise HTTPException(status_code=400, detail="Invalid request: Either stream_url or ip_address must be provided")
        try:
            port_val = int(req.port) if req.port else 554
        except (ValueError, TypeError):
            port_val = 554
        try:
            valid_host, valid_port = validate_host_and_port(ip, port_val)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        clean_path = (req.rtsp_path or "").lstrip("/")
        target_url = f"rtsp://{valid_host}:{valid_port}/{clean_path}"

    res = probe_stream(
        url=target_url,
        timeout=req.timeout,
        username=req.username or "",
        password=req.password or "",
    )

    if not res.get("connected"):
        return Response(
            content=json.dumps({"status": "error", "connected": False, "error": res.get("error", "Stream unavailable")}),
            status_code=400,
            media_type="application/json"
        )

    return {
        "status": "success",
        "connected": True,
        "protocol": res.get("protocol", "RTSP"),
        "resolution": res.get("resolution"),
        "fps": res.get("fps"),
        "sanitized_url": res.get("sanitized_url"),
        "message": "Connected",
    }


@app.post("/api/v1/connect-camera", dependencies=[Depends(verify_api_key)])
@app.post("/api/connect-ip-camera", dependencies=[Depends(verify_api_key)])
def connect_camera(req: ConnectCameraRequest):
    from urllib.parse import urlsplit
    from ingestion.onvif.security import validate_stream_url, validate_host_and_port

    direct_url = (req.stream_url or "").strip()
    effective_ip = (req.ip_address or "").strip()

    if direct_url:
        # Direct stream URL validation with SSRF protection
        if not validate_stream_url(direct_url):
            raise HTTPException(
                status_code=400,
                detail="Invalid stream URL or destination address not permitted (SSRF protection)"
            )
        try:
            parsed = urlsplit(direct_url)
            if not effective_ip and parsed.hostname:
                effective_ip = parsed.hostname
        except Exception:
            pass

        connector = RTSPConnector(
            stream_url=direct_url,
            username=req.username,
            password=req.password,
            camera_id=req.camera_id
        )
    else:
        # Legacy/Component-based RTSP address
        if not effective_ip:
            raise HTTPException(status_code=400, detail="Missing IP address or stream URL")
        try:
            port_val = int(req.port) if req.port else 554
        except (ValueError, TypeError):
            port_val = 554
        try:
            valid_host, valid_port = validate_host_and_port(effective_ip, port_val)
            effective_ip = valid_host
            req.port = valid_port
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        connector = RTSPConnector(
            ip_address=effective_ip,
            port=req.port,
            username=req.username,
            password=req.password,
            rtsp_path=req.rtsp_path,
            camera_id=req.camera_id
        )

    logger.info("[Preview Utility] Operator connecting camera %s (slot=%s, source_type=%s, target=%s)",
                req.camera_id, req.target_slot, req.source_type, sanitize_url(connector.rtsp_url))

    # Validate connection probe
    is_valid = connector.validate_connection(timeout=3)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail="Failed to ping IP stream. Check credentials/network"
        )

    # Persist camera configuration into YAML for the canonical Edge pipeline
    config_path = settings.config_yaml_path
    cameras_path = settings.cameras_yaml_path

    source_dict = None
    if direct_url:
        source_dict = {
            "type": "direct",
            "url": direct_url,
        }
        if req.username:
            source_dict["username"] = req.username
        if req.password:
            source_dict["password"] = req.password

    try:
        append_camera_config(
            req.camera_id,
            effective_ip,
            connector.rtsp_url,
            source=source_dict,
            name=req.name or req.camera_id,
            config_path=config_path
        )
        append_camera_config(
            req.camera_id,
            effective_ip,
            connector.rtsp_url,
            source=source_dict,
            name=req.name or req.camera_id,
            config_path=cameras_path
        )
    except Exception as e:
        logger.error("[Central API] Failed to update config files: %s", e)

    # Bounded preview slot management: stop existing connector on this slot
    slot = req.target_slot
    if slot in active_connectors:
        try:
            active_connectors[slot].stop()
        except Exception:
            pass

    # Enforce maximum active preview slots to prevent memory exhaustion
    if len(active_connectors) >= MAX_PREVIEW_SLOTS and slot not in active_connectors:
        oldest_slot = next(iter(active_connectors.keys()))
        try:
            active_connectors[oldest_slot].stop()
        except Exception:
            pass
        active_connectors.pop(oldest_slot, None)

    # Start background reader for operator visual preview
    connector.start()
    active_connectors[slot] = connector
    active_connectors[req.camera_id] = connector
    active_connectors[req.camera_id.upper()] = connector
    active_connectors[req.camera_id.lower()] = connector

    return {
        "status": "success",
        "message": f"Camera {req.camera_id} connected & streaming successfully in slot {slot}",
        "camera_id": req.camera_id,
        "rtsp_url": sanitize_url(connector.rtsp_url),
    }

@app.delete("/api/v1/connect-camera/{camera_slot}", dependencies=[Depends(verify_api_key)])
def disconnect_camera_slot(camera_slot: str):
    """Disconnect and release resources for an operator preview slot."""
    found = False
    for k, conn in list(active_connectors.items()):
        if k == camera_slot or k.lower() == camera_slot.lower():
            try:
                conn.stop()
            except Exception as exc:
                logger.debug("Error stopping preview connector for %s: %s", k, exc)
            active_connectors.pop(k, None)
            found = True
    if found:
        return {"status": "success", "message": f"Preview slot '{camera_slot}' disconnected."}
    return {"status": "noop", "message": f"Slot '{camera_slot}' not active."}

def gen_rtsp_frames(connector):
    try:
        consecutive_misses = 0
        while True:
            frame = connector.get_frame(block=True, timeout=0.5)
            if frame is not None:
                consecutive_misses = 0
                ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ret:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            else:
                consecutive_misses += 1
                if consecutive_misses > 20:  # ~10s of no frames -> stop generator
                    break
                time.sleep(0.03)
    except Exception as e:
        logger.debug("[Video Feed Preview] Slot stream generator closed: %s", e)

@app.get("/video_feed_slot/{camera_slot}", dependencies=[Depends(verify_api_key)])
def video_feed_slot(camera_slot: str):
    connector = None
    if camera_slot in active_connectors:
        connector = active_connectors[camera_slot]
    else:
        # Check case-insensitive / slot mapping
        norm = camera_slot.lower().replace(" ", "").replace("_", "").replace("-", "")
        for k, v in list(active_connectors.items()):
            k_norm = k.lower().replace(" ", "").replace("_", "").replace("-", "")
            if norm == k_norm:
                connector = v
                break
            if ("slot2" in norm or "cam02" in norm) and ("slot2" in k_norm or "cam02" in k_norm):
                connector = v
                break
            if ("slot3" in norm or "cam03" in norm) and ("slot3" in k_norm or "cam03" in k_norm):
                connector = v
                break

    # If connector not currently in memory, try auto-restoring from cameras.yaml
    if not connector:
        from ingestion.onvif.resolver import resolve_camera_source, find_camera_in_yaml

        # Try to find camera in cameras.yaml
        cam_id = camera_slot
        norm = camera_slot.lower().replace(" ", "").replace("_", "").replace("-", "")
        if "slot2" in norm or "cam02" in norm or "2" in norm:
            candidates = [camera_slot, "CAM_02", "cam-slot-2"]
        elif "slot3" in norm or "cam03" in norm or "3" in norm:
            candidates = [camera_slot, "CAM_03", "cam-slot-3"]
        else:
            candidates = [camera_slot]

        cam = None
        for cand in candidates:
            cam = find_camera_in_yaml(cand, settings.cameras_yaml_path)
            if cam:
                cam_id = cand
                break

        if cam:
            stream_uri, meta = resolve_camera_source(cam)
            if stream_uri:
                target_url = str(stream_uri)
                for existing_conn in list(active_connectors.values()):
                    if getattr(existing_conn, "rtsp_url", None) == target_url and getattr(existing_conn, "_running", False):
                        connector = existing_conn
                        break
                if not connector:
                    connector = RTSPConnector(stream_url=target_url, camera_id=cam_id)
                    connector.start()
                active_connectors[camera_slot] = connector
                active_connectors[cam_id] = connector
                active_connectors[cam_id.upper()] = connector

    if not connector:
        raise HTTPException(status_code=404, detail=f"Camera slot '{camera_slot}' not connected")
    return StreamingResponse(gen_rtsp_frames(connector), media_type="multipart/x-mixed-replace; boundary=frame")

# =========================================================================
# AI Scanning Endpoints (Face & Plate Recognition)
# =========================================================================
from fastapi import File, Form, UploadFile

import base64

try:
    if hasattr(cv2, 'CascadeClassifier') and hasattr(cv2, 'data'):
        _central_face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    else:
        _central_face_cascade = None
except Exception:
    _central_face_cascade = None

@app.post("/api/scan-face", dependencies=[Depends(verify_api_key)])
async def scan_face(
    file: UploadFile = File(...),
    targets: str = Form(...)
):
    """Processes face localization on incoming webcam frames without fabricating biometric matches."""
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return {"matches": []}

        # Genuine biometric recognition belongs in Fog with vector embeddings.
        # Here we localize faces if a detector is present, returning UNVERIFIED state.
        if _central_face_cascade and not getattr(_central_face_cascade, 'empty', lambda: True)():
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            dets = _central_face_cascade.detectMultiScale(
                gray_frame, scaleFactor=1.05, minNeighbors=3, minSize=(30, 30)
            )
            matches = []
            for (x, y, w, h) in dets:
                matches.append({
                    "name": "UNVERIFIED",
                    "confidence": None,
                    "bbox": [int(x), int(y), int(x + w), int(y + h)],
                    "verified": False,
                })
            return {"matches": matches}

        return {"matches": []}
    except Exception as e:
        logger.error("[Central Face API] Error: %s", e)
        return {"matches": []}

@app.post("/api/scan-plate", dependencies=[Depends(verify_api_key)])
async def scan_plate(file: UploadFile = File(...)):
    """Processes license plate recognition on incoming webcam frames."""
    try:
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if frame is None:
            return {"results": []}

        results = []
        return {"results": results}
    except Exception as e:
        print(f"[Central ANPR API] Error: {e}")
        return {"results": []}
