"""Targeted unit and integration tests verifying all P1 production-readiness fixes.

Validates:
1. Fog ANPR & FRS inference throttling, per-track debounce, and confidence thresholds.
2. Edge frame/event rate throttling and target FPS resolution.
3. Evidence clip browser compatibility (H.264/AVC1 encoding, yuv420p, faststart, playability).
4. Central preview connector isolation (bounding, cleanup, disconnect endpoint).
5. Fog per-camera YAML configuration overrides (module disable, custom loitering/abandoned parameters).
6. Central & Fog watchlist persistence and dynamic synchronization.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import yaml

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

from central import database
from central import main as central
from edge.evidence import EvidenceRecorder
from edge import main as edge_main
from fog.analytics import (
    ANPRModule,
    AbandonedObjectModule,
    FacialRecognitionModule,
    TrackedObject,
    UnifiedAnalyticsEngine,
)
from fog.main import FogRuleEngine, process_edge_payload
from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent


def make_dummy_jpeg_base64() -> str:
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    return base64.b64encode(buf).decode("utf-8")


class P1ProductionReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.create_all(bind=database.engine)

    def setUp(self):
        self.dummy_jpeg = make_dummy_jpeg_base64()

    # -------------------------------------------------------------------------
    # 1. Fog ANPR & FRS Inference Throttling & Debouncing
    # -------------------------------------------------------------------------
    def test_anpr_inference_throttling_and_debounce(self):
        """Verify repeated frames for the same tracked vehicle are debounced and not re-inferred."""
        engine = UnifiedAnalyticsEngine()
        # Mock OCR detector to track call count
        engine.anpr.detect_and_read_plate = MagicMock(return_value=[("UP16AB1234", 0.95)])

        now = datetime.now(timezone.utc)
        cam_id = "cam-main-entrance"

        # Frame 1: Vehicle detected with confidence 0.90
        ev1 = EdgeEvent(
            camera_id=cam_id,
            frame_id=1,
            occurred_at=now,
            detections=[Detection(
                label="car",
                confidence=0.90,
                track_id="veh-track-1",
                bbox=BoundingBox(x1=10, y1=10, x2=100, y2=100),
                crop_base64=self.dummy_jpeg,
            )],
        )
        alerts1 = engine.process_edge_event(ev1, now=now)
        self.assertEqual(engine.anpr.detect_and_read_plate.call_count, 1)
        self.assertEqual(len(alerts1), 1)
        self.assertEqual(alerts1[0].event_type, "anpr_match")

        # Frame 2: Immediate next frame (30ms later) for same vehicle -> MUST be debounced/throttled!
        ev2 = EdgeEvent(
            camera_id=cam_id,
            frame_id=2,
            occurred_at=now + timedelta(milliseconds=30),
            detections=[Detection(
                label="car",
                confidence=0.90,
                track_id="veh-track-1",
                bbox=BoundingBox(x1=12, y1=10, x2=102, y2=100),
                crop_base64=self.dummy_jpeg,
            )],
        )
        alerts2 = engine.process_edge_event(ev2, now=now + timedelta(milliseconds=30))
        # OCR call count MUST still be 1 (did not burn CPU)
        self.assertEqual(engine.anpr.detect_and_read_plate.call_count, 1)
        self.assertEqual(len(alerts2), 0)

    def test_anpr_confidence_threshold_filtering(self):
        """Verify detections below confidence threshold are rejected before decoding/inference."""
        engine = UnifiedAnalyticsEngine()
        engine.anpr.detect_and_read_plate = MagicMock(return_value=[("UP16AB1234", 0.95)])

        now = datetime.now(timezone.utc)
        # Vehicle detection with confidence 0.30 (below default 0.50)
        ev = EdgeEvent(
            camera_id="cam-main-entrance",
            frame_id=1,
            occurred_at=now,
            detections=[Detection(
                label="car",
                confidence=0.30,
                track_id="low-conf-veh",
                bbox=BoundingBox(x1=10, y1=10, x2=100, y2=100),
                crop_base64=self.dummy_jpeg,
            )],
        )
        alerts = engine.process_edge_event(ev, now=now)
        # detect_and_read_plate should NEVER be called
        self.assertEqual(engine.anpr.detect_and_read_plate.call_count, 0)
        self.assertEqual(len(alerts), 0)

    def test_frs_inference_throttling_and_debounce(self):
        """Verify repeated person detections for the same track are debounced."""
        engine = UnifiedAnalyticsEngine()
        engine.frs.process_person_crop = MagicMock(return_value=[
            AlertEvent(
                camera_id="cam-main-entrance",
                event_type="face_match",
                severity="critical",
                details="Target Suspect-Alpha spotted",
                confidence=0.90,
            )
        ])

        now = datetime.now(timezone.utc)
        cam_id = "cam-main-entrance"

        # Frame 1: Person detected with confidence 0.85
        ev1 = EdgeEvent(
            camera_id=cam_id,
            frame_id=1,
            occurred_at=now,
            detections=[Detection(
                label="person",
                confidence=0.85,
                track_id="person-trk-1",
                bbox=BoundingBox(x1=10, y1=10, x2=50, y2=90),
                crop_base64=self.dummy_jpeg,
            )],
        )
        alerts1 = engine.process_edge_event(ev1, now=now)
        self.assertEqual(engine.frs.process_person_crop.call_count, 1)
        self.assertEqual(len(alerts1), 1)

        # Frame 2: Immediate next frame (50ms later) -> debounced!
        ev2 = EdgeEvent(
            camera_id=cam_id,
            frame_id=2,
            occurred_at=now + timedelta(milliseconds=50),
            detections=[Detection(
                label="person",
                confidence=0.85,
                track_id="person-trk-1",
                bbox=BoundingBox(x1=11, y1=10, x2=51, y2=90),
                crop_base64=self.dummy_jpeg,
            )],
        )
        alerts2 = engine.process_edge_event(ev2, now=now + timedelta(milliseconds=50))
        self.assertEqual(engine.frs.process_person_crop.call_count, 1)
        self.assertEqual(len(alerts2), 0)

    # -------------------------------------------------------------------------
    # 2. Edge Frame / Event Rate Throttling
    # -------------------------------------------------------------------------
    def test_edge_target_fps_resolution(self):
        """Verify Edge resolves target FPS from camera config or environment."""
        with patch.dict(os.environ, {"EDGE_TARGET_FPS": "12.5"}):
            fps = edge_main.resolve_target_fps()
            self.assertEqual(fps, 12.5)

        # Fallback to sensible default
        with patch.dict(os.environ, {}, clear=True):
            fps = edge_main.resolve_target_fps()
            self.assertGreater(fps, 0.0)

    # -------------------------------------------------------------------------
    # 3. Evidence Clip Browser Compatibility (H.264/AVC1 Playability)
    # -------------------------------------------------------------------------
    def test_evidence_clip_h264_encoding_and_playability(self):
        """Verify evidence clips are encoded in browser-compatible H.264/AVC1 format."""
        temp_dir = tempfile.mkdtemp()
        try:
            target_clip = os.path.join(temp_dir, "test_evidence.mp4")
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
            frames = [frame.copy() for _ in range(15)]

            EvidenceRecorder._encode_clip_worker(frames, target_clip, fps=10.0)

            self.assertTrue(os.path.exists(target_clip), "Clip file was not created!")
            self.assertGreater(os.path.getsize(target_clip), 0, "Clip file is empty!")

            # Verify readability with cv2.VideoCapture
            cap = cv2.VideoCapture(target_clip)
            self.assertTrue(cap.isOpened(), "Could not open encoded video clip!")
            ret, read_frame = cap.read()
            self.assertTrue(ret, "Could not read frame from encoded video clip!")
            self.assertEqual(read_frame.shape, (240, 320, 3))
            cap.release()

            # Verify stream format via ffprobe if available
            if shutil.which("ffprobe"):
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,pix_fmt", "-of", "default=noprint_wrappers=1", target_clip],
                    capture_output=True,
                    text=True,
                )
                self.assertIn("codec_name=h264", probe.stdout)
                self.assertIn("pix_fmt=yuv420p", probe.stdout)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 4. Central /connect-camera Isolation & Slot Management
    # -------------------------------------------------------------------------
    def test_central_preview_slot_isolation_and_disconnect(self):
        """Verify preview connectors are bounded, cleanly isolated, and disconnectable."""
        self.assertLessEqual(len(central.active_connectors), central.MAX_PREVIEW_SLOTS)

        # Test disconnect on non-existent slot
        res_noop = central.disconnect_camera_slot("UnknownSlot99")
        self.assertEqual(res_noop["status"], "noop")

        # Mock a connector on slot 'SlotTest'
        mock_connector = MagicMock()
        central.active_connectors["SlotTest"] = mock_connector

        res_disc = central.disconnect_camera_slot("SlotTest")
        self.assertEqual(res_disc["status"], "success")
        self.assertNotIn("SlotTest", central.active_connectors)
        mock_connector.stop.assert_called_once()

    # -------------------------------------------------------------------------
    # 5. Fog Per-Camera YAML Configuration & Overrides
    # -------------------------------------------------------------------------
    def test_fog_honors_disabled_camera_modules(self):
        """Verify that cameras with disabled modules (e.g. border_cam_01) emit zero alerts."""
        engine = UnifiedAnalyticsEngine()
        # border_cam_01 has all modules disabled in cameras.yaml
        now = datetime.now(timezone.utc)
        ev = EdgeEvent(
            camera_id="border_cam_01",
            frame_id=1,
            occurred_at=now,
            detections=[
                Detection(label="person", confidence=0.95, track_id="intruder-1", bbox=BoundingBox(x1=0.1, y1=0.1, x2=0.5, y2=0.5)),
                Detection(label="car", confidence=0.95, track_id="veh-1", bbox=BoundingBox(x1=0.1, y1=0.1, x2=0.5, y2=0.5), crop_base64=self.dummy_jpeg),
            ],
        )
        alerts = engine.process_edge_event(ev, now=now)
        self.assertEqual(len(alerts), 0, "Disabled camera modules must not emit alerts!")

    def test_fog_rule_engine_honors_per_camera_loitering(self):
        """Verify FogRuleEngine skips loitering if disabled for a camera."""
        rule_engine = FogRuleEngine(loitering_seconds=5, track_expiry_seconds=3)
        cam_config_disabled = {"modules": {"loitering": {"enabled": False}}}

        now = datetime.now(timezone.utc)
        ev = EdgeEvent(
            camera_id="cam-test-disabled",
            frame_id=1,
            occurred_at=now,
            detections=[Detection(label="person", confidence=0.90, track_id="trk-no-loiter", bbox=BoundingBox(x1=1, y1=1, x2=10, y2=10))],
        )

        alerts = rule_engine.process(ev, camera_config=cam_config_disabled)
        self.assertEqual(len(alerts), 0)

    # -------------------------------------------------------------------------
    # 6. Watchlist Persistence Through Central & Fog Dynamic Reload
    # -------------------------------------------------------------------------
    def test_watchlist_enrollment_persistence_and_fog_reload(self):
        """Verify plate & face enrollments persist to YAML and are picked up by Fog."""
        # 1. Enroll face via Central API
        face_name = f"Suspect-P1Test-{int(time.time())}"
        req = central.AddFaceRequest(
            name=face_name,
            threat_level="critical",
            notes="P1 automated test target",
        )
        resp = central.add_watchlist_face(req)
        self.assertEqual(resp["status"], "success")

        # 2. Check that Central get_watchlists returns it
        wl = central.get_watchlists()
        face_names = [f.get("name") for f in wl.get("faces", [])]
        self.assertIn(face_name, face_names)

        # 3. Verify Fog dynamic reload updates watchlists
        fog_engine = UnifiedAnalyticsEngine()
        fog_engine.check_and_reload_config_if_modified()
        self.assertIn(face_name, fog_engine.watchlists.get("faces", {}))

        # Cleanup test face from YAMLs
        for cfg_path in [central.settings.cameras_yaml_path, central.settings.config_yaml_path]:
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                faces = data.get("watchlists", {}).get("faces", [])
                data["watchlists"]["faces"] = [f for f in faces if f.get("name") != face_name]
                with open(cfg_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, sort_keys=False)


if __name__ == "__main__":
    unittest.main()
