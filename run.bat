@echo off
cd /d "%~dp0"

echo Starting physical camera...
start "" /B python scripts\host_camera_streamer.py

echo Waiting for camera streamer...
timeout /t 3 /nobreak >nul

echo Starting CCTV pipeline...
docker compose --profile pipeline up -d

echo.
echo CCTV system started.
echo Dashboard: http://localhost:3000
echo.

start http://localhost:3000