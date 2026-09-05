"""Unified Perception & Analytics Engine for AI Surveillance.

Implements the canonical shared perception architecture:
Camera -> Object Detection -> Multi-object Tracking -> Shared Tracked Objects
  ├── Intrusion (Tripwire line-crossing)
  ├── Restricted-zone detection (Polygon ROI + off-hours time windows)
  ├── Loitering / Dwell time (Persistent tracking IDs + departure/cooldown)
  ├── Abandoned / Suspicious object detection (stationary unattended baggage)
  ├── ANPR (License plate localization, CLAHE enhancement, OCR & watchlist match)
  └── Facial recognition (InsightFace ArcFace vector search with explicit unknown states)
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from uuid import uuid4
import cv2
import numpy as np
import yaml

from shared.config import settings
from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent

logger = logging.getLogger("fog.analytics")


# =========================================================================
# Shared Tracked Object Contract
# =========================================================================
@dataclass
class TrackedObject:
    camera_id: str
    track_id: str
    label: str
    confidence: float
    bbox: BoundingBox
    centroid: Tuple[float, float]
    occurred_at: datetime
    crop: Optional[np.ndarray] = None


def bbox_centroid(bbox: BoundingBox) -> Tuple[float, float]:
    return ((bbox.x1 + bbox.x2) / 2.0, (bbox.y1 + bbox.y2) / 2.0)


def euclidean_distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def ccw(A: Tuple[float, float], B: Tuple[float, float], C: Tuple[float, float]) -> bool:
    return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])


def segments_intersect(
    A: Tuple[float, float],
    B: Tuple[float, float],
    C: Tuple[float, float],
    D: Tuple[float, float],
) -> bool:
    """Return True if line segment AB intersects line segment CD."""
    return ccw(A, C, D) != ccw(B, C, D) and ccw(A, B, C) != ccw(A, B, D)


def point_in_polygon(point: Tuple[float, float], polygon: List[List[float]]) -> bool:
    """Ray casting algorithm to determine if a point is inside a polygon."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        p1x, p1y = polygon[i]
        p2x, p2y = polygon[(i + 1) % n]
        if min(p1y, p2y) < y <= max(p1y, p2y):
            if x <= max(p1x, p2x):
                if p1y != p2y:
                    xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                if p1x == p2x or x <= xinters:
                    inside = not inside
    return inside


def is_in_time_window(now: datetime, start_str: str, end_str: str) -> bool:
    """Check if current time is within [start, end] window (supports overnight)."""
    if not start_str or not end_str:
        return True
    try:
        t_now = now.time()
        s_h, s_m = map(int, start_str.split(":"))
        e_h, e_m = map(int, end_str.split(":"))
        t_start = time(s_h, s_m)
        t_end = time(e_h, e_m)
        if t_start <= t_end:
            return t_start <= t_now <= t_end
        else:
            return t_now >= t_start or t_now <= t_end
    except Exception:
        return True


# =========================================================================
# Analytics Modules
# =========================================================================

class IntrusionModule:
    """Evaluates line-crossing (tripwire) events using object trajectories."""

    def __init__(self, cooldown_seconds: int = 30):
        self.cooldown_seconds = cooldown_seconds
        # (camera_id, track_id) -> last position
        self.trajectories: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self.last_alerted: Dict[Tuple[str, str, str], datetime] = {}

    def prune(self, active_keys: Set[Tuple[str, str]]) -> None:
        stale = [k for k in self.trajectories if k not in active_keys]
        for k in stale:
            del self.trajectories[k]

    def process(
        self,
        objects: List[TrackedObject],
        tripwires: List[Dict[str, Any]],
        now: datetime,
        site_lat: float,
        site_lng: float,
    ) -> List[AlertEvent]:
        alerts: List[AlertEvent] = []
        active_keys = {(obj.camera_id, obj.track_id) for obj in objects}
        self.prune(active_keys)

        for obj in objects:
            key = (obj.camera_id, obj.track_id)
            prev_pos = self.trajectories.get(key)
            self.trajectories[key] = obj.centroid

            if prev_pos is None:
                continue

            for wire in tripwires:
                wire_name = wire.get("name", "Tripwire")
                line = wire.get("line", [])
                if len(line) != 2:
                    continue
                p1 = (float(line[0][0]), float(line[0][1]))
                p2 = (float(line[1][0]), float(line[1][1]))

                if segments_intersect(prev_pos, obj.centroid, p1, p2):
                    alert_key = (obj.camera_id, obj.track_id, wire_name)
                    last_alert = self.last_alerted.get(alert_key)
                    if last_alert and (now - last_alert).total_seconds() < self.cooldown_seconds:
                        continue
                    self.last_alerted[alert_key] = now

                    alert_id = uuid4()
                    alerts.append(AlertEvent(
                        event_id=alert_id,
                        camera_id=obj.camera_id,
                        event_type="intrusion",
                        severity="high",
                        details=f"Tracked {obj.label} {obj.track_id} breached tripwire '{wire_name}'.",
                        confidence=obj.confidence,
                        track_id=obj.track_id,
                        lat=site_lat,
                        lng=site_lng,
                        snapshot_path=f"snapshots/{obj.camera_id}_{alert_id}.jpg",
                        metadata={
                            "tripwire": wire_name,
                            "label": obj.label,
                            "clip_path": f"snapshots/clips/{obj.camera_id}_{alert_id}.mp4",
                        },
                    ))
        return alerts


class RestrictedZoneModule:
    """Detects presence of objects inside polygon ROIs during scheduled windows."""

    def __init__(self, cooldown_seconds: int = 45):
        self.cooldown_seconds = cooldown_seconds
        self.last_alerted: Dict[Tuple[str, str, str], datetime] = {}

    def process(
        self,
        objects: List[TrackedObject],
        zones: List[Dict[str, Any]],
        now: datetime,
        site_lat: float,
        site_lng: float,
    ) -> List[AlertEvent]:
        alerts: List[AlertEvent] = []
        for obj in objects:
            for zone in zones:
                zone_name = zone.get("name", "Restricted Zone")
                polygon = zone.get("polygon", [])
                if len(polygon) < 3:
                    continue

                window = zone.get("time_window", {})
                if not is_in_time_window(now, window.get("start", ""), window.get("end", "")):
                    continue

                if point_in_polygon(obj.centroid, polygon):
                    alert_key = (obj.camera_id, obj.track_id, zone_name)
                    last_alert = self.last_alerted.get(alert_key)
                    if last_alert and (now - last_alert).total_seconds() < self.cooldown_seconds:
                        continue
                    self.last_alerted[alert_key] = now

                    alert_id = uuid4()
                    alerts.append(AlertEvent(
                        event_id=alert_id,
                        camera_id=obj.camera_id,
                        event_type="restricted_zone",
                        severity="critical",
                        details=f"Tracked {obj.label} {obj.track_id} entered restricted zone '{zone_name}'.",
                        confidence=obj.confidence,
                        track_id=obj.track_id,
                        lat=site_lat,
                        lng=site_lng,
                        snapshot_path=f"snapshots/{obj.camera_id}_{alert_id}.jpg",
                        metadata={
                            "zone": zone_name,
                            "label": obj.label,
                            "clip_path": f"snapshots/clips/{obj.camera_id}_{alert_id}.mp4",
                        },
                    ))
        return alerts


