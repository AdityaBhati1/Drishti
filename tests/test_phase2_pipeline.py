"""Deterministic contract/integration tests; no camera, MQTT broker, or ML model required."""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

from fastapi import Response
from pydantic import ValidationError

from central import database
from central import main as central
from fog.main import FogRuleEngine, process_edge_payload
from shared.events import AlertEvent, BoundingBox, Detection, EdgeEvent


class FakeWebSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


def edge_event(at: datetime, track_id: str = "track-7") -> EdgeEvent:
    return EdgeEvent(
        occurred_at=at,
        camera_id="cam-test",
        frame_id=1,
        detections=[Detection(
            label="person", confidence=0.91, track_id=track_id,
            bbox=BoundingBox(x1=1, y1=2, x2=30, y2=80),
        )],
    )


class PhaseTwoPipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        database.Base.metadata.create_all(bind=database.engine)

    def setUp(self):
        session = database.SessionLocal()
        try:
            session.query(database.Alert).delete()
            session.commit()
        finally:
            session.close()
        central.manager.active_connections.clear()

    async def test_contract_rejects_invalid_alert(self):
        with self.assertRaises(ValidationError):
            AlertEvent.model_validate({
                "camera_id": "cam-test", "event_type": "not-a-real-event",
                "severity": "critical", "details": "invalid",
            })

    async def test_http_ingestion_is_idempotent_and_broadcasts(self):
        central.manager.bind_loop(asyncio.get_running_loop())
        socket = FakeWebSocket()
        central.manager.active_connections.add(socket)
        event = AlertEvent(camera_id="cam-test", event_type="system", severity="info", details="pipeline test")
        session = database.SessionLocal()
        try:
            first = await central.create_alert(event, Response(), session)
            self.assertTrue(first.created)
            self.assertEqual(len(socket.messages), 1)
            duplicate_response = Response()
            duplicate = await central.create_alert(event, duplicate_response, session)
            self.assertFalse(duplicate.created)
            self.assertEqual(duplicate_response.status_code, 200)
            self.assertEqual(len(socket.messages), 1)
        finally:
            session.close()

    async def test_edge_fog_central_mqtt_interface_and_loitering_policy(self):
        start = datetime.now(timezone.utc)
        engine = FogRuleEngine(loitering_seconds=10, track_expiry_seconds=5)
        # Periodic updates within track_expiry_seconds keep the track active
        self.assertEqual(engine.process(edge_event(start)), [])
        self.assertEqual(engine.process(edge_event(start + timedelta(seconds=4))), [])
        self.assertEqual(engine.process(edge_event(start + timedelta(seconds=8))), [])
        alerts = engine.process(edge_event(start + timedelta(seconds=10)))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].event_type, "loitering")
        self.assertEqual(alerts[0].severity, "medium")
        # Immediate subsequent frame does not trigger alert (cooldown active)
        self.assertEqual(engine.process(edge_event(start + timedelta(seconds=11))), [])

        central.manager.bind_loop(asyncio.get_running_loop())
        socket = FakeWebSocket()
        central.manager.active_connections.add(socket)
        central.ingest_mqtt_alert(alerts[0])
        await asyncio.sleep(0.01)
        self.assertEqual(len(socket.messages), 1)
        session = database.SessionLocal()
        try:
            self.assertEqual(session.query(database.Alert).count(), 1)
            central.ingest_mqtt_alert(alerts[0])
            await asyncio.sleep(0.01)
            self.assertEqual(session.query(database.Alert).count(), 1)
        finally:
            session.close()

    async def test_invalid_mqtt_and_edge_payloads_are_discarded(self):
        self.assertEqual(process_edge_payload(b"not-json", FogRuleEngine(10, 5)), [])
        self.assertEqual(process_edge_payload(b"", FogRuleEngine(10, 5)), [])
        self.assertEqual(process_edge_payload(b"{}", FogRuleEngine(10, 5)), [])

        class Message:
            topic = "surveillance/fog/alerts.v1"
            payload = b'{"schema_version":"1.0","event_type":"bad"}'

        # Should log warning and not raise
        central.on_mqtt_message(None, None, Message())

        class EmptyMessage:
            topic = "surveillance/fog/alerts.v1"
            payload = b""

        central.on_mqtt_message(None, None, EmptyMessage())

    async def test_track_expiry_starts_a_new_dwell_window(self):
        start = datetime.now(timezone.utc)
        engine = FogRuleEngine(loitering_seconds=10, track_expiry_seconds=5)
        engine.process(edge_event(start, "track-expire"))
        self.assertEqual(len(engine.tracks), 1)
        # Gap of 6s exceeds track_expiry_seconds (5s) -> track departs, fresh track created
        self.assertEqual(engine.process(edge_event(start + timedelta(seconds=6), "track-expire")), [])
        self.assertEqual(len(engine.tracks), 1)
        track_state = engine.tracks[("cam-test", "track-expire")]
        self.assertEqual(track_state.first_seen, start + timedelta(seconds=6))

    async def test_loitering_cooldown_re_alerts_after_cooldown_window(self):
        start = datetime.now(timezone.utc)
        # 10s loitering, 5s expiry, 20s cooldown
        engine = FogRuleEngine(loitering_seconds=10, track_expiry_seconds=5, cooldown_seconds=20)
        engine.process(edge_event(start, "track-cool"))
        engine.process(edge_event(start + timedelta(seconds=4), "track-cool"))
        engine.process(edge_event(start + timedelta(seconds=8), "track-cool"))
        first_alert = engine.process(edge_event(start + timedelta(seconds=10), "track-cool"))
        self.assertEqual(len(first_alert), 1)
        self.assertEqual(first_alert[0].severity, "medium")

        # Keep alive before cooldown (at 20s: elapsed since alert is 10s < 20s cooldown)
        engine.process(edge_event(start + timedelta(seconds=14), "track-cool"))
        engine.process(edge_event(start + timedelta(seconds=18), "track-cool"))
        no_alert = engine.process(edge_event(start + timedelta(seconds=22), "track-cool"))
        self.assertEqual(len(no_alert), 0)

        # Keep alive until cooldown expires (at 30s: elapsed since alert is 20s >= 20s cooldown)
        engine.process(edge_event(start + timedelta(seconds=26), "track-cool"))
        re_alert = engine.process(edge_event(start + timedelta(seconds=30), "track-cool"))
        self.assertEqual(len(re_alert), 1)
        self.assertEqual(re_alert[0].severity, "high")
        self.assertIn("alert #2", re_alert[0].details)
        # Immediate subsequent frame does not re-alert (cooldown active again)
        self.assertEqual(engine.process(edge_event(start + timedelta(seconds=31), "track-cool")), [])

    async def test_websocket_broadcast_thread_safety(self):
        central.manager.bind_loop(asyncio.get_running_loop())
        sockets = [FakeWebSocket() for _ in range(5)]
        for s in sockets:
            central.manager.active_connections.add(s)

        # Concurrently broadcast from multiple tasks
        tasks = [
            central.manager.broadcast({"event": f"test-{i}"})
            for i in range(20)
        ]
        await asyncio.gather(*tasks)

        for s in sockets:
            self.assertEqual(len(s.messages), 20)

    async def test_sqlite_init_db_runs_without_postgis_errors(self):
        # Ensure init_db detects sqlite and succeeds
        success = database.init_db()
        self.assertTrue(success)

    async def test_full_e2e_edge_fog_central_ws_pipeline(self):
        """End-to-end verification: EdgeEvent -> Fog -> AlertEvent -> Central DB -> WebSocket."""
        central.manager.bind_loop(asyncio.get_running_loop())
        dashboard_socket = FakeWebSocket()
        central.manager.active_connections.add(dashboard_socket)

        # 1. Simulate Edge producing tracking frames
        t0 = datetime.now(timezone.utc)
        fog = FogRuleEngine(loitering_seconds=5, track_expiry_seconds=3)
        fog.process(edge_event(t0, "e2e-subject"))
        fog.process(edge_event(t0 + timedelta(seconds=2), "e2e-subject"))
        alerts = fog.process(edge_event(t0 + timedelta(seconds=5), "e2e-subject"))
        self.assertEqual(len(alerts), 1)
        alert_event = alerts[0]

        # 2. Central receives the AlertEvent
        session = database.SessionLocal()
        try:
            resp = await central.create_alert(alert_event, Response(), session)
            self.assertTrue(resp.created)

            # 3. Verify Database persistence
            saved = session.query(database.Alert).filter(database.Alert.event_id == str(alert_event.event_id)).first()
            self.assertIsNotNone(saved)
            self.assertEqual(saved.event_type, "loitering")
            self.assertEqual(saved.node_id, "cam-test")
            self.assertEqual(saved.status, "PENDING")

            # 4. Verify WebSocket received broadcast payload
            await asyncio.sleep(0.01)
            self.assertEqual(len(dashboard_socket.messages), 1)
            msg = dashboard_socket.messages[0]
            self.assertEqual(msg["event_id"], str(alert_event.event_id))
            self.assertEqual(msg["camera_id"], "cam-test")

            # 5. Verify Operator Acknowledgment updates DB and WebSocket
            ack_resp = central.acknowledge_alert(saved.id, session)
            self.assertEqual(ack_resp["status"], "success")
            self.assertEqual(saved.status, "ACKNOWLEDGED")
        finally:
            session.close()
