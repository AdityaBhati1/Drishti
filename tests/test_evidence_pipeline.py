"""Tests for evidence capture: event snapshots, rolling buffer video clips, concurrency, and persistence."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"


from central import database
from central import main as central
from edge.evidence import EvidenceRecorder
from shared.events import AlertEvent


class EvidencePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cctv_evidence_test_")
        self.storage_dir = Path(self.temp_dir) / "snapshots"
        self.recorder = EvidenceRecorder(
            camera_id="cam-test-evidence",
            storage_dir=str(self.storage_dir),
            snapshots_enabled=True,
            clips_enabled=True,
            format_ext="jpg",
            quality=90,
            pre_event_seconds=1.0,
            post_event_seconds=1.0,
            max_buffer_seconds=5.0,
            target_fps=10.0,
        )

        # Set up SQLite database
        database.Base.metadata.create_all(bind=database.engine)
        session = database.SessionLocal()
        try:
            session.query(database.Alert).delete()
            session.commit()
        finally:
            session.close()

    def tearDown(self):
        self.recorder.close()
        # Allow background threads to finish writing
        time.sleep(0.1)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_synthetic_camera_frame(self, color=(100, 150, 200), text="TEST"):
        """Generates an actual 640x480 3-channel frame representing camera input."""
        frame = np.full((480, 640, 3), color, dtype=np.uint8)
        cv2.putText(frame, text, (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2)
        return frame

    def test_actual_frame_produces_snapshot(self):
        """Verify an actual frame captured by camera produces a valid JPEG snapshot on disk."""
        frame = self._create_synthetic_camera_frame(color=(0, 200, 0), text="CAMERA_1_REAL_FRAME")
        now = time.time()
        self.recorder.add_frame(frame, timestamp=now, frame_id=101)

        rel_path = self.recorder.capture_snapshot(event_id="evt-001", timestamp=now)
        self.assertIsNotNone(rel_path)
        self.assertEqual(rel_path, "snapshots/cam-test-evidence_evt-001.jpg")

        # Wait for async worker thread
        full_path = self.storage_dir / "cam-test-evidence_evt-001.jpg"
        for _ in range(20):
            if full_path.exists() and full_path.stat().st_size > 0:
                break
            time.sleep(0.05)

        self.assertTrue(full_path.exists(), f"Snapshot file not created at {full_path}")
        self.assertGreater(full_path.stat().st_size, 1000)

        # Confirm image is valid and readable by OpenCV
        loaded = cv2.imread(str(full_path))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape, (480, 640, 3))

    def test_snapshot_associated_with_correct_alert_event(self):
        """Verify snapshot and clip references are deterministically associated with an AlertEvent."""
        frame = self._create_synthetic_camera_frame(text="ALERT_TARGET")
        now = time.time()
        self.recorder.add_frame(frame, timestamp=now, frame_id=1)

        event = AlertEvent(
            camera_id="cam-test-evidence",
            event_type="intrusion",
            severity="high",
            details="Person entered unauthorized zone",
        )
        snap, clip = self.recorder.handle_alert(event)

        self.assertIsNotNone(snap)
        self.assertIsNotNone(clip)
        self.assertEqual(snap, f"snapshots/cam-test-evidence_{event.event_id}.jpg")
        self.assertEqual(clip, f"snapshots/clips/cam-test-evidence_{event.event_id}.mp4")

        # Confirm wrong camera ID does not trigger capture
        wrong_event = AlertEvent(
            camera_id="cam-other",
            event_type="intrusion",
            severity="high",
            details="Other camera event",
        )
        other_snap, other_clip = self.recorder.handle_alert(wrong_event)
        self.assertIsNone(other_snap)
        self.assertIsNone(other_clip)

    def test_rolling_buffer_frames_can_produce_clip(self):
        """Verify frames in rolling buffer are encoded into a valid MP4 video clip."""
        now = time.time()
        # Feed 10 pre-event frames (1 second at 10 fps)
        for i in range(10):
            t = now - 1.0 + (i * 0.1)
            frame = self._create_synthetic_camera_frame(text=f"PRE_FRAME_{i}")
            self.recorder.add_frame(frame, timestamp=t, frame_id=i)

        # Trigger clip at time 'now'
        clip_path = self.recorder.start_clip_recording(event_id="evt-clip-1", timestamp=now, fps=10.0)
        self.assertEqual(clip_path, "snapshots/clips/cam-test-evidence_evt-clip-1.mp4")

        # Feed 10 post-event frames (1 second at 10 fps)
        for i in range(10):
            t = now + ((i + 1) * 0.1)
            frame = self._create_synthetic_camera_frame(text=f"POST_FRAME_{i}")
            self.recorder.add_frame(frame, timestamp=t, frame_id=10 + i)

        # Allow time for async post-event completion and video encoding
        full_clip_path = self.storage_dir / "clips" / "cam-test-evidence_evt-clip-1.mp4"
        for _ in range(60):
            if full_clip_path.exists() and full_clip_path.stat().st_size >= 500:
                break
            time.sleep(0.05)

        self.assertTrue(full_clip_path.exists(), f"Video clip file not created at {full_clip_path}")
        self.assertGreater(full_clip_path.stat().st_size, 500)

        # Verify video can be opened with cv2.VideoCapture and contains frames
        cap = cv2.VideoCapture(str(full_clip_path))
        self.assertTrue(cap.isOpened(), "cv2.VideoCapture could not open recorded clip")
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        self.assertEqual(w, 640)
        self.assertEqual(h, 480)
        self.assertGreaterEqual(frame_count, 15, f"Expected at least 15 frames, got {frame_count}")

    def test_pre_post_event_timing(self):
        """Verify pre-event and post-event duration window includes expected frames."""
        event_time = 1000.0
        # Add frames from t=998.0 to t=1002.0 at 0.2s intervals
        for i in range(21):
            t = 998.0 + (i * 0.2)
            frame = self._create_synthetic_camera_frame(text=f"T_{t:.1f}")
            self.recorder.add_frame(frame, timestamp=t, frame_id=i)

        # Pre-event is 1.0s, post-event is 1.0s
        # Pre-event interval: [999.0, 1000.0] -> 6 frames (999.0, 999.2, 999.4, 999.6, 999.8, 1000.0)
        # Post-event interval: (1000.0, 1001.0] -> 5 frames (1000.2, 1000.4, 1000.6, 1000.8, 1001.0)
        self.recorder.start_clip_recording(event_id="timed-clip", timestamp=event_time, fps=10.0)

        # Feed the post-event frames (simulating real-time arrival)
        for i in range(1, 6):
            t = event_time + (i * 0.2)
            frame = self._create_synthetic_camera_frame(text=f"POST_T_{t:.1f}")
            self.recorder.add_frame(frame, timestamp=t, frame_id=21 + i)

        full_clip_path = self.storage_dir / "clips" / "cam-test-evidence_timed-clip.mp4"
        for _ in range(30):
            if full_clip_path.exists() and full_clip_path.stat().st_size > 0:
                break
            time.sleep(0.05)

        self.assertTrue(full_clip_path.exists())
        cap = cv2.VideoCapture(str(full_clip_path))
        self.assertTrue(cap.isOpened())
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        self.assertGreaterEqual(frame_count, 10)

    def test_concurrent_alerts_dont_overwrite_evidence(self):
        """Verify multiple simultaneous alerts capture unique non-colliding evidence files."""
        now = time.time()
        frame = self._create_synthetic_camera_frame(text="CONCURRENT_BASE")
        self.recorder.add_frame(frame, timestamp=now, frame_id=1)

        event_a = AlertEvent(
            camera_id="cam-test-evidence",
            event_type="intrusion",
            severity="high",
            details="Alert A",
        )
        event_b = AlertEvent(
            camera_id="cam-test-evidence",
            event_type="restricted_zone",
            severity="critical",
            details="Alert B",
        )

        snap_a, clip_a = self.recorder.handle_alert(event_a)
        snap_b, clip_b = self.recorder.handle_alert(event_b)

        self.assertNotEqual(snap_a, snap_b)
        self.assertNotEqual(clip_a, clip_b)

        # Flush clips
        self.recorder.flush_active_clips()

        path_snap_a = self.storage_dir / f"cam-test-evidence_{event_a.event_id}.jpg"
        path_snap_b = self.storage_dir / f"cam-test-evidence_{event_b.event_id}.jpg"

        for _ in range(30):
            if path_snap_a.exists() and path_snap_b.exists():
                break
            time.sleep(0.05)

        self.assertTrue(path_snap_a.exists())
        self.assertTrue(path_snap_b.exists())
        self.assertGreater(path_snap_a.stat().st_size, 0)
        self.assertGreater(path_snap_b.stat().st_size, 0)

    def test_missing_storage_handled_safely(self):
        """Verify unwritable or invalid storage path fails gracefully without unhandled exceptions."""
        unwritable_recorder = EvidenceRecorder(
            camera_id="cam-unwritable",
            storage_dir="/non_existent_drive_or_readonly_root/evidence_dir",
            snapshots_enabled=True,
            clips_enabled=True,
        )
        with patch.object(unwritable_recorder, "_ensure_storage", return_value=False):
            frame = self._create_synthetic_camera_frame()
            unwritable_recorder.add_frame(frame)
            snap = unwritable_recorder.capture_snapshot(event_id="err-snap")
            clip = unwritable_recorder.start_clip_recording(event_id="err-clip")
            self.assertIsNone(snap)
            self.assertIsNone(clip)
            unwritable_recorder.close()

    def test_evidence_references_survive_database_persistence(self):
        """Verify snapshot_path and clip_path persist cleanly in PostgreSQL/SQLite and retrieve faithfully."""
        event = AlertEvent(
            camera_id="cam-test-evidence",
            event_type="anpr_match",
            severity="high",
            details="Watchlist plate detected: ABC1234",
            snapshot_path="snapshots/cam-test-evidence_anpr-1.jpg",
            metadata={"clip_path": "snapshots/clips/cam-test-evidence_anpr-1.mp4"},
        )

        session = database.SessionLocal()
        try:
            alert, created = central.persist_alert_event(event, session)
            self.assertTrue(created)

            row = session.query(database.Alert).filter(database.Alert.event_id == str(event.event_id)).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.snapshot_path, "snapshots/cam-test-evidence_anpr-1.jpg")
            self.assertEqual(row.clip_path, "snapshots/clips/cam-test-evidence_anpr-1.mp4")
        finally:
            session.close()

    def test_central_api_and_static_serving_returns_evidence(self):
        """Verify evidence written through Edge recorder is retrievable via Central API and MP4 is playable."""
        import asyncio
        import httpx

        now = time.time()
        # Feed pre-event frames into the actual Edge recorder
        for i in range(10):
            frame = self._create_synthetic_camera_frame(color=(50, 100, 150), text=f"REAL_E2E_{i}")
            self.recorder.add_frame(frame, timestamp=now - 0.5 + (i * 0.05), frame_id=i)

        event = AlertEvent(
            camera_id="cam-test-evidence",
            event_type="face_match",
            severity="critical",
            details="VIP identified",
        )

        # Trigger snapshot and clip creation through the actual Edge recorder
        snap_path, clip_path = self.recorder.handle_alert(event)
        self.assertIsNotNone(snap_path)
        self.assertIsNotNone(clip_path)

        # Feed post-event frames
        for i in range(10):
            frame = self._create_synthetic_camera_frame(color=(50, 100, 150), text=f"REAL_POST_{i}")
            self.recorder.add_frame(frame, timestamp=now + ((i + 1) * 0.05), frame_id=10 + i)

        # Wait for async snapshot and video encoding to finish on disk
        self.recorder.flush_active_clips()
        full_snap = self.storage_dir / f"cam-test-evidence_{event.event_id}.jpg"
        full_clip = self.storage_dir / "clips" / f"cam-test-evidence_{event.event_id}.mp4"
        for _ in range(40):
            if full_snap.exists() and full_clip.exists() and full_clip.stat().st_size > 500:
                break
            time.sleep(0.05)

        self.assertTrue(full_snap.exists(), f"Snapshot not written by recorder at {full_snap}")
        self.assertTrue(full_clip.exists(), f"Clip not written by recorder at {full_clip}")

        event.snapshot_path = snap_path
        event.metadata["clip_path"] = clip_path

        session = database.SessionLocal()
        try:
            central.persist_alert_event(event, session)
        finally:
            session.close()

        from dataclasses import replace

        # Test through Central's HTTP API and verify exact paths returned
        new_settings = replace(central.settings, snapshots_dir=str(self.storage_dir))
        with patch.object(central, "settings", new_settings):
            async def _test_api():
                transport = httpx.ASGITransport(app=central.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.get("/api/alerts")
                    self.assertEqual(resp.status_code, 200)
                    items = resp.json()
                    self.assertIsInstance(items, list)
                    matched = [a for a in items if a.get("event_id") == str(event.event_id)]
                    self.assertEqual(len(matched), 1)
                    alert_dict = matched[0]

                    api_snap_url = alert_dict.get("snapshot_path")
                    api_clip_url = alert_dict.get("clip_path")
                    self.assertTrue(api_snap_url.startswith("/snapshots/"))
                    self.assertTrue(api_clip_url.startswith("/snapshots/clips/"))

                    # Retrieve snapshot from Central using the exact URL path exposed by Central
                    snap_resp = await client.get(api_snap_url)
                    self.assertEqual(snap_resp.status_code, 200)
                    self.assertEqual(snap_resp.headers.get("content-type"), "image/jpeg")
                    self.assertGreater(len(snap_resp.content), 1000)

                    # Retrieve video clip from Central using the exact URL path exposed by Central
                    clip_resp = await client.get(api_clip_url)
                    self.assertEqual(clip_resp.status_code, 200)
                    self.assertEqual(clip_resp.headers.get("content-type"), "video/mp4")
                    self.assertGreater(len(clip_resp.content), 500)

                    # Verify MP4 is playable, valid, and non-zero frames
                    tmp_clip_path = Path(self.temp_dir) / "retrieved_verify.mp4"
                    tmp_clip_path.write_bytes(clip_resp.content)
                    cap = cv2.VideoCapture(str(tmp_clip_path))
                    self.assertTrue(cap.isOpened(), "Retrieved MP4 video could not be opened by VideoCapture")
                    frames_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()

                    self.assertGreater(frames_count, 0, f"MP4 video clip has zero frames! Got {frames_count}")
                    self.assertEqual(width, 640)
                    self.assertEqual(height, 480)

            asyncio.run(_test_api())

    def test_e2e_authenticated_edge_evidence_retrieval_separate_hosts(self):
        """Verify Central retrieves evidence over authenticated HTTP from Edge when storage is not shared."""
        import asyncio
        import httpx

        edge_temp = tempfile.mkdtemp(prefix="edge_isolated_")
        central_temp = tempfile.mkdtemp(prefix="central_isolated_")
        edge_storage = Path(edge_temp) / "snapshots"
        central_storage = Path(central_temp) / "snapshots"
        edge_port = 8123

        try:
            # Edge recorder running with its own isolated storage directory
            edge_recorder = EvidenceRecorder(
                camera_id="cam-isolated-edge",
                storage_dir=str(edge_storage),
                snapshots_enabled=True,
                clips_enabled=True,
                format_ext="jpg",
                quality=90,
                pre_event_seconds=1.0,
                post_event_seconds=1.0,
                max_buffer_seconds=5.0,
                target_fps=10.0,
            )
            # Start authenticated evidence server on Edge
            server = edge_recorder.start_server(
                host="127.0.0.1", port=edge_port, token="test-auth-secret"
            )

            # Feed frames on Edge
            now = time.time()
            for i in range(10):
                frame = self._create_synthetic_camera_frame(color=(0, 150, 50), text=f"ISOLATED_{i}")
                edge_recorder.add_frame(frame, timestamp=now - 0.5 + (i * 0.05), frame_id=i)

            event = AlertEvent(
                camera_id="cam-isolated-edge",
                event_type="intrusion",
                severity="high",
                details="Perimeter breached on isolated edge",
            )
            snap_rel, clip_rel = edge_recorder.handle_alert(event)
            self.assertIsNotNone(snap_rel)
            self.assertIsNotNone(clip_rel)

            # Wait for Edge local files to finish writing
            edge_recorder.flush_active_clips()
            edge_snap_file = edge_storage / f"cam-isolated-edge_{event.event_id}.jpg"
            edge_clip_file = edge_storage / "clips" / f"cam-isolated-edge_{event.event_id}.mp4"
            for _ in range(30):
                if edge_snap_file.exists() and edge_clip_file.exists():
                    break
                time.sleep(0.05)

            self.assertTrue(edge_snap_file.exists())
            self.assertTrue(edge_clip_file.exists())

            # Central starts with empty storage directory (file does NOT exist locally on Central)
            central_storage.mkdir(parents=True, exist_ok=True)
            central_snap_file = central_storage / f"cam-isolated-edge_{event.event_id}.jpg"
            central_clip_file = central_storage / "clips" / f"cam-isolated-edge_{event.event_id}.mp4"
            self.assertFalse(central_snap_file.exists(), "Central should not have file before retrieval")
            self.assertFalse(central_clip_file.exists(), "Central should not have clip before retrieval")

            event.snapshot_path = snap_rel
            event.metadata["clip_path"] = clip_rel

            # Test Central fetching the evidence on-demand over authenticated HTTP from Edge
            from dataclasses import replace
            remote_settings = replace(
                central.settings,
                snapshots_dir=str(central_storage),
                evidence_token="test-auth-secret",
            )
            with patch.object(central, "settings", remote_settings), \
                 patch("central.main.get_edge_evidence_url", return_value=f"http://127.0.0.1:{edge_port}"):

                async def _test_remote_fetch():
                    transport = httpx.ASGITransport(app=central.app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                        # Request snapshot from Central
                        resp_snap = await client.get(f"/snapshots/{edge_snap_file.name}")
                        self.assertEqual(resp_snap.status_code, 200)
                        self.assertEqual(resp_snap.headers.get("content-type"), "image/jpeg")
                        self.assertGreater(len(resp_snap.content), 1000)

                        # Verify Central now cached the snapshot file locally
                        self.assertTrue(central_snap_file.exists())

                        # Request clip from Central
                        resp_clip = await client.get(f"/snapshots/clips/{edge_clip_file.name}")
                        self.assertEqual(resp_clip.status_code, 200)
                        self.assertEqual(resp_clip.headers.get("content-type"), "video/mp4")
                        self.assertGreater(len(resp_clip.content), 500)

                        # Verify Central now cached the clip file locally
                        self.assertTrue(central_clip_file.exists())

                        # Verify retrieved MP4 video is playable and non-zero frames
                        cap = cv2.VideoCapture(str(central_clip_file))
                        self.assertTrue(cap.isOpened())
                        self.assertGreater(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 0)
                        cap.release()

                asyncio.run(_test_remote_fetch())

            edge_recorder.close()
        finally:
            shutil.rmtree(edge_temp, ignore_errors=True)
            shutil.rmtree(central_temp, ignore_errors=True)

    def test_evidence_retention_cleanup(self):
        """Verify cleanup_expired_evidence deletes files exceeding retention age limit."""
        dummy_snap = self.storage_dir / "old_snap.jpg"
        dummy_clip = self.storage_dir / "clips" / "old_clip.mp4"
        dummy_snap.write_text("dummy")
        dummy_clip.write_text("dummy")

        # Set modification time 10 seconds in the past
        old_time = time.time() - 100.0
        os.utime(dummy_snap, (old_time, old_time))
        os.utime(dummy_clip, (old_time, old_time))

        deleted = self.recorder.cleanup_expired_evidence(max_age_seconds=10.0)
        self.assertEqual(deleted, 2)
        self.assertFalse(dummy_snap.exists())
        self.assertFalse(dummy_clip.exists())


if __name__ == "__main__":
    unittest.main()
