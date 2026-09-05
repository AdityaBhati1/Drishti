"""Versioned MQTT and HTTP event contracts for the canonical pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


EVENT_SCHEMA_VERSION = "1.0"


class BoundingBox(BaseModel):
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)
    x2: float = Field(gt=0)
    y2: float = Field(gt=0)


class Detection(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)
    bbox: BoundingBox
    track_id: str | None = Field(default=None, max_length=128)
    crop_base64: str | None = Field(default=None)


class EdgeEvent(BaseModel):
    schema_version: Literal[EVENT_SCHEMA_VERSION] = EVENT_SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    camera_id: str = Field(min_length=1, max_length=128)
    frame_id: int = Field(ge=0)
    detections: list[Detection] = Field(default_factory=list)


class AlertEvent(BaseModel):
    schema_version: Literal[EVENT_SCHEMA_VERSION] = EVENT_SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    camera_id: str = Field(min_length=1, max_length=128)
    event_type: Literal[
        "intrusion", "restricted_zone", "loitering", "face_match", "anpr_match", "abandoned_object", "system"
    ]
    severity: Literal["info", "low", "medium", "high", "critical"]
    details: str = Field(min_length=1, max_length=1024)
    confidence: float | None = Field(default=None, ge=0, le=1)
    track_id: str | None = Field(default=None, max_length=128)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    snapshot_path: str | None = Field(default=None, max_length=512)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