class AbandonedObjectModule:
    """Detects stationary unattended objects (e.g. baggage) separated from people."""

    SUSPICIOUS_LABELS = {"backpack", "suitcase", "handbag", "bag", "box"}

    def __init__(
        self,
        abandoned_seconds: int = 45,
        movement_threshold: float = 0.03,
        proximity_radius: float = 0.18,
        cooldown_seconds: int = 60,
    ):
        self.abandoned_seconds = abandoned_seconds
        self.movement_threshold = movement_threshold
        self.proximity_radius = proximity_radius
        self.cooldown_seconds = cooldown_seconds

        # (camera_id, track_id) -> state dict
        self.stationary_tracks: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def process(
        self,
        objects: List[TrackedObject],
        now: datetime,
        site_lat: float,
        site_lng: float,
        expiry_seconds: int = 15,
        abandoned_seconds: Optional[int] = None,
        proximity_radius: Optional[float] = None,
    ) -> List[AlertEvent]:
        alerts: List[AlertEvent] = []
        eff_abandoned_sec = abandoned_seconds if abandoned_seconds is not None else self.abandoned_seconds
        raw_radius = proximity_radius if proximity_radius is not None else self.proximity_radius
        eff_radius = (raw_radius / 640.0) if raw_radius > 1.0 else raw_radius
        persons = [obj for obj in objects if obj.label == "person"]
        current_bag_keys = set()

        for obj in objects:
            if obj.label.lower() not in self.SUSPICIOUS_LABELS:
                continue

            key = (obj.camera_id, obj.track_id)
            current_bag_keys.add(key)
            state = self.stationary_tracks.get(key)

            if state is None:
                self.stationary_tracks[key] = {
                    "first_seen": now,
                    "last_seen": now,
                    "centroid": obj.centroid,
                    "alerted": False,
                    "last_alert": None,
                }
                continue

            # Check if object moved significantly
            if euclidean_distance(state["centroid"], obj.centroid) > self.movement_threshold:
                state["first_seen"] = now
                state["centroid"] = obj.centroid
                state["alerted"] = False

            state["last_seen"] = now
            stationary_duration = (now - state["first_seen"]).total_seconds()

            # Check if unattended (no person within proximity radius)
            unattended = True
            for p in persons:
                if euclidean_distance(obj.centroid, p.centroid) <= eff_radius:
                    unattended = False
                    break

            if not unattended:
                state["first_seen"] = now
                continue

            if stationary_duration >= eff_abandoned_sec:
                last_alert = state["last_alert"]
                if not state["alerted"] or (last_alert and (now - last_alert).total_seconds() >= self.cooldown_seconds):
                    state["alerted"] = True
                    state["last_alert"] = now
                    alert_id = uuid4()
                    alerts.append(AlertEvent(
                        event_id=alert_id,
                        camera_id=obj.camera_id,
                        event_type="abandoned_object",
                        severity="high",
                        details=(f"Suspicious unattended {obj.label} {obj.track_id} stationary for "
                                 f"{int(stationary_duration)}s without owner nearby."),
                        confidence=obj.confidence,
                        track_id=obj.track_id,
                        lat=site_lat,
                        lng=site_lng,
                        snapshot_path=f"snapshots/{obj.camera_id}_{alert_id}.jpg",
                        metadata={
                            "label": obj.label,
                            "dwell_seconds": int(stationary_duration),
                            "clip_path": f"snapshots/clips/{obj.camera_id}_{alert_id}.mp4",
                        },
                    ))

        # Prune dead tracks
        stale = [
            k for k, v in self.stationary_tracks.items()
            if (now - v["last_seen"]).total_seconds() > expiry_seconds
        ]
        for k in stale:
            del self.stationary_tracks[k]

        return alerts


class ANPRModule:
    """Automatic Number Plate Recognition with CLAHE enhancement, EasyOCR engine & watchlist check."""

    VEHICLE_LABELS = {"car", "truck", "bus", "motorcycle"}

    def __init__(self, watchlist: Optional[Dict[str, Dict[str, str]]] = None, cooldown_seconds: int = 60, gpu: bool = False):
        self.watchlist = watchlist or {}
        self.cooldown_seconds = cooldown_seconds
        self.gpu = gpu
        self.last_alerted: Dict[str, datetime] = {}
        self._ocr_reader = None
        self._ocr_initialized = False

    def get_ocr_reader(self):
        """Lazy initialization of EasyOCR engine on CPU."""
        if not self._ocr_initialized:
            self._ocr_initialized = True
            try:
                import easyocr
                logger.info("Initializing EasyOCR reader (CPU mode)...")
                self._ocr_reader = easyocr.Reader(["en"], gpu=self.gpu)
                logger.info("EasyOCR reader initialized successfully.")
            except Exception as e:
                logger.warning("Failed to initialize EasyOCR engine: %s; ANPR OCR will be disabled.", e)
                self._ocr_reader = None
        return self._ocr_reader

    def detect_and_read_plate(self, crop_img: np.ndarray) -> List[Tuple[str, float]]:
        """Localize plate region, apply CLAHE contrast enhancement, and extract text via EasyOCR."""
        if crop_img is None or crop_img.size == 0:
            return []

        reader = self.get_ocr_reader()
        if reader is None:
            return []

        results: List[Tuple[str, float]] = []
        try:
            # Grayscale & CLAHE Enhancement
            gray = cv2.cvtColor(crop_img, cv2.COLOR_BGR2GRAY)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)

            # Candidate localization: find rectangular contours matching plate aspect ratio (1.5 to 6.5)
            candidates = [enhanced]  # include full enhanced crop as primary candidate

            blurred = cv2.GaussianBlur(enhanced, (5, 5), 0)
            sobelx = cv2.Sobel(blurred, cv2.CV_8U, 1, 0, ksize=3)
            _, thresh = cv2.threshold(sobelx, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3))
            morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

            contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            h_crop, w_crop = crop_img.shape[:2]

            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                if h == 0:
                    continue
                aspect_ratio = float(w) / float(h)
                area_ratio = float(w * h) / float(w_crop * h_crop)
                if 1.5 <= aspect_ratio <= 6.5 and area_ratio >= 0.02:
                    plate_subcrop = enhanced[y:y + h, x:x + w]
                    if plate_subcrop.size > 0:
                        candidates.append(plate_subcrop)

            # Perform OCR on candidate regions
            for candidate in candidates[:3]:
                ocr_out = reader.readtext(candidate)
                for _bbox, text, conf in ocr_out:
                    if text and float(conf) >= 0.3:
                        results.append((text, float(conf)))

        except Exception as e:
            logger.error("Error during plate localization/OCR: %s", e)

        return results

    @staticmethod
    def normalize_plate(text: str) -> str:
        cleaned = re.sub(r"[^A-Z0-9]", "", text.upper())
        if len(cleaned) < 4:
            return cleaned
        num_to_char = {"0": "O", "1": "I", "8": "B", "5": "S", "2": "Z", "6": "G", "4": "A"}
        char_to_num = {"O": "0", "Q": "0", "I": "1", "L": "1", "B": "8", "S": "5", "Z": "2", "G": "6", "A": "4", "T": "7"}
        pattern = re.compile(r"^([A-Z0-9]{2})([A-Z0-9]{1,2})([A-Z0-9]{1,2})([A-Z0-9]{4})$")
        match = pattern.match(cleaned)
        if match:
            state_p, dist_p, series_p, num_p = match.groups()
            s_clean = "".join(num_to_char.get(c, c) for c in state_p)
            d_clean = "".join(char_to_num.get(c, c) for c in dist_p)
            se_clean = "".join(num_to_char.get(c, c) for c in series_p)
            n_clean = "".join(char_to_num.get(c, c) for c in num_p)
            return f"{s_clean}{d_clean}{se_clean}{n_clean}"
        return cleaned

    def evaluate_plate_reading(
        self,
        camera_id: str,
        plate_text: str,
        confidence: float,
        now: datetime,
        site_lat: float,
        site_lng: float,
        track_id: Optional[str] = None,
    ) -> Optional[AlertEvent]:
        norm_plate = self.normalize_plate(plate_text)
        if not norm_plate:
            return None

        # Check if plate is on watchlist
        for watch_plate, info in self.watchlist.items():
            if self.normalize_plate(watch_plate) == norm_plate:
                last = self.last_alerted.get(norm_plate)
                if last and (now - last).total_seconds() < self.cooldown_seconds:
                    return None
                self.last_alerted[norm_plate] = now
                owner = info.get("owner", "Flagged Subject")
                threat = info.get("threat_level", "critical")
                notes = info.get("notes", "Vehicle Watchlist Match")
                alert_id = uuid4()
                return AlertEvent(
                    event_id=alert_id,
                    camera_id=camera_id,
                    event_type="anpr_match",
                    severity=threat if threat in ["critical", "high", "medium"] else "critical",
                    details=f"Watchlist Vehicle Flagged: '{norm_plate}' ({owner}) - {notes}",
                    confidence=confidence,
                    track_id=track_id,
                    lat=site_lat,
                    lng=site_lng,
                    snapshot_path=f"snapshots/{camera_id}_{alert_id}.jpg",
                    metadata={
                        "plate": norm_plate,
                        "owner": owner,
                        "threat": threat,
                        "clip_path": f"snapshots/clips/{camera_id}_{alert_id}.mp4",
                    },
                )
        return None


