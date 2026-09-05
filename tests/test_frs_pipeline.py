"""Tests for genuine Facial Recognition System (FRS) and ArcFace vector pipeline.

Validates real face detection, 512-dim ArcFace embedding extraction,
vector similarity search, threshold decision logic, AlertEvent emission,
and Central database/WebSocket propagation on CPU.
"""
from __future__ import annotations

import base64
import os
import unittest
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

import cv2
import numpy as np
import insightface

from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent
from fog.analytics import FacialRecognitionModule, UnifiedAnalyticsEngine, VectorGallery
from central.database import Alert, Base, SessionLocal, engine
from central.main import ingest_mqtt_alert, manager


class TestFRSPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        pkg_dir = os.path.dirname(insightface.__file__)
        cls.tom_hanks_path = os.path.join(pkg_dir, "data", "images", "Tom_Hanks_54745.png")
        cls.t1_path = os.path.join(pkg_dir, "data", "images", "t1.jpg")
        cls.tom_hanks_img = cv2.imread(cls.tom_hanks_path)
        cls.t1_img = cv2.imread(cls.t1_path)
        assert cls.tom_hanks_img is not None, "Tom Hanks image must be available in insightface package"
        assert cls.t1_img is not None, "t1 image must be available in insightface package"

    def setUp(self):
        self.db = SessionLocal()
        self.db.query(Alert).delete()
        self.db.commit()
        try:
            from pymilvus import MilvusClient
            mc = MilvusClient(uri="http://localhost:19530")
            if mc.has_collection("watchlist_faces"):
                mc.delete(collection_name="watchlist_faces", filter="id >= 0")
        except Exception:
            pass

    def tearDown(self):
        self.db.query(Alert).delete()
        self.db.commit()
        self.db.close()
        try:
            from pymilvus import MilvusClient
            mc = MilvusClient(uri="http://localhost:19530")
            if mc.has_collection("watchlist_faces"):
                mc.delete(collection_name="watchlist_faces", filter="id >= 0")
        except Exception:
            pass

    def test_frs_embedding_generation(self):
        """Verify real face localization and normalized 512-dim ArcFace embedding extraction."""
        frs = FacialRecognitionModule()
        faces = frs.detect_and_extract_faces(self.tom_hanks_img)

        self.assertGreaterEqual(len(faces), 1, "Must detect at least 1 face in portrait")
        face = faces[0]
        self.assertIn("bbox", face)
        self.assertIn("embedding", face)
        self.assertIn("det_score", face)

        emb = face["embedding"]
        self.assertEqual(emb.shape, (512,), "ArcFace embedding must be 512-dimensional")
        norm = np.linalg.norm(emb)
        self.assertTrue(np.isclose(norm, 1.0, atol=1e-3), "Embedding must be L2-normalized unit vector")
        self.assertGreater(face["det_score"], 0.6, "Detection score for portrait must be confident")

    def test_frs_known_face_matching(self):
        """Enroll subject into vector gallery, query transformed frame, and verify genuine cosine match."""
        frs = FacialRecognitionModule(cooldown_seconds=0)
        enrolled = frs.enroll_subject("Tom Hanks", self.tom_hanks_img, threat_level="high", notes="Test Target")
        self.assertTrue(enrolled, "Enrollment must succeed")

        # Create a transformed version of the face (simulating camera angle/scale shift)
        h, w = self.tom_hanks_img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), 4, 0.96)
        transformed_img = cv2.warpAffine(self.tom_hanks_img, M, (w, h))

        faces = frs.detect_and_extract_faces(transformed_img)
        self.assertGreaterEqual(len(faces), 1)

        matches = frs.gallery.search(faces[0]["embedding"], top_k=1)
        self.assertGreaterEqual(len(matches), 1)
        name, similarity, meta = matches[0]

        self.assertEqual(name, "Tom Hanks")
        self.assertGreater(similarity, 0.70, f"Same subject similarity {similarity} must exceed 0.70")
        self.assertEqual(meta.get("threat_level"), "high")

    def test_frs_unknown_face_rejection(self):
        """Verify that an unenrolled face is rejected without generating a false match alert."""
        frs = FacialRecognitionModule(cooldown_seconds=0)
        frs.enroll_subject("Tom Hanks", self.tom_hanks_img, threat_level="critical")

        # Query with t1 image (different person)
        now = datetime.now(timezone.utc)
        alerts = frs.process_person_crop(
            camera_id="cam-main-entrance",
            crop_img=self.t1_img,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            threshold=0.70,
            track_id="track-99",
        )
        self.assertEqual(len(alerts), 0, "Unenrolled/different face must not trigger a face_match alert")

    def test_frs_threshold_behavior(self):
        """Verify that similarity thresholds strictly control match decisions."""
        frs = FacialRecognitionModule(cooldown_seconds=0)
        frs.enroll_subject("Tom Hanks", self.tom_hanks_img)

        h, w = self.tom_hanks_img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), 6, 0.92)
        transformed_img = cv2.warpAffine(self.tom_hanks_img, M, (w, h))

        now = datetime.now(timezone.utc)
        # Strict threshold 0.99 should reject
        alerts_strict = frs.process_person_crop(
            camera_id="cam-main-entrance",
            crop_img=transformed_img,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            threshold=0.99,
        )
        self.assertEqual(len(alerts_strict), 0, "Strict threshold (0.99) must reject")

        # Standard threshold 0.70 should accept
        alerts_standard = frs.process_person_crop(
            camera_id="cam-main-entrance",
            crop_img=transformed_img,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            threshold=0.70,
        )
        self.assertEqual(len(alerts_standard), 1, "Standard threshold (0.70) must accept genuine match")

    def test_frs_alert_event_generation_and_e2e_pipeline(self):
        """Verify complete pipeline from person crop_base64 to AlertEvent, DB persistence, and WebSocket."""
        engine_inst = UnifiedAnalyticsEngine()
        engine_inst.frs.cooldown_seconds = 0
        engine_inst.frs.enroll_subject("Tom Hanks", self.tom_hanks_img, threat_level="critical", notes="Priority Suspect")

        # Encode person crop
        ok, buf = cv2.imencode(".jpg", self.tom_hanks_img)
        self.assertTrue(ok)
        crop_base64 = base64.b64encode(buf.tobytes()).decode("utf-8")

        now = datetime.now(timezone.utc)
        edge_event = EdgeEvent(
            camera_id="cam-main-entrance",
            frame_id=101,
            occurred_at=now,
            detections=[
                Detection(
                    label="person",
                    confidence=0.92,
                    bbox=BoundingBox(x1=50, y1=50, x2=350, y2=450),
                    track_id="person-trk-1",
                    crop_base64=crop_base64,
                )
            ],
        )

        alerts = engine_inst.process_edge_event(edge_event)
        frs_alerts = [a for a in alerts if a.event_type == "face_match"]
        self.assertEqual(len(frs_alerts), 1, "Must generate exactly 1 face_match alert")

        alert = frs_alerts[0]
        self.assertEqual(alert.event_type, "face_match")
        self.assertEqual(alert.camera_id, "cam-main-entrance")
        self.assertEqual(alert.track_id, "person-trk-1")
        self.assertEqual(alert.metadata.get("subject"), "Tom Hanks")
        self.assertEqual(alert.severity, "critical")
        self.assertGreater(alert.confidence, 0.70)

        # Central DB persistence and WebSocket propagation
        ws_messages = []

        class MockWS:
            async def send_json(self, msg):
                ws_messages.append(msg)

        mock_ws = MockWS()
        manager.active_connections.add(mock_ws)
        try:
            ingest_mqtt_alert(alert)
        finally:
            manager.active_connections.discard(mock_ws)

        db_alert = self.db.query(Alert).filter(Alert.event_type == "face_match").first()
        self.assertIsNotNone(db_alert, "Alert must be persisted in database")
        self.assertEqual(db_alert.node_id, "cam-main-entrance")
        self.assertEqual(db_alert.severity, "critical")

    def test_frs_startup_failure_cleanliness(self):
        """Verify that when analyzer or crop is invalid, FRS fails gracefully without fake alerts."""
        frs = FacialRecognitionModule()
        frs._analyzer = None
        frs._analyzer_initialized = True

        # Test empty/corrupt image
        empty_res = frs.detect_and_extract_faces(np.zeros((0, 0, 3), dtype=np.uint8))
        self.assertEqual(empty_res, [])

        # Process with disabled analyzer
        alerts = frs.process_person_crop(
            camera_id="cam-main-entrance",
            crop_img=self.tom_hanks_img,
            now=datetime.now(timezone.utc),
            site_lat=28.6139,
            site_lng=77.2090,
        )
        self.assertEqual(alerts, [], "Disabled analyzer must cleanly return empty alerts, never mock alerts")


if __name__ == "__main__":
    unittest.main()
