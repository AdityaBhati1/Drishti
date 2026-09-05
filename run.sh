#!/bin/bash

cd "$(dirname "$0")"

echo "Starting physical camera..."
python3 scripts/host_camera_streamer.py &
CAMERA_PID=$!

echo "Waiting for camera streamer..."
sleep 3

echo "Starting CCTV pipeline..."
docker compose --profile pipeline up -d

echo ""
echo "CCTV system started."
echo "Dashboard: http://localhost:3000"
echo ""

# Open dashboard
if [[ "$OSTYPE" == "darwin"* ]]; then
    open http://localhost:3000
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open http://localhost:3000
fi

# Keep script aware of the camera process
wait $CAMERA_PID