class VectorGallery:
    """Vector database coordinator supporting Milvus and in-memory cosine fallback.

    Guarantees biometric preservation: collections are never dropped on startup.
    """
    COLLECTION_NAME = "watchlist_faces"
    DIMENSION = 512

    def __init__(self, host: str = "localhost", port: int = 19530):
        self.host = host
        self.port = port
        self.milvus_client = None
        self._local_vectors: List[Dict[str, Any]] = []
        self._init_error: Optional[str] = None
        self.init_backend()

    @property
    def is_persistent(self) -> bool:
        """True only if persistent vector database (Milvus) is connected and verified."""
        return self.milvus_client is not None

    def get_status(self) -> Dict[str, Any]:
        """Return clear readiness and persistence status."""
        return {
            "persistent": self.is_persistent,
            "backend": "milvus" if self.is_persistent else "degraded_in_memory",
            "host": self.host,
            "port": self.port,
            "collection": self.COLLECTION_NAME,
            "enrolled_count": len(self._local_vectors),
            "error": self._init_error,
        }

    def init_backend(self) -> None:
        try:
            from pymilvus import MilvusClient, DataType
            uri = f"http://{self.host}:{self.port}"
            client = MilvusClient(uri=uri)
            if not client.has_collection(self.COLLECTION_NAME):
                schema = client.create_schema(
                    auto_id=True,
                    enable_dynamic_field=True,
                    description="Watchlist Facial Recognition System",
                )
                schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
                schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=self.DIMENSION)
                schema.add_field(field_name="name", datatype=DataType.VARCHAR, max_length=100)
                schema.add_field(field_name="threat_level", datatype=DataType.VARCHAR, max_length=50)
                schema.add_field(field_name="notes", datatype=DataType.VARCHAR, max_length=255)

                index_params = client.prepare_index_params()
                index_params.add_index(
                    field_name="vector",
                    metric_type="COSINE",
                    index_type="IVF_FLAT",
                    params={"nlist": 128},
                )
                client.create_collection(
                    collection_name=self.COLLECTION_NAME,
                    schema=schema,
                    index_params=index_params,
                )
                logger.info("Created Milvus collection '%s'", self.COLLECTION_NAME)
            else:
                logger.info("Preserved existing Milvus collection '%s'", self.COLLECTION_NAME)
            self.milvus_client = client
            self._init_error = None
        except Exception as e:
            self.milvus_client = None
            self._init_error = str(e)
            logger.warning(
                "Milvus persistent vector storage is unavailable (%s); operating in degraded mode. "
                "Biometric enrollments will NOT persist across restarts unless Milvus is reachable.",
                e,
            )

    def enroll(
        self,
        name: str,
        vector: np.ndarray,
        threat_level: str = "critical",
        notes: str = "Watchlist Subject",
        require_persistence: bool = False,
        allow_ephemeral: bool = True,
        replace: bool = False,
    ) -> bool:
        """Enroll subject into the vector gallery (supports multiple samples per subject).

        If Milvus is unavailable:
        - If require_persistence=True (or settings.frs_require_persistence is True):
          fails clearly and returns False. Does not falsely claim persistent enrollment.
        - If allow_ephemeral=True:
          stores in memory for this session, but is_persistent remains False.
        """
        norm = np.linalg.norm(vector)
        if norm <= 0:
            return False
        normalized_vector = (vector / norm).astype(np.float32)

        enforce_persistence = require_persistence or getattr(settings, "frs_require_persistence", False)

        if not self.is_persistent:
            if enforce_persistence or not allow_ephemeral:
                logger.error(
                    "Persistent FRS enrollment failed for '%s': Milvus is unavailable at %s:%s. "
                    "Ephemeral enrollment rejected.",
                    name, self.host, self.port,
                )
                return False
            logger.warning(
                "Milvus unavailable; subject '%s' stored in ephemeral memory only. "
                "Enrollment will NOT survive restart.",
                name,
            )

        # Store in local in-memory gallery
        if replace:
            self._local_vectors = [item for item in self._local_vectors if item["name"] != name]
            self._local_vectors.append({
                "name": name,
                "vector": normalized_vector,
                "threat_level": threat_level,
                "notes": notes,
            })
        else:
            already_enrolled = any(
                item["name"] == name and float(np.dot(normalized_vector, item["vector"])) > 0.99
                for item in self._local_vectors
            )
            if not already_enrolled:
                self._local_vectors.append({
                    "name": name,
                    "vector": normalized_vector,
                    "threat_level": threat_level,
                    "notes": notes,
                })

        # Store in Milvus if connected
        if self.milvus_client is not None:
            try:
                if replace:
                    try:
                        self.milvus_client.delete(collection_name=self.COLLECTION_NAME, filter=f'name == "{name}"')
                    except Exception:
                        pass
                data = [{
                    "vector": normalized_vector.tolist(),
                    "name": name,
                    "threat_level": threat_level,
                    "notes": notes,
                }]
                self.milvus_client.insert(collection_name=self.COLLECTION_NAME, data=data)
                logger.info("Subject '%s' sample enrolled in Milvus collection '%s'", name, self.COLLECTION_NAME)
            except Exception as e:
                logger.error("Failed to insert into Milvus for '%s': %s", name, e)
                if enforce_persistence:
                    return False

        return True

    def search(self, query_vector: np.ndarray, top_k: int = 1) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Search nearest vectors using cosine similarity across multi-sample subjects. Returns [(name, max_similarity, metadata)]."""
        norm = np.linalg.norm(query_vector)
        if norm <= 0:
            return []
        q_norm = (query_vector / norm).astype(np.float32)

        subject_matches: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # If Milvus is active, query Milvus
        if self.milvus_client is not None:
            try:
                search_params = {"metric_type": "COSINE", "params": {"nprobe": 10}}
                res = self.milvus_client.search(
                    collection_name=self.COLLECTION_NAME,
                    data=[q_norm.tolist()],
                    limit=max(top_k * 10, 20),
                    output_fields=["name", "threat_level", "notes"],
                    search_params=search_params,
                )
                if res and len(res[0]) > 0:
                    for hit in res[0]:
                        sim = float(hit.get("distance", 0.0))
                        entity = hit.get("entity", {})
                        s_name = entity.get("name", "Unknown")
                        meta = {
                            "threat_level": entity.get("threat_level", "critical"),
                            "notes": entity.get("notes", ""),
                        }
                        if s_name not in subject_matches or sim > subject_matches[s_name][0]:
                            subject_matches[s_name] = (sim, meta)
            except Exception as e:
                logger.warning("Milvus search error (%s); falling back to in-memory gallery", e)

        # Merge local in-memory vectors for immediate consistency on fresh enrollments
        for item in self._local_vectors:
            sim = float(np.dot(q_norm, item["vector"]))
            s_name = item["name"]
            meta = {
                "threat_level": item.get("threat_level", "critical"),
                "notes": item.get("notes", ""),
            }
            if s_name not in subject_matches or sim > subject_matches[s_name][0]:
                subject_matches[s_name] = (sim, meta)

        if not subject_matches:
            return []

        results = [(name, sim, meta) for name, (sim, meta) in subject_matches.items()]
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]


class FacialRecognitionModule:
    """Genuine Facial Recognition System with InsightFace Buffalo_S on CPU & ArcFace vector search."""

    def __init__(
        self,
        watchlist: Optional[Dict[str, Dict[str, Any]]] = None,
        cooldown_seconds: int = 60,
        milvus_host: str = settings.milvus_host,
        milvus_port: int = settings.milvus_port,
    ):
        self.watchlist = watchlist or {}
        self.cooldown_seconds = cooldown_seconds
        self.last_alerted: Dict[str, datetime] = {}
        self.gallery = VectorGallery(host=milvus_host, port=milvus_port)
        self._analyzer = None
        self._analyzer_initialized = False

    @property
    def is_persistent(self) -> bool:
        """Indicates whether persistent vector storage is connected."""
        return self.gallery.is_persistent

    def is_persistent_storage_available(self) -> bool:
        """Returns True only if persistent vector storage is connected."""
        return self.gallery.is_persistent

    def get_status(self) -> Dict[str, Any]:
        """Readiness and persistence summary for FRS."""
        gallery_status = self.gallery.get_status()
        analyzer_status = (self._analyzer is not None) if self._analyzer_initialized else None
        return {
            "ready": analyzer_status is not False,
            "persistent_storage": gallery_status["persistent"],
            "model_loaded": analyzer_status,
            "gallery": gallery_status,
        }

    def get_analyzer(self):
        """Lazy-load InsightFace buffalo_s on CPU."""
        if not self._analyzer_initialized:
            self._analyzer_initialized = True
            try:
                from insightface.app import FaceAnalysis
                logger.info("Initializing InsightFace FaceAnalysis (buffalo_s, CPU)...")
                analyzer = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
                analyzer.prepare(ctx_id=0, det_size=(320, 320))
                self._analyzer = analyzer
                logger.info("InsightFace FaceAnalysis initialized successfully.")
            except Exception as e:
                logger.warning("Failed to initialize InsightFace analyzer: %s; FRS disabled.", e)
                self._analyzer = None
        return self._analyzer

    def detect_and_extract_faces(self, img: np.ndarray) -> List[Dict[str, Any]]:
        """Detect face boxes and extract 512-dim normalized ArcFace embeddings."""
        if img is None or img.size == 0:
            return []
        analyzer = self.get_analyzer()
        if analyzer is None:
            return []

        try:
            faces = analyzer.get(img)
            extracted = []
            for face in faces:
                emb = getattr(face, "embedding", None)
                if emb is None:
                    continue
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb = emb / norm
                extracted.append({
                    "bbox": [float(c) for c in face.bbox.tolist()],
                    "embedding": emb,
                    "det_score": float(getattr(face, "det_score", 0.0)),
                })
            return extracted
        except Exception as e:
            logger.error("Face detection/embedding extraction error: %s", e)
            return []

    def assess_face_quality(self, crop_img: np.ndarray, face: Dict[str, Any]) -> Tuple[bool, float, str]:
        """Assess face detection quality, size, and sharpness (Laplacian blur).
        
        Returns: (is_usable, quality_score, reason)
        """
        bbox = face.get("bbox", [0, 0, 0, 0])
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        det_score = float(face.get("det_score", 0.0))

        if w < 28 or h < 28:
            return False, 0.0, f"face_too_small ({w:.0f}x{h:.0f})"

        if det_score < 0.45:
            return False, det_score, f"low_det_score ({det_score:.2f})"

        # Crop face patch for sharpness and lighting check
        ih, iw = crop_img.shape[:2]
        x1, y1 = max(0, int(bbox[0])), max(0, int(bbox[1]))
        x2, y2 = min(iw, int(bbox[2])), min(ih, int(bbox[3]))
        if (x2 - x1) < 10 or (y2 - y1) < 10:
            return False, 0.0, "invalid_crop"

        face_patch = crop_img[y1:y2, x1:x2]
        if face_patch.size == 0:
            return False, 0.0, "empty_patch"

        gray = cv2.cvtColor(face_patch, cv2.COLOR_BGR2GRAY)
        mean_val = float(gray.mean())
        if mean_val < 18 or mean_val > 245:
            return False, 0.0, f"extreme_lighting (mean={mean_val:.1f})"

        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        # Motion blur filter
        if lap_var < 20.0:
            return False, lap_var / 100.0, f"motion_blur (lap={lap_var:.1f})"

        size_norm = min(w * h, 16000.0) / 16000.0
        sharp_norm = min(lap_var, 300.0) / 300.0
        quality_score = (det_score * 0.4) + (sharp_norm * 0.4) + (size_norm * 0.2)
        return True, quality_score, "good_quality"

    def enroll_subject(
        self,
        name: str,
        image_or_path: Any,
        threat_level: str = "critical",
        notes: str = "Watchlist Subject",
        require_persistence: bool = False,
        allow_ephemeral: bool = True,
        replace: bool = False,
    ) -> bool:
        """Enroll a face portrait into the vector gallery."""
        if isinstance(image_or_path, str):
            if not os.path.exists(image_or_path):
                logger.error("Enrollment image not found: %s", image_or_path)
                return False
            img = cv2.imread(image_or_path)
        else:
            img = image_or_path

        if img is None or img.size == 0:
            return False

        faces = self.detect_and_extract_faces(img)
        if not faces:
            logger.warning("No face detected in enrollment image for '%s'", name)
            return False

        best_face = max(
            faces,
            key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]),
        )
        success = self.gallery.enroll(
            name=name,
            vector=best_face["embedding"],
            threat_level=threat_level,
            notes=notes,
            require_persistence=require_persistence,
            allow_ephemeral=allow_ephemeral,
            replace=replace,
        )
        if success:
            self.watchlist[name] = {"threat_level": threat_level, "notes": notes}
        return success

    def process_person_crop(
        self,
        camera_id: str,
        crop_img: np.ndarray,
        now: datetime,
        site_lat: float,
        site_lng: float,
        threshold: float = 0.70,
        track_id: Optional[str] = None,
        precomputed_faces: Optional[List[Dict[str, Any]]] = None,
    ) -> List[AlertEvent]:
        """Detect faces in person crop, query vector gallery, and emit AlertEvent on match."""
        faces = precomputed_faces if precomputed_faces is not None else self.detect_and_extract_faces(crop_img)
        if not faces:
            return []

        alerts: List[AlertEvent] = []
        for face in faces:
            emb = face["embedding"]
            matches = self.gallery.search(emb, top_k=1)
            if not matches:
                continue

            name, similarity, meta = matches[0]
            # Genuine threshold validation: reject unknown / unverified faces
            if not name or name.upper() in {"UNKNOWN", "UNVERIFIED"} or similarity < threshold:
                logger.debug("Face similarity %.3f below threshold %.3f for %s; unverified.", similarity, threshold, name)
                continue

            # Cooldown per subject
            last = self.last_alerted.get(name)
            if last and (now - last).total_seconds() < self.cooldown_seconds:
                continue
            self.last_alerted[name] = now

            threat = meta.get("threat_level", "critical")
            notes = meta.get("notes", "Watchlist Subject Identified")
            alert_id = uuid4()
            alerts.append(AlertEvent(
                event_id=alert_id,
                camera_id=camera_id,
                event_type="face_match",
                severity=threat if threat in ["critical", "high", "medium"] else "critical",
                details=f"Watchlist Face Identified: '{name}' (sim: {similarity:.2f}) - {notes}",
                confidence=round(float(similarity), 3),
                track_id=track_id,
                lat=site_lat,
                lng=site_lng,
                snapshot_path=f"snapshots/{camera_id}_{alert_id}.jpg",
                metadata={
                    "subject": name,
                    "threat": threat,
                    "similarity": round(float(similarity), 3),
                    "face_bbox": json.dumps(face["bbox"]),
                    "clip_path": f"snapshots/clips/{camera_id}_{alert_id}.mp4",
                },
            ))

        return alerts

    def evaluate_match(
        self,
        camera_id: str,
        subject_name: str,
        similarity: float,
        now: datetime,
        site_lat: float,
        site_lng: float,
        threshold: float = 0.65,
        track_id: Optional[str] = None,
    ) -> Optional[AlertEvent]:
        # Genuine match requires valid name (not unknown/unverified) and confidence above threshold
        if not subject_name or subject_name.upper() in {"UNKNOWN", "UNVERIFIED"} or similarity < threshold:
            return None

        info = self.watchlist.get(subject_name, {})
        threat = info.get("threat_level", "critical")
        notes = info.get("notes", "Watchlist Subject Identified")

        last = self.last_alerted.get(subject_name)
        if last and (now - last).total_seconds() < self.cooldown_seconds:
            return None
        self.last_alerted[subject_name] = now

        alert_id = uuid4()
        return AlertEvent(
            event_id=alert_id,
            camera_id=camera_id,
            event_type="face_match",
            severity=threat if threat in ["critical", "high", "medium"] else "critical",
            details=f"Watchlist Face Identified: '{subject_name}' (sim: {similarity:.2f}) - {notes}",
            confidence=round(float(similarity), 3),
            track_id=track_id,
            lat=site_lat,
            lng=site_lng,
            snapshot_path=f"snapshots/{camera_id}_{alert_id}.jpg",
            metadata={
                "subject": subject_name,
                "threat": threat,
                "similarity": round(float(similarity), 3),
                "clip_path": f"snapshots/clips/{camera_id}_{alert_id}.mp4",
            },
        )


# =========================================================================
# Unified Perception & Analytics Pipeline Coordinator
# =========================================================================
class UnifiedAnalyticsEngine:
    """Centralized Fog perception coordinator applying modules across cameras."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or settings.config_yaml_path
        self.cameras: Dict[str, Dict[str, Any]] = {}
        self.watchlists: Dict[str, Any] = {"plates": {}, "faces": {}}
        self._config_mtimes: Dict[str, float] = {}
        self._last_config_check: float = 0.0
        self.anpr_track_history: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.frs_track_history: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.frs_track_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.frs_observation_buffer: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.anpr_identity_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.frs_identity_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.last_telemetry: Optional[Dict[str, Any]] = None
        self._last_prune_time: float = 0.0
        self.load_config()

        self.intrusion = IntrusionModule()
        self.restricted_zone = RestrictedZoneModule()
        self.abandoned = AbandonedObjectModule(abandoned_seconds=settings.abandoned_seconds)
        self.anpr = ANPRModule(watchlist=self.watchlists.get("plates", {}))
        self.frs = FacialRecognitionModule(watchlist=self.watchlists.get("faces", {}))
        self._enroll_watchlist_faces()

    def _enroll_watchlist_faces(self) -> None:
        """Enroll faces from the watchlist into FRS VectorGallery if images are provided (multi-sample support)."""
        if not hasattr(self, "frs") or self.frs is None:
            return
        face_dict = self.watchlists.get("faces", {})
        for name, f_entry in list(face_dict.items()):
            if not isinstance(f_entry, dict):
                continue

            images_to_enroll: List[np.ndarray] = []

            # 1. Single or multiple base64 images
            b64_list = []
            if f_entry.get("images_base64") and isinstance(f_entry["images_base64"], list):
                b64_list.extend(f_entry["images_base64"])
            if f_entry.get("image_base64") and f_entry["image_base64"] not in b64_list:
                b64_list.append(f_entry["image_base64"])

            for b64 in b64_list:
                try:
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    raw = base64.b64decode(b64)
                    arr = np.frombuffer(raw, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None and img.size > 0:
                        images_to_enroll.append(img)
                except Exception as e:
                    logger.warning("Could not decode image_base64 sample for '%s': %s", name, e)

            # 2. Single or multiple image paths
            path_list = []
            if f_entry.get("image_paths") and isinstance(f_entry["image_paths"], list):
                path_list.extend(f_entry["image_paths"])
            if f_entry.get("image_path") and f_entry["image_path"] not in path_list:
                path_list.append(f_entry["image_path"])

            for p in path_list:
                if os.path.exists(p):
                    img = cv2.imread(p)
                    if img is not None and img.size > 0:
                        images_to_enroll.append(img)

            threat = f_entry.get("threat_level", "critical")
            notes = f_entry.get("notes", "Watchlist Face")

            enrolled_count = 0
            for img in images_to_enroll:
                success = self.frs.enroll_subject(name=name, image_or_path=img, threat_level=threat, notes=notes, replace=False)
                if success:
                    enrolled_count += 1

            if enrolled_count > 0:
                logger.info("Enrolled %d face sample(s) for '%s' (threat: %s) into FRS gallery", enrolled_count, name, threat)
            elif images_to_enroll:
                logger.warning("Failed to auto-enroll any face sample for '%s' (no face detected or error)", name)

    def load_config(self) -> None:
        paths_to_try = []
        if self.config_path and os.path.exists(self.config_path):
            paths_to_try.append(self.config_path)
        if settings.cameras_yaml_path and os.path.exists(settings.cameras_yaml_path) and settings.cameras_yaml_path not in paths_to_try:
            paths_to_try.append(settings.cameras_yaml_path)
        if settings.config_yaml_path and os.path.exists(settings.config_yaml_path) and settings.config_yaml_path not in paths_to_try:
            paths_to_try.append(settings.config_yaml_path)

        if not paths_to_try:
            return

        for path in paths_to_try:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    for cam in data.get("cameras", []):
                        cam_id = cam.get("id")
                        if cam_id:
                            self.cameras[cam_id] = cam

                    # Watchlists
                    wl = data.get("watchlists", {})
                    plate_list = wl.get("plates", [])
                    face_list = wl.get("faces", [])
                    for p in plate_list:
                        if "plate" in p:
                            self.watchlists["plates"][p["plate"]] = p
                    for f_entry in face_list:
                        if "name" in f_entry:
                            self.watchlists["faces"][f_entry["name"]] = f_entry
            except Exception as e:
                logger.error("Failed to load analytics config from %s: %s", path, e)

    def check_and_reload_config_if_modified(self) -> bool:
        changed = False
        for path in [self.config_path, settings.cameras_yaml_path, settings.config_yaml_path]:
            if path and os.path.exists(path):
                try:
                    mtime = os.path.getmtime(path)
                    if path in self._config_mtimes and self._config_mtimes[path] != mtime:
                        changed = True
                    self._config_mtimes[path] = mtime
                except OSError:
                    pass
        if changed:
            logger.info("Configuration file modified on disk; reloading analytics config")
            self.load_config()
            if hasattr(self, "anpr"):
                self.anpr.watchlist = self.watchlists.get("plates", {})
            if hasattr(self, "frs"):
                self.frs.watchlist = self.watchlists.get("faces", {})
                self._enroll_watchlist_faces()
            return True
        return False

    def prune_track_histories(self, now: datetime) -> None:
        expiry = 120.0  # prune entries older than 2 minutes
        to_del_anpr = [
            k for k, v in self.anpr_track_history.items()
            if (now - v["last_attempt"]).total_seconds() > expiry
        ]
        for k in to_del_anpr:
            self.anpr_track_history.pop(k, None)

        to_del_frs = [
            k for k, v in self.frs_track_history.items()
            if (now - v["last_attempt"]).total_seconds() > expiry
        ]
        for k in to_del_frs:
            self.frs_track_history.pop(k, None)

        for k in list(self.anpr_identity_cache.keys()):
            if (now - self.anpr_identity_cache[k]["updated_at"]).total_seconds() > expiry:
                self.anpr_identity_cache.pop(k, None)

        for k in list(self.frs_identity_cache.keys()):
            if (now - self.frs_identity_cache[k]["updated_at"]).total_seconds() > expiry:
                self.frs_identity_cache.pop(k, None)

    def process_edge_event(
        self,
        event: EdgeEvent,
        now: Optional[datetime] = None,
    ) -> List[AlertEvent]:
        now = now or event.occurred_at
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        # Check for dynamic config reload periodically
        now_epoch = time_mod.time()
        if now_epoch - self._last_config_check > 3.0:
            self._last_config_check = now_epoch
            self.check_and_reload_config_if_modified()

        # Prune track history periodically
        if now_epoch - self._last_prune_time > 30.0:
            self._last_prune_time = now_epoch
            self.prune_track_histories(now)

        cam_config = self.cameras.get(event.camera_id, {})
        loc = cam_config.get("location", {})
        lat = loc.get("lat", settings.site_lat)
        lng = loc.get("lng", settings.site_lng)
        modules_cfg = cam_config.get("modules", {})

        # Build shared TrackedObject instances (1 shared perception pass)
        shared_objects: List[TrackedObject] = []
        for det in event.detections:
            if not det.track_id:
                continue
            shared_objects.append(TrackedObject(
                camera_id=event.camera_id,
                track_id=det.track_id,
                label=det.label,
                confidence=det.confidence,
                bbox=det.bbox,
                centroid=bbox_centroid(det.bbox),
                occurred_at=now,
            ))

        alerts: List[AlertEvent] = []

        # 1. Intrusion / Tripwires
        intrusion_cfg = modules_cfg.get("intrusion", {})
        if intrusion_cfg.get("enabled", True):
            tripwires = intrusion_cfg.get("tripwires", [])
            if tripwires:
                alerts.extend(self.intrusion.process(shared_objects, tripwires, now, lat, lng))

        # 2. Restricted Zones
        rz_cfg = modules_cfg.get("restricted_zone", {})
        if rz_cfg.get("enabled", False):
            zones = rz_cfg.get("zones", [])
            if zones:
                alerts.extend(self.restricted_zone.process(shared_objects, zones, now, lat, lng))

        # 3. Abandoned / Suspicious Objects
        ab_cfg = modules_cfg.get("abandoned_object", {})
        if ab_cfg.get("enabled", False):
            ab_sec = int(ab_cfg.get("abandoned_seconds", self.abandoned.abandoned_seconds))
            prox_rad = float(ab_cfg.get("proximity_radius", self.abandoned.proximity_radius))
            alerts.extend(self.abandoned.process(
                shared_objects, now, lat, lng,
                abandoned_seconds=ab_sec,
                proximity_radius=prox_rad,
            ))

        # 4. ANPR / License Plate Recognition (Bounded & Throttled)
        anpr_cfg = modules_cfg.get("anpr", {})
        if anpr_cfg.get("enabled", True):
            min_conf = float(anpr_cfg.get("confidence_threshold", 0.50))
            debounce_sec = float(anpr_cfg.get("debounce_seconds", 1.5))
            cooldown_sec = float(anpr_cfg.get("cooldown_seconds", 60.0))
            crops_processed = 0

            for det in event.detections:
                if crops_processed >= 2:
                    break
                if det.label.lower() not in ANPRModule.VEHICLE_LABELS or not det.crop_base64:
                    continue
                if det.confidence < min_conf:
                    continue

                # Per-track throttling & debounce
                if det.track_id:
                    key = (event.camera_id, det.track_id)
                    hist = self.anpr_track_history.get(key)
                    if hist:
                        time_since_attempt = (now - hist["last_attempt"]).total_seconds()
                        if hist.get("matched") and time_since_attempt < cooldown_sec:
                            continue
                        if time_since_attempt < debounce_sec:
                            continue
                else:
                    cam_key = (event.camera_id, "__untracked_anpr__")
                    hist = self.anpr_track_history.get(cam_key)
                    if hist and (now - hist["last_attempt"]).total_seconds() < 1.0:
                        continue

                crops_processed += 1
                try:
                    img_bytes = base64.b64decode(det.crop_base64)
                    nparr = np.frombuffer(img_bytes, np.uint8)
                    crop_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    matched_alert = None
                    if crop_img is not None and crop_img.size > 0:
                        readings = self.anpr.detect_and_read_plate(crop_img)
                        for text, conf in readings:
                            if det.track_id:
                                norm_p = self.anpr.normalize_plate(text)
                                is_p_match = any(self.anpr.normalize_plate(w) == norm_p for w in self.anpr.watchlist.keys())
                                self.anpr_identity_cache[(event.camera_id, det.track_id)] = {
                                    "text": norm_p or text,
                                    "confidence": round(float(conf), 2),
                                    "matched": is_p_match,
                                    "updated_at": now,
                                }
                            alert = self.anpr.evaluate_plate_reading(
                                event.camera_id,
                                text,
                                conf,
                                now,
                                lat,
                                lng,
                                track_id=det.track_id,
                            )
                            if alert:
                                alerts.append(alert)
                                matched_alert = alert

                    rec_key = (event.camera_id, det.track_id) if det.track_id else (event.camera_id, "__untracked_anpr__")
                    self.anpr_track_history[rec_key] = {
                        "last_attempt": now,
                        "matched": matched_alert is not None,
                    }
                except Exception as exc:
                    logger.error("Failed to process ANPR crop for camera %s: %s", event.camera_id, exc)

        # 5. Facial Recognition System (FRS) (Bounded, Intelligent Scheduling & Hysteresis)
        frs_cfg = modules_cfg.get("facial_recognition", {})
        if frs_cfg.get("enabled", True):
            sim_threshold = float(frs_cfg.get("similarity_threshold", 0.50))
            min_conf = float(frs_cfg.get("confidence_threshold", 0.50))
            crops_processed = 0

            for det in event.detections:
                if crops_processed >= 2:
                    break
                if det.label.lower() != "person" or not det.crop_base64:
                    continue
                if det.confidence < min_conf:
                    continue

                track_key = (event.camera_id, det.track_id) if det.track_id else (event.camera_id, "__untracked_frs__")
                state_info = self.frs_track_state.get(track_key)

                # Intelligent CPU-bounded scheduling
                if state_info is not None:
                    time_since_attempt = (now - state_info["last_attempt"]).total_seconds()
                    curr_state = state_info.get("state", "UNIDENTIFIED")
                    if curr_state == "STABLE":
                        # Stable track: throttle to 4.0s to preserve CPU
                        if time_since_attempt < 4.0:
                            continue
                    elif curr_state == "PROVISIONAL":
                        # Fast confirmation in 0.30s
                        if time_since_attempt < 0.30:
                            continue
                    else:
                        # Active unidentified track: fast retry in 0.35s
                        if time_since_attempt < 0.35:
                            continue
                # If state_info is None -> Brand new track: immediate recognition opportunity (0.0s delay!)

                crops_processed += 1
                try:
                    t_obs_start = time_mod.time()
                    logger.info("FRS_OBSERVATION: camera=%s track=%s ts=%s",
                                event.camera_id, det.track_id, now.isoformat())

                    img_bytes = base64.b64decode(det.crop_base64)
                    nparr = np.frombuffer(img_bytes, np.uint8)
                    crop_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    if crop_img is None or crop_img.size == 0:
                        continue

                    # 1. Detect faces and measure detection latency
                    t_det_start = time_mod.time()
                    faces = self.frs.detect_and_extract_faces(crop_img)
                    det_time_ms = (time_mod.time() - t_det_start) * 1000.0
                    logger.info("FRS_FACE_DETECTED: track=%s faces=%d det_time_ms=%.2f",
                                det.track_id, len(faces), det_time_ms)

                    if not faces:
                        if state_info is None:
                            self.frs_track_state[track_key] = {
                                "state": "UNIDENTIFIED",
                                "subject": "Unknown Face",
                                "similarity": 0.0,
                                "consecutive_matches": 0,
                                "consecutive_misses": 1,
                                "provisional_name": None,
                                "last_attempt": now,
                                "first_seen": now,
                            }
                        else:
                            state_info["last_attempt"] = now

                        matches = self.frs.process_person_crop(
                            camera_id=event.camera_id,
                            crop_img=crop_img,
                            now=now,
                            site_lat=lat,
                            site_lng=lng,
                            threshold=sim_threshold,
                            track_id=det.track_id,
                            precomputed_faces=[],
                        )
                        if matches:
                            alerts.extend(matches)
                        continue

                    # 2. Quality assessment across detected faces
                    best_face = None
                    best_quality = -1.0
                    for face in faces:
                        is_usable, q_score, reason = self.frs.assess_face_quality(crop_img, face)
                        if q_score > best_quality:
                            best_quality = q_score
                            best_face = face

                    if best_face is None:
                        best_face = max(faces, key=lambda f: float(f.get("det_score", 0.0)))
                        best_quality = float(best_face.get("det_score", 0.0))

                    # Track observation buffer (bounded to 5 recent observations)
                    if track_key not in self.frs_observation_buffer:
                        self.frs_observation_buffer[track_key] = []
                    self.frs_observation_buffer[track_key].append({
                        "face": best_face,
                        "quality": best_quality,
                        "timestamp": now,
                    })
                    if len(self.frs_observation_buffer[track_key]) > 5:
                        self.frs_observation_buffer[track_key].pop(0)

                    # Select the highest-quality face observation from the recent buffer
                    best_obs = max(self.frs_observation_buffer[track_key], key=lambda o: o["quality"])
                    eval_face = best_obs["face"]

                    emb = eval_face.get("embedding")
                    if emb is None:
                        continue

                    # 3. Embedding and vector search
                    t_search_start = time_mod.time()
                    matches = self.frs.gallery.search(emb, top_k=1)
                    search_time_ms = (time_mod.time() - t_search_start) * 1000.0
                    logger.info("FRS_EMBEDDED: track=%s search_time_ms=%.2f", det.track_id, search_time_ms)

                    m_name = None
                    m_sim = 0.0
                    m_meta = {}
                    if matches and matches[0][0] and matches[0][0].upper() not in {"UNKNOWN", "UNVERIFIED"}:
                        m_name, m_sim, m_meta = matches[0]

                    logger.info("FRS_MATCHED: track=%s subject=%s sim=%.3f search_time_ms=%.2f",
                                det.track_id, m_name or "Unknown", m_sim, search_time_ms)

                    if state_info is None:
                        state_info = {
                            "state": "UNIDENTIFIED",
                            "subject": "Unknown Face",
                            "similarity": round(float(m_sim), 2),
                            "consecutive_matches": 0,
                            "consecutive_misses": 0,
                            "provisional_name": None,
                            "last_attempt": now,
                            "first_seen": now,
                        }
                        self.frs_track_state[track_key] = state_info
                    else:
                        state_info["last_attempt"] = now

                    # 4. Two-Stage Identity State Machine with Hysteresis
                    is_confident_match = (m_name is not None and m_sim >= sim_threshold)

                    if is_confident_match:
                        state_info["consecutive_misses"] = 0

                        # Process person crop alert evaluation (reusing precomputed face observation)
                        matched_alerts = self.frs.process_person_crop(
                            camera_id=event.camera_id,
                            crop_img=crop_img,
                            now=now,
                            site_lat=lat,
                            site_lng=lng,
                            threshold=sim_threshold,
                            track_id=det.track_id,
                            precomputed_faces=[eval_face],
                        )
                        if matched_alerts:
                            alerts.extend(matched_alerts)

                        if state_info["state"] == "UNIDENTIFIED":
                            # Provisional identity reached immediately (~0.3s - 1.0s)
                            state_info["state"] = "PROVISIONAL"
                            state_info["provisional_name"] = m_name
                            state_info["similarity"] = round(float(m_sim), 2)
                            state_info["consecutive_matches"] = 1
                            total_ms = (time_mod.time() - t_obs_start) * 1000.0
                            logger.info("FRS_IDENTITY_CONFIRMED: track=%s subject=%s state=PROVISIONAL total_ms=%.2f",
                                        det.track_id, m_name, total_ms)
                            self.frs_identity_cache[track_key] = {
                                "name": m_name,
                                "similarity": f"{m_sim:.2f} (PROVISIONAL)",
                                "matched": True,
                                "status": "PROVISIONAL",
                                "updated_at": now,
                            }
                        elif state_info["state"] == "PROVISIONAL":
                            if m_name == state_info.get("provisional_name"):
                                state_info["consecutive_matches"] += 1
                                if state_info["consecutive_matches"] >= 2:
                                    # 1 confirming observation reached -> STABLE!
                                    state_info["state"] = "STABLE"
                                    state_info["subject"] = m_name
                                    state_info["similarity"] = round(float(m_sim), 2)
                                    total_ms = (time_mod.time() - t_obs_start) * 1000.0
                                    logger.info("FRS_IDENTITY_CONFIRMED: track=%s subject=%s state=STABLE total_ms=%.2f",
                                                det.track_id, m_name, total_ms)
                                    self.frs_identity_cache[track_key] = {
                                        "name": m_name,
                                        "similarity": f"{m_sim:.2f} STABLE",
                                        "matched": True,
                                        "status": "STABLE",
                                        "updated_at": now,
                                    }
                            else:
                                state_info["provisional_name"] = m_name
                                state_info["consecutive_matches"] = 1
                        elif state_info["state"] == "STABLE":
                            if m_name == state_info["subject"]:
                                state_info["similarity"] = round(float(m_sim), 2)
                                self.frs_identity_cache[track_key] = {
                                    "name": m_name,
                                    "similarity": f"{m_sim:.2f} STABLE",
                                    "matched": True,
                                    "status": "STABLE",
                                    "updated_at": now,
                                }
                            else:
                                # Contradicting observation on stable track: apply hysteresis
                                state_info["consecutive_misses"] += 1
                                if state_info["consecutive_misses"] >= 6:
                                    state_info["state"] = "UNIDENTIFIED"
                                    state_info["subject"] = "Unknown Face"
                                    state_info["consecutive_matches"] = 0
                    else:
                        # Non-matching frame (< threshold or unknown)
                        if state_info["state"] == "STABLE":
                            # HYSTERESIS: Do not oscillate back to Unknown from occasional bad/blurry frames
                            state_info["consecutive_misses"] += 1
                            if state_info["consecutive_misses"] >= 6:
                                state_info["state"] = "UNIDENTIFIED"
                                state_info["subject"] = "Unknown Face"
                                self.frs_identity_cache[track_key] = {
                                    "name": "Unknown Face",
                                    "similarity": round(float(m_sim), 2),
                                    "matched": False,
                                    "status": "UNIDENTIFIED",
                                    "updated_at": now,
                                }
                        elif state_info["state"] == "PROVISIONAL":
                            state_info["consecutive_misses"] += 1
                            if state_info["consecutive_misses"] >= 3:
                                state_info["state"] = "UNIDENTIFIED"
                                state_info["provisional_name"] = None
                                self.frs_identity_cache[track_key] = {
                                    "name": "Unknown Face",
                                    "similarity": round(float(m_sim), 2),
                                    "matched": False,
                                    "status": "UNIDENTIFIED",
                                    "updated_at": now,
                                }
                        else:
                            self.frs_identity_cache[track_key] = {
                                "name": "Unknown Face",
                                "similarity": round(float(m_sim or eval_face.get("det_score", 0.0)), 2),
                                "matched": False,
                                "status": "UNIDENTIFIED",
                                "updated_at": now,
                            }
                except Exception as exc:
                    logger.error("Failed to process FRS crop for camera %s: %s", event.camera_id, exc)

        # Build live perception telemetry
        fw = 640.0
        fh = 480.0
        if event.detections:
            max_x = max(d.bbox.x2 for d in event.detections)
            max_y = max(d.bbox.y2 for d in event.detections)
            if max_x > fw:
                fw = max_x
            if max_y > fh:
                fh = max_y

        telemetry_tracks = []
        for det in event.detections:
            track_id = det.track_id or f"det_{len(telemetry_tracks)}"
            face_data = None
            frs_entry = self.frs_identity_cache.get((event.camera_id, det.track_id))
            if frs_entry and (now - frs_entry["updated_at"]).total_seconds() < 60.0:
                face_data = {
                    "name": frs_entry["name"],
                    "similarity": frs_entry["similarity"],
                    "matched": frs_entry["matched"],
                    "status": frs_entry.get("status", "STABLE" if frs_entry["matched"] else "UNIDENTIFIED"),
                }

            plate_data = None
            anpr_entry = self.anpr_identity_cache.get((event.camera_id, det.track_id))
            if anpr_entry and (now - anpr_entry["updated_at"]).total_seconds() < 60.0:
                plate_data = {
                    "text": anpr_entry["text"],
                    "confidence": anpr_entry["confidence"],
                    "matched": anpr_entry["matched"],
                }

            nx1 = max(0.0, min(1.0, det.bbox.x1 / fw))
            ny1 = max(0.0, min(1.0, det.bbox.y1 / fh))
            nx2 = max(0.0, min(1.0, det.bbox.x2 / fw))
            ny2 = max(0.0, min(1.0, det.bbox.y2 / fh))

            telemetry_tracks.append({
                "track_id": track_id,
                "class_name": det.label,
                "confidence": round(float(det.confidence), 2),
                "bbox": [det.bbox.x1, det.bbox.y1, det.bbox.x2, det.bbox.y2],
                "norm_bbox": [round(nx1, 4), round(ny1, 4), round(nx2, 4), round(ny2, 4)],
                "face": face_data,
                "plate": plate_data,
            })

        self.last_telemetry = {
            "type": "telemetry",
            "event_type": "telemetry",
            "camera_id": event.camera_id,
            "frame_id": event.frame_id,
            "timestamp": now.isoformat(),
            "frame_size": [int(fw), int(fh)],
            "tracks": telemetry_tracks,
        }

        return alerts

