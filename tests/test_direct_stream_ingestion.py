import unittest
from unittest.mock import MagicMock, patch
import tempfile
import os
import numpy as np

from ingestion.onvif.security import validate_stream_url, sanitize_url, ALLOWED_STREAM_SCHEMES
from ingestion.onvif.resolver import (
    inject_credentials_into_url,
    resolve_camera_source,
    clear_resolver_cache
)
from ingestion.stream import probe_stream, RTSPConnector
from ingestion.config import append_camera_config, load_config


class TestDirectStreamIngestion(unittest.TestCase):
    """
    Focused tests for direct stream IP camera ingestion:
    - valid RTSP URL
    - valid HTTP MJPEG URL
    - valid HTTPS URL
    - malformed URL
    - unsupported scheme
    - URL credentials handling
    - separate username/password handling
    - SSRF protection
    - duplicate/invalid camera configuration
    - Test Connection success using a mocked/local stream
    - Test Connection failure
    - existing ONVIF camera configuration remains valid
    - existing legacy/manual RTSP configuration remains valid
    """

    def setUp(self):
        clear_resolver_cache()

    def tearDown(self):
        clear_resolver_cache()

    def test_valid_rtsp_url(self):
        url = "rtsp://192.168.1.50:554/stream"
        self.assertTrue(validate_stream_url(url))

    def test_valid_http_mjpeg_url(self):
        url = "http://192.168.1.50/mjpeg"
        self.assertTrue(validate_stream_url(url))

    def test_valid_https_url(self):
        url = "https://192.168.1.50/video"
        self.assertTrue(validate_stream_url(url))

    def test_malformed_url(self):
        malformed_cases = [
            "not_a_valid_url",
            "://missing-scheme",
            "rtsp://",
            "http://:8080/path",
            "",
            "   ",
            "rtsp://[invalid-host",
            None
        ]
        for bad_url in malformed_cases:
            self.assertFalse(validate_stream_url(bad_url), msg=f"Should reject {bad_url}")

    def test_unsupported_scheme(self):
        unsupported = [
            "ftp://192.168.1.50/stream",
            "file:///etc/shadow",
            "ssh://192.168.1.50:22",
            "gopher://192.168.1.50",
            "ws://192.168.1.50/stream"
        ]
        for url in unsupported:
            self.assertFalse(validate_stream_url(url), msg=f"Should reject scheme for {url}")

    def test_url_credentials_handling_and_sanitization(self):
        url_with_creds = "rtsp://admin:superSecret123@192.168.1.50:554/stream1"
        self.assertTrue(validate_stream_url(url_with_creds))

        sanitized = sanitize_url(url_with_creds)
        self.assertNotIn("superSecret123", sanitized)
        self.assertIn("admin:***@192.168.1.50:554/stream1", sanitized)

    def test_separate_username_password_handling(self):
        base_url = "rtsp://192.168.1.50:554/h264Preview_01_main"
        injected = inject_credentials_into_url(base_url, "operator", "p@ss:w/ord")
        self.assertIn("operator:p%40ss%3Aw%2Ford@192.168.1.50:554", injected)

        # Ensure sanitization masks the credentials
        sanitized = sanitize_url(injected)
        self.assertNotIn("p@ss", sanitized)
        self.assertIn("operator:***@192.168.1.50:554", sanitized)

    def test_ssrf_protection(self):
        blocked_targets = [
            "http://169.254.169.254/latest/meta-data/",
            "rtsp://169.254.1.1:554/stream",
            "http://0.0.0.0:80/stream",
            "http://metadata.google.internal/computeMetadata/v1/",
            "https://169.254.169.254/secret"
        ]
        for target in blocked_targets:
            self.assertFalse(validate_stream_url(target), msg=f"Should block SSRF target {target}")

    def test_probe_stream_success_mocked(self):
        with patch("cv2.VideoCapture") as mock_cap_cls:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
            mock_cap.read.return_value = (True, mock_frame)
            mock_cap.get.side_effect = lambda prop: {
                3: 1920,
                4: 1080,
                5: 25.0
            }.get(prop, 0)
            mock_cap_cls.return_value = mock_cap

            result = probe_stream("rtsp://192.168.1.50:554/stream", timeout=2.0)
            self.assertTrue(result["connected"])
            self.assertEqual(result["protocol"], "RTSP")
            self.assertEqual(result["resolution"], "1920x1080")
            self.assertEqual(result["fps"], 25.0)

    def test_probe_stream_failure_mocked(self):
        with patch("cv2.VideoCapture") as mock_cap_cls:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = False
            mock_cap_cls.return_value = mock_cap

            result = probe_stream("rtsp://secret_user:secret_pass@192.168.1.99:554/live", timeout=1.0)
            self.assertFalse(result["connected"])
            self.assertIn("error", result)
            # Ensure error does not leak credentials
            self.assertNotIn("secret_pass", result["error"])
            self.assertNotIn("secret_user", result["error"])

    def test_probe_stream_non_persistent(self):
        # Probing should not write to any config file
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"cameras:\n  - id: CAM_01\n    source:\n      type: rtsp\n")
            temp_config = f.name

        try:
            with patch("cv2.VideoCapture") as mock_cap_cls:
                mock_cap = MagicMock()
                mock_cap.isOpened.return_value = True
                mock_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
                mock_cap.read.return_value = (True, mock_frame)
                mock_cap.get.return_value = 0
                mock_cap_cls.return_value = mock_cap

                result = probe_stream("http://192.168.1.75/mjpeg", timeout=1.0)
                self.assertTrue(result["connected"])

            with open(temp_config, "r") as f:
                content = f.read()
            self.assertNotIn("192.168.1.75", content)
        finally:
            if os.path.exists(temp_config):
                os.remove(temp_config)

    def test_existing_onvif_camera_configuration_remains_valid(self):
        onvif_config = {
            "id": "cam-border-onvif",
            "source": {
                "type": "onvif",
                "host": "192.168.1.188",
                "port": 80,
                "username": "admin",
                "password": "password123"
            }
        }
        with patch("ingestion.onvif.resolver.ONVIFCameraClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.probe_camera.return_value = {
                "status": "online",
                "rtsp_url": "rtsp://192.168.1.188:554/onvif1"
            }
            mock_client_cls.return_value = mock_client
            resolved_url, meta = resolve_camera_source(onvif_config)
            self.assertTrue(resolved_url.startswith("rtsp://"))
            self.assertIn("admin:password123@192.168.1.188:554/onvif1", resolved_url)

    def test_existing_legacy_manual_rtsp_remains_valid(self):
        legacy_config = {
            "id": "cam-manual-rtsp",
            "source": {
                "type": "rtsp",
                "host": "192.168.1.200",
                "port": 554,
                "path": "/live/ch0",
                "username": "viewer",
                "password": "pass"
            }
        }
        resolved_url, meta = resolve_camera_source(legacy_config)
        self.assertEqual(resolved_url, "rtsp://viewer:pass@192.168.1.200:554/live/ch0")
        self.assertEqual(meta.get("type"), "rtsp")

    def test_direct_stream_resolver(self):
        direct_config = {
            "id": "cam-direct-stream",
            "source": {
                "type": "direct",
                "url": "https://192.168.1.88/stream.mjpg",
                "username": "admin",
                "password": "pass"
            }
        }
        resolved_url, meta = resolve_camera_source(direct_config)
        self.assertEqual(resolved_url, "https://admin:pass@192.168.1.88/stream.mjpg")
        self.assertEqual(meta.get("type"), "direct")

    def test_append_camera_config_persistence_and_deduplication(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"cameras:\n  - id: CAM_01\n    rtsp_url: rtsp://192.168.1.10/live\n")
            temp_config = f.name

        try:
            # 1. Append CAM_02
            append_camera_config(
                camera_id="CAM_02",
                ip_address="192.168.1.20",
                rtsp_url="rtsp://192.168.1.20/live",
                source={"type": "direct", "url": "rtsp://192.168.1.20/live"},
                name="PERIMETER_GATE",
                config_path=temp_config
            )

            data = load_config(temp_config)
            cam_ids = [c.get("id") for c in data.get("cameras", [])]
            self.assertIn("CAM_01", cam_ids)
            self.assertIn("CAM_02", cam_ids)

            cam_02 = next(c for c in data["cameras"] if c.get("id") == "CAM_02")
            self.assertEqual(cam_02["rtsp_url"], "rtsp://192.168.1.20/live")
            self.assertEqual(cam_02["name"], "PERIMETER_GATE")
            self.assertEqual(cam_02["source"]["type"], "direct")

            # 2. Update CAM_02 (no duplicate entry created)
            append_camera_config(
                camera_id="CAM_02",
                ip_address="192.168.1.25",
                rtsp_url="rtsp://192.168.1.25/live_updated",
                source={"type": "direct", "url": "rtsp://192.168.1.25/live_updated"},
                name="PERIMETER_GATE_UPDATED",
                config_path=temp_config
            )

            data_updated = load_config(temp_config)
            matching = [c for c in data_updated["cameras"] if c.get("id") == "CAM_02"]
            self.assertEqual(len(matching), 1, "Should deduplicate and update existing camera ID")
            self.assertEqual(matching[0]["rtsp_url"], "rtsp://192.168.1.25/live_updated")
            self.assertEqual(matching[0]["name"], "PERIMETER_GATE_UPDATED")
        finally:
            if os.path.exists(temp_config):
                os.remove(temp_config)

    def test_api_test_connection_endpoint_success(self):
        from central.main import test_camera_connection, TestConnectionRequest
        with patch("ingestion.stream.probe_stream") as mock_probe:
            mock_probe.return_value = {
                "connected": True,
                "protocol": "RTSP",
                "resolution": "1920x1080",
                "fps": 25.0,
                "sanitized_url": "rtsp://admin:***@192.168.1.50:554/stream",
                "message": "Connected"
            }
            req = TestConnectionRequest(
                stream_url="rtsp://192.168.1.50:554/stream",
                username="admin",
                password="secret_password"
            )
            res = test_camera_connection(req)
            self.assertTrue(res["connected"])
            self.assertEqual(res["protocol"], "RTSP")
            self.assertEqual(res["resolution"], "1920x1080")
            self.assertEqual(res["fps"], 25.0)
            self.assertNotIn("secret_password", str(res))

    def test_api_test_connection_endpoint_ssrf_blocked(self):
        from central.main import test_camera_connection, TestConnectionRequest
        from fastapi import HTTPException
        req = TestConnectionRequest(stream_url="http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(HTTPException) as ctx:
            test_camera_connection(req)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("SSRF protection", ctx.exception.detail)

    def test_api_connect_camera_direct_url_sanitizes_credentials(self):
        from central.main import connect_camera, ConnectCameraRequest
        with patch("central.main.append_camera_config") as mock_append, \
             patch("central.main.RTSPConnector.validate_connection", return_value=True), \
             patch("central.main.RTSPConnector.start", return_value=True):

            req = ConnectCameraRequest(
                camera_id="CAM_TEST_DIRECT",
                stream_url="rtsp://192.168.1.90:554/live",
                username="operator",
                password="camera_top_secret_pass",
                target_slot="Camera Slot 2"
            )
            res = connect_camera(req)
            self.assertEqual(res["status"], "success")
            # Password must NEVER appear in response
            self.assertNotIn("camera_top_secret_pass", str(res))
            self.assertIn("***", res["rtsp_url"])

            # Verify append_camera_config was called with direct source dict
            self.assertTrue(mock_append.called)
            called_args, called_kwargs = mock_append.call_args
            self.assertEqual(called_kwargs.get("camera_id") or called_args[0], "CAM_TEST_DIRECT")
            source = called_kwargs.get("source")
            self.assertIsNotNone(source)
            self.assertEqual(source["type"], "direct")
            self.assertEqual(source["url"], "rtsp://192.168.1.90:554/live")

    def test_test_connection_request_flexible_types(self):
        from central.main import TestConnectionRequest
        # Null username, null password, string port must not throw Pydantic ValidationError
        req = TestConnectionRequest(
            stream_url="rtsp://192.168.1.50:554/stream",
            username=None,
            password=None,
            port="554",
            rtsp_path=None
        )
        self.assertEqual(req.stream_url, "rtsp://192.168.1.50:554/stream")
        self.assertIsNone(req.username)
        self.assertIsNone(req.password)
        self.assertEqual(req.port, "554")

    def test_connect_camera_request_flexible_types(self):
        from central.main import ConnectCameraRequest
        req = ConnectCameraRequest(
            camera_id="CAM_02",
            stream_url="rtsp://192.168.1.50:554/stream",
            username=None,
            password=None,
            port="554",
            rtsp_path=None
        )
        self.assertEqual(req.camera_id, "CAM_02")
        self.assertIsNone(req.username)
        self.assertIsNone(req.password)
        self.assertEqual(req.port, "554")

    def test_active_connectors_dual_keying_and_lookup(self):
        from central.main import connect_camera, ConnectCameraRequest, video_feed_slot, active_connectors
        active_connectors.clear()

        with patch("central.main.append_camera_config"), \
             patch("central.main.RTSPConnector.validate_connection", return_value=True), \
             patch("central.main.RTSPConnector.start", return_value=True):

            req = ConnectCameraRequest(
                camera_id="CAM_02",
                stream_url="rtsp://192.168.1.50:554/stream",
                target_slot="Camera Slot 2"
            )
            connect_camera(req)

            # Confirm both target_slot and camera_id are present in active_connectors
            self.assertIn("Camera Slot 2", active_connectors)
            self.assertIn("CAM_02", active_connectors)

            # Both lookups should return StreamingResponse without raising 404
            res_slot = video_feed_slot("Camera Slot 2")
            self.assertIsNotNone(res_slot)

            res_id = video_feed_slot("CAM_02")
            self.assertIsNotNone(res_id)


if __name__ == "__main__":
    unittest.main()

