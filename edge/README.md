# Edge Node

This module handles video ingestion (RTSP stream or webcam) and performs lightweight inference (such as YOLOv8 object detection). Detectable coordinates and object classifications are sent to the Fog node for further analysis.

## Key Features
- **RTSP Ingestion**: Pulls stream frames using OpenCV.
- **YOLOv8 Inference**: Identifies persons, vehicles, and assets.
- **MQTT Event Publisher**: Encodes detections as JSON and publishes them to the Mosquitto Broker.

## Environment Variables
- `MQTT_BROKER`: Host address of Mosquitto broker (default: `localhost`).
- `MQTT_PORT`: Connection port of Mosquitto broker (default: `1883`).
- `MQTT_TOPIC`: Output topic for detections (default: `surveillance/edge/detections`).
- `RTSP_URL`: Source camera path (default: `0` for webcam).
- `NODE_ID`: Unique identity for this node (default: `edge-node-alpha`).
