import unittest
from unittest.mock import patch, MagicMock
import tempfile
import os
import yaml

from ingestion.config import (
    load_config,
    save_config,
    append_camera_config,
    update_camera_config,
    set_camera_status,
    delete_camera_config,
)
from central.main import app
from central.database import Base, Alert, engine, SessionLocal


class TestCameraManagement(unittest.TestCase):
    """
    Tests for camera management functionality:
    - Edit existing camera configuration
    - Enable / disable cameras
    - Delete cameras
    - Verify deleting camera does NOT delete historical alerts, snapshots, or db records
    - Verify persistence in YAML config
    - Verify Central REST API endpoints
    """

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp_dir.name, "cameras.yaml")
        self.initial_data = {
            "cameras": [
                {
                    "id": "CAM_01",
                    "name": "Local Node",
                    "rtsp_url": "http://localhost:8085/video_feed",
                    "status": "active",
                    "location": {"lat": 28.6139, "lng": 77.2090, "address": "BOP Alpha"},
                    "modules": {"intrusion": {"enabled": True}}
                },
                {
                    "id": "CAM_02",
                    "name": "Checkpost Ingress",
                    "rtsp_url": "rtsp://192.168.1.33:554/stream",
                    "status": "active",
                    "location": {"lat": 28.6145, "lng": 77.2085, "address": "Gate 2"},
                    "modules": {"intrusion": {"enabled": False}}
                }
            ]
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.initial_data, f)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_update_camera_config_success(self):
        updates = {
            "name": "Updated Checkpost Bay",
            "rtsp_url": "rtsp://192.168.1.99:554/stream2",
            "status": "disabled",
        }
        updated = update_camera_config("CAM_02", updates, config_path=self.config_path)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["name"], "Updated Checkpost Bay")
        self.assertEqual(updated["rtsp_url"], "rtsp://192.168.1.99:554/stream2")
        self.assertEqual(updated["status"], "disabled")
        # Location & modules should be preserved
        self.assertEqual(updated["location"]["address"], "Gate 2")
        self.assertEqual(updated["modules"]["intrusion"]["enabled"], False)

        # Verify disk persistence
        cfg = load_config(self.config_path)
        cam2 = next(c for c in cfg["cameras"] if c["id"] == "CAM_02")
        self.assertEqual(cam2["name"], "Updated Checkpost Bay")
        self.assertEqual(cam2["status"], "disabled")

    def test_update_nonexistent_camera_returns_none(self):
        res = update_camera_config("NON_EXISTENT", {"name": "Ghost"}, config_path=self.config_path)
        self.assertIsNone(res)

    def test_set_camera_status(self):
        # Disable CAM_01
        res = set_camera_status("CAM_01", "disabled", config_path=self.config_path)
        self.assertTrue(res)
        cfg = load_config(self.config_path)
        cam1 = next(c for c in cfg["cameras"] if c["id"] == "CAM_01")
        self.assertEqual(cam1["status"], "disabled")

        # Re-enable CAM_01
        res = set_camera_status("CAM_01", "active", config_path=self.config_path)
        self.assertTrue(res)
        cfg = load_config(self.config_path)
        cam1 = next(c for c in cfg["cameras"] if c["id"] == "CAM_01")
        self.assertEqual(cam1["status"], "active")

    def test_delete_camera_config(self):
        res = delete_camera_config("CAM_02", config_path=self.config_path)
        self.assertTrue(res)

        cfg = load_config(self.config_path)
        self.assertEqual(len(cfg["cameras"]), 1)
        self.assertEqual(cfg["cameras"][0]["id"], "CAM_01")

        # Deleting again should return False
        res2 = delete_camera_config("CAM_02", config_path=self.config_path)
        self.assertFalse(res2)

    def test_deleting_camera_preserves_historical_alerts(self):
        """Deleting a camera from config MUST NOT delete database alerts or records for that camera."""
        from datetime import datetime
        db = SessionLocal()
        test_event_id = f"test-hist-alert-{os.getpid()}"
        try:
            # Seed a historical alert for CAM_02
            alert = Alert(
                event_id=test_event_id,
                node_id="CAM_02",
                event_type="UNAUTHORIZED_PRESENCE",
                severity="critical",
                details="Historical intruder event before camera decommission",
                lat=28.6145,
                lng=77.2085,
                snapshot_path="/app/snapshots/test_cam02_hist.jpg",
                clip_path="/app/snapshots/test_cam02_hist.mp4",
                timestamp=datetime.utcnow()
            )
            db.add(alert)
            db.commit()

            # Now delete CAM_02 from configuration
            delete_success = delete_camera_config("CAM_02", config_path=self.config_path)
            self.assertTrue(delete_success)

            # Query database: historical alert must still be 100% present and intact
            fetched = db.query(Alert).filter(Alert.event_id == test_event_id).first()
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.node_id, "CAM_02")
            self.assertEqual(fetched.event_type, "UNAUTHORIZED_PRESENCE")
            self.assertEqual(fetched.snapshot_path, "/app/snapshots/test_cam02_hist.jpg")
            self.assertEqual(fetched.clip_path, "/app/snapshots/test_cam02_hist.mp4")

        finally:
            # Cleanup test db record
            db.query(Alert).filter(Alert.event_id == test_event_id).delete()
            db.commit()
            db.close()

    def test_central_api_camera_endpoints(self):
        import dataclasses
        from shared.config import settings
        from central.main import (
            get_configured_cameras,
            update_camera,
            toggle_camera,
            delete_camera,
            UpdateCameraRequest,
            ToggleCameraRequest,
        )

        new_settings = dataclasses.replace(
            settings,
            cameras_yaml_path=self.config_path,
            config_yaml_path=self.config_path
        )

        with patch("central.main.settings", new_settings):

            # 1. GET /api/cameras
            res = get_configured_cameras()
            self.assertEqual(res["status"], "success")
            self.assertIn("cameras", res)
            cams = res["cameras"]
            self.assertEqual(len(cams), 2)

            # 2. PUT /api/cameras/CAM_02
            update_req = UpdateCameraRequest(
                name="Vehicle Gate Updated",
                status="disabled"
            )
            res = update_camera("CAM_02", update_req)
            self.assertEqual(res["status"], "success")

            # Verify through get_configured_cameras
            res = get_configured_cameras()
            cams = res["cameras"]
            cam2 = next(c for c in cams if c["id"] == "CAM_02")
            self.assertEqual(cam2["name"], "Vehicle Gate Updated")
            self.assertEqual(cam2["status"], "disabled")

            # 3. POST /api/cameras/CAM_02/toggle
            toggle_req = ToggleCameraRequest(enabled=True)
            res = toggle_camera("CAM_02", toggle_req)
            self.assertEqual(res["new_status"], "active")

            # 4. DELETE /api/cameras/CAM_02
            res = delete_camera("CAM_02")
            self.assertIn("deleted successfully", res["message"])

            # Verify CAM_02 is gone
            res = get_configured_cameras()
            cams = res["cameras"]
            self.assertEqual(len(cams), 1)
            self.assertEqual(cams[0]["id"], "CAM_01")

    def test_clear_alerts_endpoint(self):
        from datetime import datetime
        import uuid
        from central.main import clear_alerts

        db = SessionLocal()
        # Seed test alerts
        event_1 = str(uuid.uuid4())
        event_2 = str(uuid.uuid4())
        test_snapshot = os.path.join(self.tmp_dir.name, "evidence_snapshot.jpg")
        with open(test_snapshot, "w") as f:
            f.write("fake-image-bytes")

        try:
            alert1 = Alert(
                event_id=event_1,
                node_id="CAM_01",
                event_type="UNAUTHORIZED_PRESENCE",
                severity="critical",
                details="Test Alert 1",
                lat=28.6139,
                lng=77.2090,
                snapshot_path=test_snapshot,
                timestamp=datetime.utcnow()
            )
            alert2 = Alert(
                event_id=event_2,
                node_id="CAM_02",
                event_type="LOITERING",
                severity="high",
                details="Test Alert 2",
                lat=28.6145,
                lng=77.2085,
                snapshot_path=test_snapshot,
                timestamp=datetime.utcnow()
            )
            db.add(alert1)
            db.add(alert2)
            db.commit()

            # Verify seeded
            count_before = db.query(Alert).filter(Alert.event_id.in_([event_1, event_2])).count()
            self.assertEqual(count_before, 2)

            # Call clear_alerts
            res = clear_alerts(db=db)
            self.assertEqual(res["status"], "success")
            self.assertGreaterEqual(res["deleted_count"], 2)

            # Verify alerts table is now cleared
            count_after = db.query(Alert).count()
            self.assertEqual(count_after, 0)

            # Verify calling clear_alerts again when 0 alerts returns gracefully
            res_empty = clear_alerts(db=db)
            self.assertEqual(res_empty["status"], "success")
            self.assertEqual(res_empty["deleted_count"], 0)

            # Verify snapshot file on disk was NOT deleted
            self.assertTrue(os.path.exists(test_snapshot))

            # Verify camera config in YAML is completely untouched
            cfg = load_config(self.config_path)
            self.assertEqual(len(cfg["cameras"]), 2)
            self.assertEqual(cfg["cameras"][0]["id"], "CAM_01")
            self.assertEqual(cfg["cameras"][1]["id"], "CAM_02")

        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
