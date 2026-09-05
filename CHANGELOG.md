# Changelog

## Unreleased

### Phase 0 — architecture consolidation

- Declared Edge → Fog → Central → Dashboard as the canonical topology.
- Added shared, versioned event contracts and environment-backed settings.
- Documented standalone/demo components as quarantined experiments.

### Phase 1 — bootstrapping

- Added a supported Python version, environment template, and consolidated
  dependency definitions.
- Added Docker definitions for Central and dashboard, with hardware inference
  workloads opt-in under the `pipeline` Compose profile.
- Disabled the browser-upload AI demo by default so it cannot emit production
  alerts through the dashboard.

### Phase 2 — pipeline reliability & biometrics integrity

- Implemented replay-safe idempotent alert persistence in Central (`POST /api/alerts`) with schema boundaries.
- Added dynamic database dialect handling supporting both PostgreSQL (+ PostGIS) and SQLite seamlessly.
- Hardened MQTT ingestion in Central against malformed payloads and broker disconnects.
- Secured WebSocket connection management with thread locks and ASGI broadcast serialization.
- Removed random mock embedding generator from Fog and eliminated pseudo-biometric 16x16 pixel matching from Central.
- Fixed temporal loitering/dwell rules with persistent tracking IDs, departure pruning, and cooldown escalation.
- Wired live WebSocket alert ingestion and operator acknowledgment into the dashboard.

### Phase 3 — shared perception & modular analytics

- Established the canonical shared perception architecture:
  Single detection & multi-object tracking pass feeding shared tracked objects to all downstream analytics.
- Added YAML configuration schemas (`cameras.yaml` / `config.yaml`) defining per-camera module enablement, tripwires, polygon ROIs, and watchlists.
- Built Intrusion Module with vector trajectory line-crossing detection.
- Built Restricted Zone Module with polygon point-in-polygon tests and scheduled off-hours windows.
- Built Abandoned/Suspicious Object Module with unattended baggage dwell and proximity tracking.
- Built ANPR Module with Indian/international plate format normalization and watchlist matching.
- Built Facial Recognition Module with authentic vector matching and explicit unknown/unverified states.
- Enhanced Central REST API with multi-field alert filtering, keyword search, camera catalog, and watchlist administration.
