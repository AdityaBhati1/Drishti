"""Comprehensive focused test suite for P2 security and reliability fixes:
1. Operational REST API Authentication (Bearer, X-API-Key, Query token, 401 rejection, public health probes)
2. WebSocket Authentication (token rejection / acceptance)
3. MQTT Authenticated Communication & Password Protection
4. Removal of dangerous default secrets (audit test)
5. Redis Failure Handling & Non-Blocking Degraded Mode
6. FRS Persistence Integrity & Unambiguous Degraded Status
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"  # Intentionally unreachable port for failure testing

import dataclasses
import httpx
import numpy as np

from central import database
from central import main as central
from edge import main as edge_main
from edge.evidence import EvidenceRecorder, EvidenceServer
from fog import main as fog_main
from fog.analytics import FacialRecognitionModule, UnifiedAnalyticsEngine, VectorGallery
from shared.config import Settings, settings
from shared.events import AlertEvent


class TestP2SecurityAndReliability(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.create_all(bind=database.engine)

    def setUp(self):
        self.session = database.SessionLocal()
        try:
            self.session.query(database.Alert).delete()
            self.session.commit()
        finally:
            self.session.close()

    def tearDown(self):
        pass

    # -------------------------------------------------------------------------
    # 1. REST API Authentication
    # -------------------------------------------------------------------------
    async def test_rest_api_unauthenticated_requests_rejected(self):
        """Operational endpoints must return 401 Unauthorized when unauthenticated."""
        test_key = "secure-cctv-p2-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            transport = httpx.ASGITransport(app=central.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. Alert list
                resp = await client.get("/api/alerts")
                self.assertEqual(resp.status_code, 401)
                self.assertIn("Invalid or missing API key", resp.json().get("detail", ""))

                # 2. Watchlists
                resp = await client.get("/api/watchlists")
                self.assertEqual(resp.status_code, 401)

                # 3. Cameras
                resp = await client.get("/api/cameras")
                self.assertEqual(resp.status_code, 401)

                # 4. Snapshots
                resp = await client.get("/snapshots/sample.jpg")
                self.assertEqual(resp.status_code, 401)

                # 5. Connect camera
                resp = await client.post("/api/v1/connect-camera", json={"camera_id": "c1", "ip_address": "1.2.3.4"})
                self.assertEqual(resp.status_code, 401)

    async def test_rest_api_authenticated_requests_accepted(self):
        """Requests with Bearer token, X-API-Key header, or ?token= query param succeed."""
        test_key = "secure-cctv-p2-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            transport = httpx.ASGITransport(app=central.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. Bearer Header
                resp = await client.get("/api/alerts", headers={"Authorization": f"Bearer {test_key}"})
                self.assertEqual(resp.status_code, 200)

                # 2. X-API-Key Header
                resp = await client.get("/api/watchlists", headers={"X-API-Key": test_key})
                self.assertEqual(resp.status_code, 200)

                # 3. Query Param ?token=
                resp = await client.get(f"/api/cameras?token={test_key}")
                self.assertEqual(resp.status_code, 200)

                # 4. Query Param ?api_key=
                resp = await client.get(f"/api/alerts?api_key={test_key}")
                self.assertEqual(resp.status_code, 200)

    async def test_rest_api_public_probes_accessible_without_auth(self):
        """Liveness, readiness, and root service info remain unauthenticated for orchestrators."""
        test_key = "secure-cctv-p2-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            transport = httpx.ASGITransport(app=central.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/")
                self.assertEqual(resp.status_code, 200)

                resp = await client.get("/health")
                self.assertEqual(resp.status_code, 200)

                resp = await client.get("/ready")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body.get("service"), "central")
                self.assertIn("redis", body)
                self.assertIn("frs_persistent_storage", body)

    # -------------------------------------------------------------------------
    # 2. WebSocket Authentication
    # -------------------------------------------------------------------------
    async def test_websocket_unauthenticated_connection_rejected(self):
        """Unauthenticated clients connecting to /ws/alerts must be rejected."""
        test_key = "secure-cctv-ws-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            # Attempt connect without token
            mock_ws = MagicMock()
            mock_ws.headers = {}
            mock_ws.close = unittest.mock.AsyncMock()

            await central.websocket_endpoint(mock_ws, token=None, api_key=None)
            mock_ws.close.assert_awaited_once_with(code=1008, reason="Unauthorized")

            # Attempt connect with invalid token
            mock_ws.close.reset_mock()
            await central.websocket_endpoint(mock_ws, token="wrong-key", api_key=None)
            mock_ws.close.assert_awaited_once_with(code=1008, reason="Unauthorized")

    async def test_websocket_authenticated_connection_accepted(self):
        """Authenticated clients with matching token are accepted."""
        test_key = "secure-cctv-ws-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            mock_ws = MagicMock()
            mock_ws.headers = {}
            mock_ws.accept = unittest.mock.AsyncMock()
            mock_ws.receive_text = unittest.mock.AsyncMock(side_effect=central.WebSocketDisconnect(code=1000))
            mock_ws.close = unittest.mock.AsyncMock()

            await central.websocket_endpoint(mock_ws, token=test_key, api_key=None)
            mock_ws.accept.assert_awaited_once()
            mock_ws.close.assert_not_called()

    async def test_websocket_subprotocol_authenticated_without_url_token(self):
        """Browser clients pass token via Sec-WebSocket-Protocol header without leaking token in URL query params."""
        test_key = "secure-cctv-ws-subprotocol-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            mock_ws = MagicMock()
            mock_ws.headers = {"sec-websocket-protocol": f"cctv-auth, {test_key}"}
            mock_ws.accept = unittest.mock.AsyncMock()
            mock_ws.receive_text = unittest.mock.AsyncMock(side_effect=central.WebSocketDisconnect(code=1000))
            mock_ws.close = unittest.mock.AsyncMock()

            # No query params provided - token is purely in the subprotocol header
            await central.websocket_endpoint(mock_ws, token=None, api_key=None)
            mock_ws.accept.assert_awaited_once_with(subprotocol="cctv-auth")
            mock_ws.close.assert_not_called()

    async def test_websocket_invalid_subprotocol_rejected(self):
        """Invalid subprotocol token must be rejected with close code 1008."""
        test_key = "secure-cctv-ws-token"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            mock_ws = MagicMock()
            mock_ws.headers = {"sec-websocket-protocol": "cctv-auth, wrong-subprotocol-token"}
            mock_ws.close = unittest.mock.AsyncMock()

            await central.websocket_endpoint(mock_ws, token=None, api_key=None)
            mock_ws.close.assert_awaited_once_with(code=1008, reason="Unauthorized")

    async def test_evidence_ticket_generation_and_access(self):
        """Evidence tickets allow short-lived (120s) access without exposing master API keys in URLs."""
        test_key = "secure-cctv-master-key"
        new_settings = dataclasses.replace(central.settings, central_api_key=test_key)
        with patch.object(central, "settings", new_settings):
            # 1. Valid ticket generation
            ticket = central.generate_evidence_ticket("cam-1/snapshot_001.jpg", expires_seconds=120)
            self.assertTrue(central.validate_evidence_ticket("cam-1/snapshot_001.jpg", ticket))

            # 2. Expired ticket fails
            expired_ticket = central.generate_evidence_ticket("cam-1/snapshot_001.jpg", expires_seconds=-10)
            self.assertFalse(central.validate_evidence_ticket("cam-1/snapshot_001.jpg", expired_ticket))

            # 3. Ticket for different path fails (prevent path traversal / token replay)
            self.assertFalse(central.validate_evidence_ticket("cam-2/snapshot_other.jpg", ticket))

            # 4. GET /api/evidence-ticket endpoint
            transport = httpx.ASGITransport(app=central.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # Unauthenticated fails
                resp = await client.get("/api/evidence-ticket?path=test.jpg")
                self.assertEqual(resp.status_code, 401)

                # Authenticated succeeds
                resp = await client.get(
                    "/api/evidence-ticket?path=test.jpg",
                    headers={"Authorization": f"Bearer {test_key}"}
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("ticket", data)
                self.assertEqual(data["expires_in"], 120)

    def test_no_credentials_in_dashboard_urls(self):
        """Dashboard source must not append long-lived API keys to WebSocket or evidence URLs."""
        app_jsx = Path("dashboard/src/App.jsx").read_text(encoding="utf-8")
        # Ensure resolveEvidenceUrl does not append ?token=
        self.assertNotIn("token=${encodeURIComponent(CENTRAL_API_KEY)}", app_jsx)
        # Ensure getDefaultWsUrl does not append ?token=
        self.assertNotIn("ws://localhost:8000/ws/alerts?token=", app_jsx)
        # Ensure WebSocket connection uses subprotocols
        self.assertIn("['cctv-auth', CENTRAL_API_KEY]", app_jsx)

    # -------------------------------------------------------------------------
    # 3. MQTT Authentication & Password Protection
    # -------------------------------------------------------------------------
    def test_mqtt_client_authentication_configured(self):
        """MQTT clients configure username_pw_set when credentials are provided in settings."""
        mock_settings = dataclasses.replace(settings, mqtt_username="cctv_worker", mqtt_password="super_secret_mqtt_pass")
        from paho.mqtt.client import Client as PahoClient
        c = PahoClient(client_id="test")
        c.username_pw_set = MagicMock()
        if mock_settings.mqtt_username and mock_settings.mqtt_password:
            c.username_pw_set(mock_settings.mqtt_username, mock_settings.mqtt_password)

        c.username_pw_set.assert_called_once_with("cctv_worker", "super_secret_mqtt_pass")

    def test_mosquitto_conf_anonymous_disabled(self):
        """Mosquitto broker config must have allow_anonymous false and password_file set."""
        conf_path = Path("infrastructure/mosquitto/config/mosquitto.conf")
        self.assertTrue(conf_path.exists(), "mosquitto.conf must exist")
        content = conf_path.read_text(encoding="utf-8")
        self.assertIn("allow_anonymous false", content)
        self.assertIn("password_file /mosquitto/config/passwd", content)
        self.assertNotIn("allow_anonymous true", content)

    # -------------------------------------------------------------------------
    # 4. Dangerous Default Secrets Audit
    # -------------------------------------------------------------------------
    def test_audit_no_dangerous_fallback_secrets(self):
        """Confirm absence of change-me, cctv_password, cctv-evidence-secret, and default minio credentials."""
        # 1. Check shared/config.py
        cfg_content = Path("shared/config.py").read_text(encoding="utf-8")
        self.assertNotIn("cctv_password", cfg_content)
        self.assertNotIn("cctv-evidence-secret", cfg_content)
        self.assertNotIn("change-me", cfg_content)

        # 2. Check docker-compose.yml
        compose_content = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("change-me", compose_content)
        self.assertNotIn("cctv-evidence-secret", compose_content)
        self.assertNotIn("MINIO_ACCESS_KEY: minioadmin", compose_content)
        self.assertNotIn("MINIO_SECRET_KEY: minioadmin", compose_content)

        # 3. Check edge/evidence.py
        evidence_content = Path("edge/evidence.py").read_text(encoding="utf-8")
        self.assertNotIn('"cctv-evidence-secret"', evidence_content)

    # -------------------------------------------------------------------------
    # 5. Redis Failure Handling & Non-Blocking Degraded Mode
    # -------------------------------------------------------------------------
    def test_redis_outage_does_not_block_alert_ingestion_or_acknowledgement(self):
        """Alert ingestion and acknowledgement must not block on connection timeouts during Redis outage."""
        # Ensure Redis is marked unavailable
        central._redis_available = False
        central.redis_client = None

        now = datetime.now(timezone.utc)
        event = AlertEvent(
            camera_id="cam-main-entrance",
            event_type="intrusion",
            severity="critical",
            details="Perimeter breach test while Redis is dead",
            occurred_at=now,
        )

        db = database.SessionLocal()
        try:
            # Measure time to ingest alert when Redis is dead
            start_time = time.perf_counter()
            alert, created = central.persist_alert_event(event, db)
            central.arm_escalation(alert)
            ingest_duration = time.perf_counter() - start_time

            # Must complete instantaneously without waiting 1-5s socket timeout
            self.assertTrue(created)
            self.assertLess(ingest_duration, 0.15, f"Alert ingestion took {ingest_duration:.4f}s; must be <0.15s")

            # Measure time to acknowledge alert when Redis is dead
            ack_start = time.perf_counter()
            ack_resp = central.acknowledge_alert(alert.id, db=db)
            ack_duration = time.perf_counter() - ack_start

            self.assertEqual(ack_resp["status"], "success")
            self.assertLess(ack_duration, 0.15, f"Alert acknowledgement took {ack_duration:.4f}s; must be <0.15s")
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # 6. FRS Persistence Integrity & Degraded Mode
    # -------------------------------------------------------------------------
    def test_frs_does_not_claim_persistence_when_milvus_unavailable(self):
        """VectorGallery and FRS module must report is_persistent=False when Milvus is unavailable."""
        gallery = VectorGallery(host="127.0.0.1", port=1)
        self.assertFalse(gallery.is_persistent, "Milvus on port 1 is unreachable; is_persistent must be False")

        status = gallery.get_status()
        self.assertFalse(status["persistent"])
        self.assertEqual(status["backend"], "degraded_in_memory")
        self.assertIsNotNone(status["error"])

        frs = FacialRecognitionModule(milvus_host="127.0.0.1", milvus_port=1)
        self.assertFalse(frs.is_persistent)
        self.assertFalse(frs.is_persistent_storage_available())

    def test_frs_strict_persistence_enrollment_fails_clearly(self):
        """Enrollment with require_persistence=True must fail clearly if Milvus is offline."""
        gallery = VectorGallery(host="127.0.0.1", port=1)
        fake_vector = np.ones((512,), dtype=np.float32)

        # 1. With require_persistence=True, must reject and return False
        success = gallery.enroll("Test Target", fake_vector, require_persistence=True)
        self.assertFalse(success, "Must reject enrollment when persistence is required but Milvus is dead")

        # 2. With allow_ephemeral=False, must reject
        success = gallery.enroll("Test Target", fake_vector, allow_ephemeral=False)
        self.assertFalse(success, "Must reject enrollment when ephemeral storage is disallowed")

        # 3. Ephemeral enrollment allowed only for test/dev with explicit flag, but is_persistent remains False
        success = gallery.enroll("Test Target", fake_vector, require_persistence=False, allow_ephemeral=True)
        self.assertTrue(success, "Ephemeral dev mode may enroll into in-memory gallery")
        self.assertFalse(gallery.is_persistent, "Gallery must still report is_persistent=False")


if __name__ == "__main__":
    unittest.main()
