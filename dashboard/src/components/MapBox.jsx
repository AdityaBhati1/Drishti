import React, { useEffect, useState } from 'react';
import { MapContainer, TileLayer, Marker, Popup, useMap } from 'react-leaflet';
import * as L from 'leaflet';
import 'leaflet/dist/leaflet.css';

const cameraIcon = new L.DivIcon({
    className: 'custom-camera-icon',
    html: `<div class="flex items-center justify-center w-8 h-8 rounded-full bg-slate-950 border-2 border-blue-500 shadow-2xl text-blue-400">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
      <circle cx="12" cy="13" r="4"/>
    </svg>
  </div>`,
    iconSize: [32, 32],
    iconAnchor: [16, 16],
    popupAnchor: [0, -16]
});

const alertIcon = new L.DivIcon({
    className: 'custom-alert-icon',
    html: `<div class="relative flex items-center justify-center w-9 h-9 rounded-full bg-slate-950 border-2 border-rose-500 shadow-2xl text-rose-500">
    <span class="absolute w-14 h-14 rounded-full border-2 border-rose-500/50 animate-ping"></span>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  </div>`,
    iconSize: [36, 36],
    iconAnchor: [18, 18],
    popupAnchor: [0, -18]
});

// Force map view strictly to current active node center
function MapRecenter({ coords }) {
    const map = useMap();

    useEffect(() => {
        if (coords) {
            map.setView(coords, 15);
            map.invalidateSize();
        }
    }, [coords, map]);

    return null;
}

export default function LiveMap({ alerts = [] }) {
    const [nodeCenter, setNodeCenter] = useState([28.6139, 77.2090]);
    const [locStatus, setLocStatus] = useState('LOCATING NODE...');

    useEffect(() => {
        if (!('geolocation' in navigator)) {
            setLocStatus('GEOLOCATION UNSUPPORTED');
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                setNodeCenter([pos.coords.latitude, pos.coords.longitude]);
                setLocStatus('GPS LOCK ACTIVE');
            },
            (err) => {
                console.warn('Geolocation error:', err);
                setLocStatus('STATIONARY OVERWATCH NODE');
            },
            { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 }
        );
    }, []);

    return (
        <div className="w-full h-full min-h-[350px] rounded-xl overflow-hidden relative border border-slate-800 bg-slate-950 flex flex-col">
            <div className="p-3 bg-slate-900/90 border-b border-slate-800 flex items-center justify-between z-10">
                <span className="text-xs font-mono font-bold text-cyan-400 flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse"></span>
                    LIVE OVERWATCH GRID // {locStatus}
                </span>
                <span className="text-[10px] font-mono text-slate-400">
                    TARGET ALERTS: {alerts.length}
                </span>
            </div>

            <div className="flex-1 w-full h-full relative">
                <MapContainer
                    center={nodeCenter}
                    zoom={15}
                    scrollWheelZoom={true}
                    className="w-full h-full z-0 min-h-[300px]"
                >
                    <TileLayer
                        attribution='&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a>'
                        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                    />

                    <MapRecenter coords={nodeCenter} />

                    {/* Node Camera Location */}
                    <Marker position={nodeCenter} icon={cameraIcon}>
                        <Popup>
                            <div className="text-xs font-mono font-bold text-slate-800">
                                📹 CURRENT OVERWATCH CAMERA<br />
                                <span className="text-emerald-600">NODE ACTIVE</span>
                            </div>
                        </Popup>
                    </Marker>

                    {/* Pin incoming alerts directly to active node coordinates */}
                    {alerts.map((alert, idx) => {
                        const markerLat = alert.lat ?? nodeCenter[0];
                        const markerLng = alert.lng ?? nodeCenter[1];

                        return (
                            <Marker
                                key={alert.id || alert.timestamp || idx}
                                position={[markerLat, markerLng]}
                                icon={alertIcon}
                            >
                                <Popup>
                                    <div className="text-xs font-mono text-slate-900">
                                        <div className="font-bold text-rose-600 uppercase">
                                            🚨 {alert.eventType || 'PLATE MATCH'}
                                        </div>
                                        <div><strong>TARGET:</strong> {alert.subject}</div>
                                        <div><strong>TIME:</strong> {alert.timestamp}</div>
                                    </div>
                                </Popup>
                            </Marker>
                        );
                    })}
                </MapContainer>
            </div>
        </div>
    );
}