"""Comprehensive unit and integration tests for Phase 3 analytics and API extensions."""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

from fastapi import Response

from central import database
from central import main as central
from fog.analytics import (
    ANPRModule,
    AbandonedObjectModule,
    FacialRecognitionModule,
    IntrusionModule,
    RestrictedZoneModule,
    TrackedObject,
    UnifiedAnalyticsEngine,
    bbox_centroid,
    point_in_polygon,
    segments_intersect,
)
from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent


class AnalyticsAndFeaturesTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.create_all(bind=database.engine)

    def test_geometry_primitives(self):
        # Line intersection test
        self.assertTrue(segments_intersect((0, 0), (2, 2), (0, 2), (2, 0)))
        self.assertFalse(segments_intersect((0, 0), (1, 1), (2, 2), (3, 3)))

        # Point in polygon test
        polygon = [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
        self.assertTrue(point_in_polygon((2.0, 2.0), polygon))
        self.assertFalse(point_in_polygon((5.0, 5.0), polygon))

    def test_intrusion_tripwire_module(self):
        intrusion = IntrusionModule(cooldown_seconds=10)
        t0 = datetime.now(timezone.utc)
        tripwires = [{"name": "Border Fence", "line": [[0.0, 5.0], [10.0, 5.0]]}]

        # Frame 1: Object at (5.0, 4.0) (South of wire)
        obj_f1 = TrackedObject(
            camera_id="cam-test",
            track_id="trk-1",
            label="person",
            confidence=0.92,
            bbox=BoundingBox(x1=4.5, y1=3.5, x2=5.5, y2=4.5),
            centroid=(5.0, 4.0),
            occurred_at=t0,
        )
        alerts_f1 = intrusion.process([obj_f1], tripwires, t0, 28.0, 77.0)
        self.assertEqual(len(alerts_f1), 0)

        # Frame 2: Object moves to (5.0, 6.0) (North of wire) -> Intersects!
        t1 = t0 + timedelta(seconds=1)
        obj_f2 = TrackedObject(
            camera_id="cam-test",
            track_id="trk-1",
            label="person",
            confidence=0.92,
            bbox=BoundingBox(x1=4.5, y1=5.5, x2=5.5, y2=6.5),
            centroid=(5.0, 6.0),
            occurred_at=t1,
        )
        alerts_f2 = intrusion.process([obj_f2], tripwires, t1, 28.0, 77.0)
        self.assertEqual(len(alerts_f2), 1)
        self.assertEqual(alerts_f2[0].event_type, "intrusion")
        self.assertEqual(alerts_f2[0].severity, "high")
        self.assertIn("Border Fence", alerts_f2[0].details)

        # Immediate next frame does not re-alert due to cooldown
        t2 = t1 + timedelta(seconds=1)
        alerts_f3 = intrusion.process([obj_f2], tripwires, t2, 28.0, 77.0)
        self.assertEqual(len(alerts_f3), 0)

    def test_restricted_zone_module(self):
        rz = RestrictedZoneModule(cooldown_seconds=10)
        t0 = datetime.now(timezone.utc)
        zones = [{
            "name": "Restricted Armory",
            "polygon": [[1.0, 1.0], [5.0, 1.0], [5.0, 5.0], [1.0, 5.0]],
            "time_window": {"start": "00:00", "end": "23:59"},
        }]

        # Outside zone: centroid (0.5, 0.5)
        obj_out = TrackedObject(
            camera_id="cam-test",
            track_id="trk-rz",
            label="person",
            confidence=0.88,
            bbox=BoundingBox(x1=0.0, y1=0.0, x2=1.0, y2=1.0),
            centroid=(0.5, 0.5),
            occurred_at=t0,
        )
        self.assertEqual(rz.process([obj_out], zones, t0, 28.0, 77.0), [])

        # Inside zone: centroid (3.0, 3.0)
        obj_in = TrackedObject(
            camera_id="cam-test",
            track_id="trk-rz",
            label="person",
            confidence=0.88,
            bbox=BoundingBox(x1=2.5, y1=2.5, x2=3.5, y2=3.5),
            centroid=(3.0, 3.0),
            occurred_at=t0,
        )
        alerts = rz.process([obj_in], zones, t0, 28.0, 77.0)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].event_type, "restricted_zone")
        self.assertEqual(alerts[0].severity, "critical")

    def test_abandoned_object_module(self):
        abandoned = AbandonedObjectModule(
            abandoned_seconds=5, movement_threshold=0.05, proximity_radius=0.20, cooldown_seconds=15
        )
        t0 = datetime.now(timezone.utc)

        bag = TrackedObject(
            camera_id="cam-test",
            track_id="bag-1",
            label="backpack",
            confidence=0.85,
            bbox=BoundingBox(x1=0.5, y1=0.5, x2=0.6, y2=0.6),
            centroid=(0.55, 0.55),
            occurred_at=t0,
        )

        # Initial frame -> no alert
        self.assertEqual(abandoned.process([bag], t0, 28.0, 77.0), [])

        # Bag still there at 3s with a person nearby at (0.58, 0.58) -> Not unattended
        person = TrackedObject(
            camera_id="cam-test",
            track_id="person-1",
            label="person",
            confidence=0.90,
            bbox=BoundingBox(x1=0.55, y1=0.55, x2=0.65, y2=0.65),
            centroid=(0.58, 0.58),
            occurred_at=t0 + timedelta(seconds=3),
        )
        alerts_attended = abandoned.process([bag, person], t0 + timedelta(seconds=3), 28.0, 77.0)
        self.assertEqual(len(alerts_attended), 0)

        # Person leaves at 3s. Bag remains stationary. At 8s (3s + 5s), alert is triggered!
        abandoned.process([bag], t0 + timedelta(seconds=6), 28.0, 77.0)
        alerts_abandoned = abandoned.process([bag], t0 + timedelta(seconds=8), 28.0, 77.0)
        self.assertEqual(len(alerts_abandoned), 1)
        self.assertEqual(alerts_abandoned[0].event_type, "abandoned_object")
        self.assertEqual(alerts_abandoned[0].severity, "high")
        # Immediate subsequent frame at 10s is suppressed by cooldown
        self.assertEqual(abandoned.process([bag], t0 + timedelta(seconds=10), 28.0, 77.0), [])

    def test_anpr_plate_normalization_and_watchlist(self):
        watchlist = {
            "UP16AB1234": {"owner": "Target 1", "threat_level": "critical", "notes": "Interdict"},
            "DL01XY9999": {"owner": "Target 2", "threat_level": "high", "notes": "Flagged"},
        }
        anpr = ANPRModule(watchlist=watchlist)

        # Normalization
        self.assertEqual(anpr.normalize_plate("up 16 ab 1234"), "UP16AB1234")
        self.assertEqual(anpr.normalize_plate("UP-16-AB-1234"), "UP16AB1234")

        # Non-watchlist vehicle -> returns None
        now = datetime.now(timezone.utc)
        self.assertIsNone(anpr.evaluate_plate_reading("cam-test", "HR26DK5555", 0.90, now, 28.0, 77.0))

        # Watchlist vehicle -> generates anpr_match alert
        alert = anpr.evaluate_plate_reading("cam-test", "UP-16-AB-1234", 0.95, now, 28.0, 77.0, "v-track-1")
        self.assertIsNotNone(alert)
        self.assertEqual(alert.event_type, "anpr_match")
        self.assertEqual(alert.severity, "critical")
        self.assertIn("UP16AB1234", alert.details)

    def test_facial_recognition_module_integrity(self):
        watchlist = {"Suspect-Alpha": {"threat_level": "critical", "notes": "Wanted"}}
        frs = FacialRecognitionModule(watchlist=watchlist)
        now = datetime.now(timezone.utc)

        # Unknown / unverified names must NEVER generate match alert
        self.assertIsNone(frs.evaluate_match("cam-test", "UNKNOWN", 0.95, now, 28.0, 77.0))
        self.assertIsNone(frs.evaluate_match("cam-test", "UNVERIFIED", 0.85, now, 28.0, 77.0))
        self.assertIsNone(frs.evaluate_match("cam-test", "", 0.99, now, 28.0, 77.0))

        # Below threshold match must return None
        self.assertIsNone(frs.evaluate_match("cam-test", "Suspect-Alpha", 0.50, now, 28.0, 77.0, threshold=0.70))

        # Genuine match above threshold returns face_match alert
        alert = frs.evaluate_match("cam-test", "Suspect-Alpha", 0.88, now, 28.0, 77.0, threshold=0.70, track_id="p-1")
        self.assertIsNotNone(alert)
        self.assertEqual(alert.event_type, "face_match")
        self.assertEqual(alert.severity, "critical")
        self.assertIn("Suspect-Alpha", alert.details)

    async def test_central_rest_api_search_and_cameras(self):
        # 1. Test GET /api/cameras returns configured list
        data = central.get_configured_cameras()
        self.assertIn("cameras", data)
        self.assertTrue(len(data["cameras"]) >= 1)

        # 2. Test GET /api/watchlists
        wl_data = central.get_watchlists()
        self.assertIn("plates", wl_data)

        # 3. Test POST /api/watchlists/plates
        req = central.AddPlateRequest(
            plate="KA01ZZ1111",
            owner="Unit Test Target",
            threat_level="high",
            notes="Added via direct call",
        )
        resp_add = central.add_watchlist_plate(req)
        self.assertEqual(resp_add["status"], "success")

        # 4. Ingest an alert with specific event_type and verify filtering
        alert_event = AlertEvent(
            camera_id="cam-filter-test",
            event_type="restricted_zone",
            severity="critical",
            details="Restricted zone breached at armory post",
            confidence=0.94,
        )
        session = database.SessionLocal()
        try:
            create_resp = await central.create_alert(alert_event, Response(), session)
            self.assertTrue(create_resp.created)

            # Filter by event_type
            filtered_list = central.get_alerts(event_type="restricted_zone", db=session)
            self.assertTrue(any(a["camera_id"] == "cam-filter-test" for a in filtered_list))

            # Search by keyword
            search_list = central.get_alerts(search="armory", db=session)
            self.assertTrue(any("armory" in a["details"] for a in search_list))
        finally:
            session.close()
