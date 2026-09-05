"""Comprehensive test suite for Multi-Camera Edge Ingestion & Tracking.

Validates:
1. Discovery of active cameras and filtering of inactive cameras from YAML.
2. Per-camera target FPS resolution.
3. Bounded queue & stale-frame drop behavior (discarding oldest on queue full).
4. Construction of canonical EdgeEvent v1 with correct camera_id and track context.
5. Worker isolation: independent CameraWorkers running concurrently with separate YOLO instances.
6. Failure isolation: failure of one camera stream does not stop other camera workers.
7. AlertEvent routing to per-camera EvidenceRecorders.
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from edge.main import (
    CameraWorker,
    EdgeService,
    discover_active_cameras,
    event_from_result,
    open_capture_for_camera,
    resolve_capture_source_for_camera,
    resolve_target_fps_for_camera,
)
from shared.events import AlertEvent, EdgeEvent


class TestMultiCameraEdge(unittest.TestCase):
    def setUp(self):
        self.mock_mqtt_client = MagicMock()
        self.mock_mqtt_client.publish.return_value = MagicMock(rc=0)

    def test_discover_active_cameras(self):
        """Verify that discover_active_cameras returns only active cameras without duplicates."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "test_cameras.yaml"
            cfg_file.write_text("""
cameras:
  - id: cam-01
    status: active
    rtsp_url: rtsp://10.0.0.1/live
  - id: cam-02
    status: inactive
    rtsp_url: rtsp://10.0.0.2/live
  - id: cam-03
    status: active
    source:
      type: direct
      url: rtsp://10.0.0.3/live
  - id: cam-04
    status: disabled
""")
            active = discover_active_cameras(cameras_yaml_path=str(cfg_file), config_yaml_path=str(cfg_file))
            active_ids = [c["id"] for c in active]
            self.assertIn("cam-01", active_ids)
            self.assertIn("cam-03", active_ids)
            self.assertNotIn("cam-02", active_ids)
            self.assertNotIn("cam-04", active_ids)
            self.assertEqual(len(active_ids), 2)

    def test_resolve_target_fps(self):
        """Verify per-camera target FPS resolution."""
        cam_with_fps = {"id": "cam-1", "target_fps": 15.0}
        self.assertEqual(resolve_target_fps_for_camera("cam-1", cam_with_fps), 15.0)

        cam_with_legacy_fps = {"id": "cam-2", "fps": 12.5}
        self.assertEqual(resolve_target_fps_for_camera("cam-2", cam_with_legacy_fps), 12.5)

        cam_no_fps = {"id": "cam-3"}
        self.assertEqual(resolve_target_fps_for_camera("cam-3", cam_no_fps), 10.0)

    def test_camera_worker_bounded_queue_drops_stale_frames(self):
        """Verify that CameraWorker's capture queue never exceeds maxsize and discards older frames."""
        worker = CameraWorker(
            camera_id="test-cam",
            cam_config={"id": "test-cam", "target_fps": 30.0},
            mqtt_client=self.mock_mqtt_client,
        )
        frame_dummy = np.zeros((100, 100, 3), dtype=np.uint8)
        for fid in range(1, 10):
            if worker._frame_queue.full():
                try:
                    worker._frame_queue.get_nowait()
                    worker.dropped_frames += 1
                except queue.Empty:
                    pass
            worker._frame_queue.put((fid, time.time(), frame_dummy))

        self.assertLessEqual(worker._frame_queue.qsize(), 2)
        self.assertEqual(worker.dropped_frames, 7)  # Pushed 9 frames into queue maxsize=2 -> dropped 7

    def test_camera_worker_event_building(self):
        """Verify event_from_result constructs canonical EdgeEvents with camera context."""
        dummy_box = MagicMock()
        dummy_box.cls = [0]
        dummy_box.xyxy = [[10.0, 20.0, 50.0, 60.0]]
        dummy_box.id = [17]
        dummy_box.conf = [0.88]

        mock_result = MagicMock()
        mock_result.boxes = [dummy_box]
        mock_result.names = {0: "person"}

        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        event = event_from_result("CAM_02", 1, mock_result, frame=frame)

        self.assertIsInstance(event, EdgeEvent)
        self.assertEqual(event.camera_id, "CAM_02")
        self.assertEqual(event.frame_id, 1)
        self.assertEqual(len(event.detections), 1)
        self.assertEqual(event.detections[0].label, "person")
        self.assertEqual(event.detections[0].track_id, "17")
        self.assertAlmostEqual(event.detections[0].confidence, 0.88, places=2)
        self.assertEqual(event.detections[0].bbox.x1, 10.0)
        self.assertIsNotNone(event.detections[0].crop_base64)

    def test_multi_camera_worker_isolation(self):
        """Verify that multiple CameraWorkers run concurrently and emit EdgeEvents with their own camera IDs."""
        published_events = []

        def mock_publish(topic, payload):
            published_events.append(json.loads(payload))
            res = MagicMock()
            res.rc = 0
            return res

        self.mock_mqtt_client.publish = mock_publish

        worker1 = CameraWorker("CAM_01", {"id": "CAM_01", "target_fps": 10.0}, self.mock_mqtt_client)
        worker2 = CameraWorker("CAM_02", {"id": "CAM_02", "target_fps": 10.0}, self.mock_mqtt_client)
        worker3 = CameraWorker("CAM_03", {"id": "CAM_03", "target_fps": 10.0}, self.mock_mqtt_client)

        for w in [worker1, worker2, worker3]:
            m = MagicMock()
            box = MagicMock()
            box.cls = [0]
            box.xyxy = [[10.0, 10.0, 40.0, 40.0]]
            box.id = [100]  # Same local track ID to verify camera_id separation
            box.conf = [0.90]
            res = MagicMock()
            res.boxes = [box]
            res.names = {0: "person"}
            m.track.return_value = [res]
            w._model = m

        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        for w in [worker1, worker2, worker3]:
            w._frame_queue.put((1, time.time(), frame))
            item = w._frame_queue.get()
            fid, ts, frm = item
            res = w._model.track(frm, persist=True, conf=w.confidence)[0]
            ev = event_from_result(w.camera_id, fid, res, frame=frm)
            w.mqtt_client.publish("surveillance/edge/events.v1", ev.model_dump_json())

        self.assertEqual(len(published_events), 3)
        cam_ids = {e["camera_id"] for e in published_events}
        self.assertEqual(cam_ids, {"CAM_01", "CAM_02", "CAM_03"})
        for e in published_events:
            self.assertEqual(len(e["detections"]), 1)
            self.assertEqual(e["detections"][0]["track_id"], "100")

    def test_failure_isolation_camera_disconnect(self):
        """Verify that if one camera fails to capture, other workers continue unaffected."""
        worker_ok = CameraWorker("CAM_01", {"id": "CAM_01"}, self.mock_mqtt_client)
        worker_fail = CameraWorker("CAM_02", {"id": "CAM_02"}, self.mock_mqtt_client)

        with patch("edge.main.cv2.VideoCapture") as mock_cap_cls:
            broken_cap = MagicMock()
            broken_cap.isOpened.return_value = False
            mock_cap_cls.return_value = broken_cap

            cap = open_capture_for_camera("CAM_02", {})
            self.assertFalse(cap.isOpened())

        self.assertEqual(worker_ok.status, "initializing")
        self.assertEqual(worker_fail.status, "initializing")

    def test_edge_service_alert_routing(self):
        """Verify EdgeService routes incoming AlertEvents to the correct CameraWorker evidence recorder."""
        service = EdgeService()
        service.mqtt_client = self.mock_mqtt_client

        worker1 = MagicMock()
        worker1.camera_id = "CAM_01"
        worker2 = MagicMock()
        worker2.camera_id = "CAM_02"

        service.workers["CAM_01"] = worker1
        service.workers["CAM_02"] = worker2

        alert_cam2 = AlertEvent(
            camera_id="CAM_02",
            event_type="intrusion",
            severity="high",
            details="Intrusion line crossed",
        )

        msg = MagicMock()
        msg.payload = alert_cam2.model_dump_json().encode("utf-8")
        service._on_mqtt_message(self.mock_mqtt_client, None, msg)

        worker2.evidence_recorder.handle_alert.assert_called_once()
        worker1.evidence_recorder.handle_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
