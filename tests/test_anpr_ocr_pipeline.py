"""Comprehensive unit, OCR engine, and end-to-end pipeline tests for genuine ANPR/OCR integration."""
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
from fastapi import Response

from central import database
from central import main as central
from fog.analytics import ANPRModule, UnifiedAnalyticsEngine
from fog.main import process_edge_payload
from shared.events import BoundingBox, Detection, EdgeEvent


def generate_plate_image(plate_text: str = "UP16AB1234") -> np.ndarray:
    """Generates an image of a license plate with text rendered via OpenCV."""
    # Create white plate canvas
    plate = np.ones((120, 400, 3), dtype=np.uint8) * 240
    # Black border
    cv2.rectangle(plate, (5, 5), (395, 115), (0, 0, 0), 4)
    # Render plate text
    cv2.putText(
        plate,
        plate_text,
        (25, 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.0,
        (0, 0, 0),
        5,
        cv2.LINE_AA,
    )
    return plate


def encode_image_base64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise ValueError("Failed to encode image to JPEG")
    return base64.b64encode(buf).decode("utf-8")


class ANPROCRPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.create_all(bind=database.engine)
        cls.analytics_engine = UnifiedAnalyticsEngine()

    def setUp(self):
        session = database.SessionLocal()
        try:
            session.query(database.Alert).delete()
            session.commit()
        finally:
            session.close()

    def test_plate_normalization_unit_logic(self):
        # Clean formatting and fuzzy character swaps
        self.assertEqual(ANPRModule.normalize_plate("UP-16-AB-1234"), "UP16AB1234")
        self.assertEqual(ANPRModule.normalize_plate("up 16 ab 1234"), "UP16AB1234")
        self.assertEqual(ANPRModule.normalize_plate("UP16AB1234"), "UP16AB1234")
        self.assertEqual(ANPRModule.normalize_plate("DL-01-XY-9999"), "DL01XY9999")
        # Short strings
        self.assertEqual(ANPRModule.normalize_plate("AB1"), "AB1")

    def test_watchlist_matching_and_cooldown(self):
        watchlist = {
            "UP16AB1234": {"owner": "Suspect Alpha", "threat_level": "critical"},
        }
        anpr = ANPRModule(watchlist=watchlist, cooldown_seconds=60)
        now = datetime.now(timezone.utc)

        # 1. Match watchlisted plate
        alert = anpr.evaluate_plate_reading(
            camera_id="cam-test",
            plate_text="UP-16-AB-1234",
            confidence=0.92,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            track_id="trk-veh-1",
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.event_type, "anpr_match")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.metadata.get("plate"), "UP16AB1234")

        # 2. Cooldown suppression for immediate re-reading
        alert_cooldown = anpr.evaluate_plate_reading(
            camera_id="cam-test",
            plate_text="UP-16-AB-1234",
            confidence=0.95,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            track_id="trk-veh-1",
        )
        self.assertIsNone(alert_cooldown)

        # 3. Non-watchlisted plate produces NO alert
        alert_unknown = anpr.evaluate_plate_reading(
            camera_id="cam-test",
            plate_text="KA-05-MN-1111",
            confidence=0.90,
            now=now,
            site_lat=28.6139,
            site_lng=77.2090,
            track_id="trk-veh-2",
        )
        self.assertIsNone(alert_unknown)

    def test_ocr_engine_text_extraction(self):
        anpr = ANPRModule()
        reader = anpr.get_ocr_reader()
        if reader is None:
            self.skipTest("EasyOCR engine unavailable in environment")

        plate_img = generate_plate_image("UP16AB1234")
        readings = anpr.detect_and_read_plate(plate_img)
        self.assertGreater(len(readings), 0, "OCR should extract text from plate image")

        # Confirm normalized text matches target
        extracted_texts = [ANPRModule.normalize_plate(text) for text, conf in readings]
        self.assertTrue(
            any("UP16AB1234" in t or t in "UP16AB1234" for t in extracted_texts),
            f"Extracted OCR text {extracted_texts} should contain UP16AB1234",
        )

    async def test_full_anpr_e2e_pipeline(self):
        now = datetime.now(timezone.utc)

        # Render watchlisted plate image & base64 encode
        plate_img = generate_plate_image("UP16AB1234")
        crop_b64 = encode_image_base64(plate_img)

        # Create EdgeEvent with car detection + crop
        event = EdgeEvent(
            camera_id="cam-main-entrance",
            occurred_at=now,
            frame_id=42,
            detections=[Detection(
                label="car",
                confidence=0.94,
                track_id="trk-car-77",
                bbox=BoundingBox(x1=10, y1=20, x2=200, y2=150),
                crop_base64=crop_b64,
            )],
        )

        # Process payload through Fog Pipeline (includes EasyOCR & ANPRModule)
        alerts = process_edge_payload(
            event.model_dump_json().encode("utf-8"),
            analytics=self.analytics_engine,
        )

        # 1. Verify AlertEvent generated by Fog
        anpr_alerts = [a for a in alerts if a.event_type == "anpr_match"]
        self.assertEqual(len(anpr_alerts), 1, "Should generate exactly 1 ANPR AlertEvent for watchlisted plate")
        alert = anpr_alerts[0]
        self.assertEqual(alert.camera_id, "cam-main-entrance")
        self.assertEqual(alert.metadata.get("plate"), "UP16AB1234")

        # 2. Persist to Central API & DB
        session = database.SessionLocal()
        try:
            resp = await central.create_alert(alert, Response(), session)
            self.assertTrue(resp.created)

            # Query database to confirm persistence
            saved = session.query(database.Alert).filter(database.Alert.event_id == str(alert.event_id)).first()
            self.assertIsNotNone(saved)
            self.assertEqual(saved.event_type, "anpr_match")
            self.assertEqual(saved.node_id, "cam-main-entrance")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
