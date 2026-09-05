# Surveillance Operations Control Center Dashboard

A web UI for the surveillance platform. Renders live camera feeds (incorporating simulated bounding-box overlays from YOLO detection alerts), connects to real-time WebSockets, and maps spatio-temporal telemetry on a tactical grid.

## Key Features
- **Vite + React Setup**: Built with premium styled glassmorphic panels and dark-mode styling.
- **WebSocket Gateway Client**: Opens a direct channel to `ws://localhost:8000/ws/alerts` for zero-latency alert logs.
- **REST Api Integration**: Fetches historical incident logs from the central server on initial load.
- **Tactical GPS Map**: Coordinates latitude and longitude metadata onto an operations map using glowing markers and pulsing radar waves.

## Running the Dashboard
1. Run `npm install` to install dependencies.
2. Run `npm run dev` to start the frontend server on port `3000`.
