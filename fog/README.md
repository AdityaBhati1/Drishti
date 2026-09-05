# Fog Node

This module handles heavy inference tasks including facial recognition vector similarity searches (using Milvus) and rule-based anomaly detection (such as unauthorized off-hours occupancy or loitering).

## Key Features
- **MQTT Event Subscriber**: Listens to the `surveillance/edge/detections` topic.
- **Facial Recognition System (FRS)**: Integrates with Milvus database. Matches simulated/extracted 512-dimension face vectors against stored profiles.
- **Anomaly Detection Engine**: Monitors stateful tracking variables (e.g. tracking dwell time for loitering, evaluating off-hours timestamps for unauthorized entries).
- **Escalation Hub**: Forwards flagged critical events to the Central backend database and notification handlers.

## Configuration Environment
- `MQTT_BROKER`: Host address of Mosquitto broker (default: `localhost`).
- `MQTT_PORT`: Connection port of Mosquitto broker (default: `1883`).
- `MQTT_TOPIC`: Input topic for incoming edge detections (default: `surveillance/edge/detections`).
- `MILVUS_HOST`: IP/hostname of Milvus Standalone server (default: `localhost`).
- `MILVUS_PORT`: Port of Milvus Standalone server (default: `19530`).
- `CENTRAL_API_URL`: REST API destination to post alerts (default: `http://localhost:8000/api/alerts`).
- `NODE_ID`: Unique identity for this node (default: `fog-node-bravo`).
