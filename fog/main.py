"""Canonical Fog rule engine.

Consumes versioned EdgeEvent messages and emits only validated, rule-derived
AlertEvent messages. Biometric recognition is intentionally absent until a
real model and enrollment flow are integrated; unknown is never a match.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from uuid import uuid4
from shared.config import settings
from shared.events import AlertEvent, EdgeEvent


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fog")


@dataclass
class TrackState:
    first_seen: datetime
    last_seen: datetime
    loitering_alerted: bool = False
    last_alerted_at: datetime | None = None
    alert_count: int = 0


class FogRuleEngine:
    """Per-camera, per-track temporal rules with bounded track retention, departure, and cooldown."""

    def __init__(self, loitering_seconds: int, track_expiry_seconds: int, cooldown_seconds: int = 60):
        self.loitering_seconds = loitering_seconds
        self.track_expiry_seconds = track_expiry_seconds
        self.cooldown_seconds = cooldown_seconds
        self.tracks: dict[tuple[str, str], TrackState] = {}

    def expire_tracks(self, now: datetime) -> list[tuple[str, str]]:
        """Prune tracks unseen beyond track_expiry_seconds (departure) and return departed keys."""
        departed = [
            key for key, state in self.tracks.items()
            if (now - state.last_seen).total_seconds() > self.track_expiry_seconds
        ]
        for key in departed:
            state = self.tracks.pop(key)
            duration = (state.last_seen - state.first_seen).total_seconds()
            logger.info(
                "Track %s on camera %s departed after %.1fs (total alerts: %d)",
                key[1], key[0], duration, state.alert_count,
            )
        return departed

    def process(self, event: EdgeEvent, camera_config: Optional[dict] = None) -> list[AlertEvent]:
        # Check if loitering is enabled for this camera
        modules_cfg = (camera_config or {}).get("modules", {})
        loitering_cfg = modules_cfg.get("loitering", {})
        if not loitering_cfg.get("enabled", True):
            return []

        eff_loitering_seconds = int(loitering_cfg.get("loitering_seconds", self.loitering_seconds))
        eff_cooldown_seconds = int(loitering_cfg.get("cooldown_seconds", self.cooldown_seconds))

        now = event.occurred_at
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        self.expire_tracks(now)
        alerts: list[AlertEvent] = []
        for detection in event.detections:
            # A temporal rule cannot be trustworthy without a stable tracker ID.
            if detection.label != "person" or not detection.track_id:
                continue
            key = (event.camera_id, detection.track_id)
            state = self.tracks.get(key)
            if state is None:
                self.tracks[key] = TrackState(first_seen=now, last_seen=now)
                continue
            state.last_seen = now
            dwell_seconds = (now - state.first_seen).total_seconds()
            if dwell_seconds < eff_loitering_seconds:
                continue

            # Check cooldown if already alerted
            if state.loitering_alerted:
                if eff_cooldown_seconds <= 0:
                    continue
                if state.last_alerted_at and (now - state.last_alerted_at).total_seconds() < eff_cooldown_seconds:
                    continue

            state.loitering_alerted = True
            state.last_alerted_at = now
            state.alert_count += 1
            severity = "high" if state.alert_count > 1 else "medium"
            alert_id = uuid4()
            alerts.append(AlertEvent(
                event_id=alert_id,
                camera_id=event.camera_id,
                event_type="loitering",
                severity=severity,
                details=(f"Tracked person {detection.track_id} remained visible for "
                         f"{int(dwell_seconds)} seconds (alert #{state.alert_count})."),
                confidence=detection.confidence,
                track_id=detection.track_id,
                lat=settings.site_lat,
                lng=settings.site_lng,
                snapshot_path=f"snapshots/{event.camera_id}_{alert_id}.jpg",
                metadata={"clip_path": f"snapshots/clips/{event.camera_id}_{alert_id}.mp4"},
            ))
        return alerts


try:
    from fog.analytics import UnifiedAnalyticsEngine
    analytics_engine = UnifiedAnalyticsEngine()
except ImportError:
    try:
        from analytics import UnifiedAnalyticsEngine
        analytics_engine = UnifiedAnalyticsEngine()
    except Exception as exc:
        logger.warning("UnifiedAnalyticsEngine initialization deferred: %s", exc)
        analytics_engine = None
except Exception as exc:
    logger.warning("UnifiedAnalyticsEngine initialization deferred: %s", exc)
    analytics_engine = None

rule_engine = FogRuleEngine(settings.loitering_seconds, settings.track_expiry_seconds)


def process_edge_payload(
    raw_payload: bytes,
    engine: FogRuleEngine = rule_engine,
    analytics: Optional[UnifiedAnalyticsEngine] = None,
    return_telemetry: bool = False,
):
    """Validate one MQTT payload and return derived alerts without publishing."""
    try:
        edge_event = EdgeEvent.model_validate_json(raw_payload)
    except (ValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Discarding invalid EdgeEvent: %s", exc)
        return ([], None) if return_telemetry else []

    active_analytics = analytics if analytics is not None else analytics_engine
    cam_config = active_analytics.cameras.get(edge_event.camera_id, {}) if active_analytics else {}

    alerts: list[AlertEvent] = []
    alerts.extend(engine.process(edge_event, camera_config=cam_config))

    telemetry = None
    if active_analytics is not None:
        try:
            alerts.extend(active_analytics.process_edge_event(edge_event))
            telemetry = getattr(active_analytics, "last_telemetry", None)
        except Exception as e:
            logger.error("Analytics processing failed for camera %s: %s", edge_event.camera_id, e)

    if return_telemetry:
        return alerts, telemetry
    return alerts


def on_message(client: mqtt.Client, _userdata: object, msg: mqtt.MQTTMessage) -> None:
    alerts, telemetry = process_edge_payload(msg.payload, return_telemetry=True)
    for alert in alerts:
        result = client.publish(settings.mqtt_fog_alert_topic, alert.model_dump_json())
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Failed to publish AlertEvent %s: MQTT rc=%s", alert.event_id, result.rc)
        else:
            logger.info("Published %s for camera %s (id=%s, details=%s)",
                        alert.event_type, alert.camera_id, alert.event_id, alert.details)

    if telemetry and telemetry.get("tracks") is not None:
        try:
            client.publish(settings.mqtt_telemetry_topic, json.dumps(telemetry))
        except Exception as exc:
            logger.debug("Failed to publish telemetry to %s: %s", settings.mqtt_telemetry_topic, exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fog-rule-engine")
    client.on_message = on_message

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc == 0:
            c.subscribe(settings.mqtt_edge_topic)
            logger.info("Connected and subscribed to %s", settings.mqtt_edge_topic)
        else:
            logger.warning("Fog MQTT connection failed with code %s", rc)

    client.on_connect = on_connect
    if settings.mqtt_username and settings.mqtt_password:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    client.connect(settings.mqtt_broker, settings.mqtt_port, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
