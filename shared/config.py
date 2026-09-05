"""Environment-backed configuration shared by Edge, Fog, and Central services."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    mqtt_broker: str = os.getenv("MQTT_BROKER", "localhost")
    mqtt_port: int = _int("MQTT_PORT", 1883)
    mqtt_edge_topic: str = os.getenv("MQTT_EDGE_TOPIC", "surveillance/edge/events.v1")
    mqtt_fog_alert_topic: str = os.getenv("MQTT_FOG_ALERT_TOPIC", "surveillance/fog/alerts.v1")
    mqtt_telemetry_topic: str = os.getenv("MQTT_TELEMETRY_TOPIC", "surveillance/telemetry.v1")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///./cctv_dev.db" if os.getenv("APP_ENV", "development") == "development" else "",
    )
    central_api_key: str = os.getenv("CENTRAL_API_KEY", "")
    mqtt_username: str = os.getenv("MQTT_USERNAME", "")
    mqtt_password: str = os.getenv("MQTT_PASSWORD", "")
    frs_require_persistence: bool = os.getenv("FRS_REQUIRE_PERSISTENCE", "false").lower() in ("true", "1", "yes")
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = _int("REDIS_PORT", 6379)
    milvus_host: str = os.getenv("MILVUS_HOST", "localhost")
    milvus_port: int = _int("MILVUS_PORT", 19530)
    site_lat: float = _float("SITE_LAT", 28.6139)
    site_lng: float = _float("SITE_LNG", 77.2090)
    site_address: str = os.getenv("SITE_ADDRESS", "Unconfigured site")
    loitering_seconds: int = _int("LOITERING_SECONDS", 300)
    track_expiry_seconds: int = _int("TRACK_EXPIRY_SECONDS", 30)
    abandoned_seconds: int = _int("ABANDONED_SECONDS", 60)
    cooldown_seconds: int = _int("COOLDOWN_SECONDS", 60)
    snapshots_dir: str = os.getenv("SNAPSHOTS_DIR", str(PROJECT_ROOT / "snapshots"))
    evidence_snapshots_enabled: bool = os.getenv("EVIDENCE_SNAPSHOTS_ENABLED", "true").lower() in ("true", "1", "yes")
    evidence_clips_enabled: bool = os.getenv("EVIDENCE_CLIPS_ENABLED", "true").lower() in ("true", "1", "yes")
    evidence_format: str = os.getenv("EVIDENCE_FORMAT", "jpg")
    evidence_quality: int = _int("EVIDENCE_QUALITY", 90)
    evidence_pre_event_seconds: float = _float("EVIDENCE_PRE_EVENT_SECONDS", 3.0)
    evidence_post_event_seconds: float = _float("EVIDENCE_POST_EVENT_SECONDS", 3.0)
    evidence_retention_hours: int = _int("EVIDENCE_RETENTION_HOURS", 24)
    evidence_token: str = os.getenv("EVIDENCE_TOKEN", "")
    edge_evidence_port: int = _int("EDGE_EVIDENCE_PORT", 8001)
    edge_evidence_host: str = os.getenv("EDGE_EVIDENCE_HOST", "edge")
    config_yaml_path: str = os.getenv("CONFIG_YAML_PATH", str(PROJECT_ROOT / "config.yaml"))
    cameras_yaml_path: str = os.getenv("CAMERAS_YAML_PATH", str(PROJECT_ROOT / "cameras.yaml"))
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    )


settings = Settings()

