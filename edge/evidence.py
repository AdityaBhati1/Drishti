"""Local Edge evidence recorder capturing actual frame snapshots and rolling buffer video clips.

Non-blocking background encoding ensures YOLO inference and camera ingestion loops
are never stalled. Deterministic paths correlate evidence to AlertEvents.
"""
from __future__ import annotations

import collections
import concurrent.futures
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import http.server
import secrets
import socketserver

import cv2
import numpy as np

from shared.config import settings

logger = logging.getLogger("edge.evidence")


class FrameRecord:
    __slots__ = ("timestamp", "frame_id", "frame")

    def __init__(self, timestamp: float, frame_id: int, frame: np.ndarray):
        self.timestamp = timestamp
        self.frame_id = frame_id
        self.frame = frame


class ClipJob:
    def __init__(
        self,
        event_id: str,
        end_time: float,
        output_path: str,
        initial_frames: List[np.ndarray],
        fps: float = 20.0,
    ):
        self.event_id = event_id
        self.end_time = end_time
        self.output_path = output_path
        self.frames: List[np.ndarray] = list(initial_frames)
        self.fps = fps


class EvidenceRecorder:
    """Thread-safe, non-blocking evidence capture engine for Edge video streams."""

    def __init__(
        self,
        camera_id: str = "cam-main-entrance",
        storage_dir: Optional[str] = None,
        snapshots_enabled: Optional[bool] = None,
        clips_enabled: Optional[bool] = None,
        format_ext: Optional[str] = None,
        quality: Optional[int] = None,
        pre_event_seconds: Optional[float] = None,
        post_event_seconds: Optional[float] = None,
        retention_hours: Optional[int] = None,
        max_buffer_seconds: float = 10.0,
        target_fps: float = 20.0,
    ):
        self.camera_id = camera_id
        self.storage_dir = Path(storage_dir or settings.snapshots_dir)
        self.snapshots_enabled = (
            snapshots_enabled if snapshots_enabled is not None else settings.evidence_snapshots_enabled
        )
        self.clips_enabled = (
            clips_enabled if clips_enabled is not None else settings.evidence_clips_enabled
        )
        self.format_ext = (format_ext or settings.evidence_format).lstrip(".").lower()
        self.quality = quality if quality is not None else settings.evidence_quality
        self.pre_event_seconds = (
            pre_event_seconds if pre_event_seconds is not None else settings.evidence_pre_event_seconds
        )
        self.post_event_seconds = (
            post_event_seconds if post_event_seconds is not None else settings.evidence_post_event_seconds
        )
        self.retention_hours = (
            retention_hours if retention_hours is not None else settings.evidence_retention_hours
        )
        self.target_fps = target_fps

        max_frames = max(50, int(max_buffer_seconds * target_fps))
        self._buffer: Deque[FrameRecord] = collections.deque(maxlen=max_frames)
        self._buffer_lock = threading.Lock()

        self._active_clip_jobs: Dict[str, ClipJob] = {}
        self._jobs_lock = threading.Lock()

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=3, thread_name_prefix=f"evidence-{camera_id}"
        )

        self._ensure_storage()

    def _ensure_storage(self) -> bool:
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            (self.storage_dir / "clips").mkdir(parents=True, exist_ok=True)
            return True
        except Exception as exc:
            logger.error("Unable to initialize evidence storage directory %s: %s", self.storage_dir, exc)
            return False

    def add_frame(self, frame: np.ndarray, timestamp: Optional[float] = None, frame_id: int = 0) -> None:
        """In-memory append to rolling buffer; non-blocking (<0.1ms)."""
        if frame is None or frame.size == 0:
            return

        ts = timestamp if timestamp is not None else time.time()
        record = FrameRecord(timestamp=ts, frame_id=frame_id, frame=frame.copy())

        with self._buffer_lock:
            self._buffer.append(record)

        # Feed any in-flight clip recording jobs
        completed_jobs: List[ClipJob] = []
        with self._jobs_lock:
            if self._active_clip_jobs:
                for event_id, job in list(self._active_clip_jobs.items()):
                    job.frames.append(frame.copy())
                    if ts >= job.end_time:
                        completed_jobs.append(self._active_clip_jobs.pop(event_id))

        # Schedule encoding for completed jobs asynchronously
        for job in completed_jobs:
            self._executor.submit(self._encode_clip_worker, job.frames, job.output_path, job.fps)

    def capture_snapshot(self, event_id: str, timestamp: Optional[float] = None) -> Optional[str]:
        """Capture the frame closest to timestamp and save as JPEG image.
        
        Returns the relative web path (e.g. 'snapshots/{camera_id}_{event_id}.jpg').
        """
        if not self.snapshots_enabled:
            return None

        if not self._ensure_storage():
            return None

        target_ts = timestamp if timestamp is not None else time.time()
        best_frame: Optional[np.ndarray] = None

        with self._buffer_lock:
            if not self._buffer:
                return None
            # Find closest frame in time
            best_record = min(self._buffer, key=lambda r: abs(r.timestamp - target_ts))
            best_frame = best_record.frame.copy()

        if best_frame is None or best_frame.size == 0:
            return None

        filename = f"{self.camera_id}_{event_id}.{self.format_ext}"
        out_path = self.storage_dir / filename
        rel_path = f"snapshots/{filename}"

        # Write image asynchronously
        self._executor.submit(self._write_image_worker, best_frame, str(out_path), self.quality)
        return rel_path

    def start_clip_recording(
        self, event_id: str, timestamp: Optional[float] = None, fps: Optional[float] = None
    ) -> Optional[str]:
        """Schedule a video clip incorporating pre-event and post-event frames.
        
        Returns the relative web path (e.g. 'snapshots/clips/{camera_id}_{event_id}.mp4').
        """
        if not self.clips_enabled:
            return None

        if not self._ensure_storage():
            return None

        target_ts = timestamp if timestamp is not None else time.time()
        start_ts = target_ts - self.pre_event_seconds
        end_ts = target_ts + self.post_event_seconds
        clip_fps = fps or self.target_fps

        pre_frames: List[np.ndarray] = []
        with self._buffer_lock:
            for r in self._buffer:
                if start_ts <= r.timestamp <= target_ts:
                    pre_frames.append(r.frame.copy())
            if not pre_frames and self._buffer:
                pre_frames.append(self._buffer[-1].frame.copy())

        filename = f"{self.camera_id}_{event_id}.mp4"
        out_path = str(self.storage_dir / "clips" / filename)
        rel_path = f"snapshots/clips/{filename}"

        if self.post_event_seconds <= 0:
            # Immediate write
            self._executor.submit(self._encode_clip_worker, pre_frames, out_path, clip_fps)
            return rel_path

        job = ClipJob(
            event_id=event_id,
            end_time=end_ts,
            output_path=out_path,
            initial_frames=pre_frames,
            fps=clip_fps,
        )
        with self._jobs_lock:
            self._active_clip_jobs[event_id] = job

        return rel_path

    def flush_active_clips(self) -> None:
        """Immediately finalize all in-flight clip jobs (e.g. on shutdown or camera disconnect)."""
        with self._jobs_lock:
            jobs = list(self._active_clip_jobs.values())
            self._active_clip_jobs.clear()

        for job in jobs:
            self._executor.submit(self._encode_clip_worker, job.frames, job.output_path, job.fps)

    def handle_alert(self, alert_data: Any) -> Tuple[Optional[str], Optional[str]]:
        """Trigger snapshot and clip capture upon receiving an alert event for this camera."""
        if isinstance(alert_data, dict):
            cam_id = alert_data.get("camera_id") or alert_data.get("node_id")
            event_id = str(alert_data.get("event_id") or alert_data.get("id"))
            occurred_at = alert_data.get("occurred_at") or alert_data.get("timestamp")
        else:
            cam_id = getattr(alert_data, "camera_id", None)
            event_id = str(getattr(alert_data, "event_id", ""))
            occurred_at = getattr(alert_data, "occurred_at", None)

        if cam_id != self.camera_id or not event_id:
            return None, None

        ts: Optional[float] = None
        if isinstance(occurred_at, datetime):
            ts = occurred_at.timestamp()
        elif isinstance(occurred_at, (int, float)):
            ts = float(occurred_at)

        snap = self.capture_snapshot(event_id, timestamp=ts)
        clip = self.start_clip_recording(event_id, timestamp=ts)
        return snap, clip

    def cleanup_expired_evidence(self, max_age_seconds: Optional[float] = None) -> int:
        """Prune evidence files older than retention policy."""
        age_limit = (
            max_age_seconds if max_age_seconds is not None else float(self.retention_hours * 3600)
        )
        now = time.time()
        deleted = 0

        for target_dir in [self.storage_dir, self.storage_dir / "clips"]:
            if not target_dir.exists():
                continue
            try:
                for entry in target_dir.iterdir():
                    if entry.is_file():
                        file_age = now - entry.stat().st_mtime
                        if file_age > age_limit:
                            entry.unlink(missing_ok=True)
                            deleted += 1
            except Exception as exc:
                logger.error("Error pruning expired evidence in %s: %s", target_dir, exc)

        return deleted

    def start_server(
        self, host: str = "0.0.0.0", port: Optional[int] = None, token: Optional[str] = None
    ) -> EvidenceServer:
        """Start a lightweight authenticated HTTP server to serve evidence to Central."""
        server = EvidenceServer(
            storage_dir=self.storage_dir,
            host=host,
            port=port if port is not None else settings.edge_evidence_port,
            token=token or settings.evidence_token,
        )
        server.start()
        self._server = server
        return server

    def close(self) -> None:
        if hasattr(self, "_server") and self._server:
            try:
                self._server.stop()
            except Exception as exc:
                logger.debug("Error stopping evidence server: %s", exc)
            self._server = None
        self.flush_active_clips()
        self._executor.shutdown(wait=False)

    # -------------------------------------------------------------------------
    # Static background workers
    # -------------------------------------------------------------------------
    @staticmethod
    def _write_image_worker(frame: np.ndarray, out_path: str, quality: int) -> None:
        try:
            target_path = Path(out_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                with open(target_path, "wb") as f:
                    f.write(buf.tobytes())
                logger.info("Saved evidence snapshot to %s", target_path)
            else:
                logger.error("cv2.imencode failed for snapshot %s", target_path)
        except Exception as exc:
            logger.error("Failed to write snapshot to %s: %s", out_path, exc)

    @staticmethod
    def _encode_clip_worker(frames: List[np.ndarray], out_path: str, fps: float) -> None:
        if not frames:
            return
        temp_raw_path: Optional[Path] = None
        try:
            target_path = Path(out_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp_raw_path = target_path.with_name(f"temp_{target_path.name}")

            h, w = frames[0].shape[:2]
            # Try H.264/AVC1 codecs in priority order for browser compatibility
            candidate_codecs = ["avc1", "H264", "x264", "mp4v"]
            writer = None
            used_codec = "mp4v"
            for codec in candidate_codecs:
                fourcc = cv2.VideoWriter_fourcc(*codec)
                test_writer = cv2.VideoWriter(str(temp_raw_path), fourcc, fps, (w, h))
                if test_writer.isOpened():
                    writer = test_writer
                    used_codec = codec
                    break
                else:
                    test_writer.release()

            if writer is None or not writer.isOpened():
                logger.error("Failed to initialize any cv2.VideoWriter for %s", target_path)
                return

            try:
                for f in frames:
                    if f.shape[:2] == (h, w):
                        writer.write(f)
                    else:
                        resized = cv2.resize(f, (w, h))
                        writer.write(resized)
            finally:
                writer.release()

            # Ensure genuine browser playability (H.264 Baseline, yuv420p, +faststart moov atom)
            ffmpeg_bin = shutil.which("ffmpeg")
            encoded_cleanly = False
            if ffmpeg_bin:
                temp_transcoded_path = target_path.with_name(f"trans_{target_path.name}")
                try:
                    cmd = [
                        ffmpeg_bin, "-y",
                        "-i", str(temp_raw_path),
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                        "-preset", "ultrafast",
                        str(temp_transcoded_path),
                    ]
                    res = subprocess.run(cmd, capture_output=True, timeout=30)
                    if res.returncode == 0 and temp_transcoded_path.exists() and temp_transcoded_path.stat().st_size > 0:
                        encoded_cleanly = True
                        if target_path.exists():
                            target_path.unlink()
                        temp_transcoded_path.replace(target_path)
                        if temp_raw_path.exists():
                            temp_raw_path.unlink()
                    else:
                        logger.warning("FFmpeg transcode returned %d; falling back to direct writer", res.returncode)
                        if temp_transcoded_path.exists():
                            temp_transcoded_path.unlink()
                except Exception as ffmpeg_err:
                    logger.warning("FFmpeg invocation failed: %s; falling back to direct writer", ffmpeg_err)
                    if temp_transcoded_path.exists():
                        temp_transcoded_path.unlink()

            if not encoded_cleanly:
                if temp_raw_path.exists():
                    if target_path.exists():
                        target_path.unlink()
                    temp_raw_path.replace(target_path)

            logger.info("Successfully encoded browser-compatible evidence clip %s (%d frames, initial codec=%s)",
                        target_path, len(frames), used_codec)
        except Exception as exc:
            logger.error("Failed to encode video clip to %s: %s", out_path, exc)
            if temp_raw_path and temp_raw_path.exists():
                try:
                    temp_raw_path.unlink()
                except OSError:
                    pass


class EvidenceHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    storage_dir: Path
    token: str

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("EvidenceServer: " + format, *args)

    def do_GET(self) -> None:
        # Authenticate via header (X-Evidence-Token, Authorization: Bearer) or query param
        auth_token = self.headers.get("X-Evidence-Token")
        if not auth_token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                auth_token = auth_header[7:].strip()
            elif auth_header:
                auth_token = auth_header.strip()
        if not auth_token and "?token=" in self.path:
            auth_token = self.path.split("?token=")[-1].split("&")[0]

        expected_token = getattr(self, "token", "") or settings.evidence_token
        if not expected_token or not auth_token or not secrets.compare_digest(auth_token, expected_token):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Unauthorized"}')
            return

        clean_path = self.path.split("?")[0]
        if clean_path.startswith("/evidence/"):
            subpath = clean_path[len("/evidence/"):].lstrip("/")
        elif clean_path.startswith("/snapshots/"):
            subpath = clean_path[len("/snapshots/"):].lstrip("/")
        else:
            subpath = clean_path.lstrip("/")

        target = (self.storage_dir / subpath).resolve()
        base = self.storage_dir.resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Evidence not found"}')
            return

        ext = target.suffix.lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "video/mp4" if ext == ".mp4" else "application/octet-stream"
        try:
            file_bytes = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(file_bytes)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(file_bytes)
        except Exception as exc:
            logger.error("Error reading evidence file %s: %s", target, exc)
            self.send_response(500)
            self.end_headers()


class EvidenceServer:
    """Lightweight background HTTP server on Edge to serve evidence files to Central."""

    def __init__(
        self,
        storage_dir: Path,
        host: str = "0.0.0.0",
        port: int = 8001,
        token: Optional[str] = None,
    ):
        self.storage_dir = Path(storage_dir)
        self.host = host
        self.port = port
        self.token = token if token is not None else settings.evidence_token
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler = type(
            "BoundEvidenceHandler",
            (EvidenceHTTPRequestHandler,),
            {"storage_dir": self.storage_dir, "token": self.token},
        )
        self._server = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="edge-evidence-server"
        )
        self._thread.start()
        logger.info("Started authenticated Edge evidence server on %s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
