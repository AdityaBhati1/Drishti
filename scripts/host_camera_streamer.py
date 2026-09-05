"""Host Camera Streaming Bridge for CCTV Edge Ingestion.

Captures real frames from the host camera (DirectShow index 0 on Windows)
and serves them as a low-latency HTTP MJPEG stream at:
  http://0.0.0.0:8085/video_feed

This allows Docker containers running on Windows (which do not have access
to /dev/video0) to ingest real host camera frames via:
  http://host.docker.internal:8085/video_feed
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [HostCameraStreamer] %(message)s",
)
logger = logging.getLogger("HostCameraStreamer")

CAMERA_INDEX = 0
PORT = 8085
TARGET_FPS = 15.0
FRAME_INTERVAL = 1.0 / TARGET_FPS


class FrameBuffer:
    """Thread-safe buffer holding the latest camera frame."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frame_bytes: bytes | None = None
        self.frame_count: int = 0
        self.last_capture_time: float = 0.0

    def set_frame(self, jpeg_bytes: bytes):
        with self.lock:
            self.frame_bytes = jpeg_bytes
            self.frame_count += 1
            self.last_capture_time = time.time()

    def get_frame(self) -> tuple[bytes | None, int]:
        with self.lock:
            return self.frame_bytes, self.frame_count


buffer = FrameBuffer()
stop_event = threading.Event()


def capture_loop():
    """Background worker continuously reading from the physical webcam."""
    logger.info("Opening physical webcam at index %d...", CAMERA_INDEX)
    cap = cv2.VideoCapture(CAMERA_INDEX)

    # Configure optimal resolution and buffering
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)

    if not cap.isOpened():
        logger.error("Could not open camera at index %d!", CAMERA_INDEX)
        sys.exit(1)

    logger.info("Physical camera successfully opened. Streaming at ~%.1f FPS...", TARGET_FPS)

    try:
        while not stop_event.is_set():
            loop_start = time.time()
            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("Failed to read frame from webcam; retrying in 0.5s...")
                time.sleep(0.5)
                continue

            # Encode frame to JPEG
            ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                buffer.set_frame(jpeg.tobytes())

            elapsed = time.time() - loop_start
            sleep_time = FRAME_INTERVAL - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    finally:
        cap.release()
        logger.info("Webcam released.")


class StreamingHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler serving health checks and the MJPEG stream."""

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            frame_bytes, count = buffer.get_frame()
            self.wfile.write(
                f'{{"status": "ok", "frames_captured": {count}, "streaming": true}}'.encode("utf-8")
            )
            return

        if self.path in ("/video_feed", "/"):
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=--jpgboundary"
            )
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.end_headers()

            last_count = -1
            try:
                while not stop_event.is_set():
                    frame_bytes, count = buffer.get_frame()
                    if frame_bytes and count != last_count:
                        last_count = count
                        self.wfile.write(b"--jpgboundary\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            f"Content-Length: {len(frame_bytes)}\r\n\r\n".encode("utf-8")
                        )
                        self.wfile.write(frame_bytes)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress routine request logging to prevent console noise
        pass


def main():
    capture_thread = threading.Thread(target=capture_loop, daemon=True)
    capture_thread.start()

    # Wait for first frame to be captured before listening
    logger.info("Waiting for first camera frame...")
    for _ in range(50):
        frame_bytes, count = buffer.get_frame()
        if frame_bytes is not None:
            break
        time.sleep(0.1)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), StreamingHandler)
    logger.info("Host Camera Streamer listening on http://0.0.0.0:%d/video_feed", PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down host camera streamer...")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
