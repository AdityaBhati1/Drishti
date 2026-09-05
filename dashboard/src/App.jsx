import React, { useState, useEffect, useRef } from 'react';
import CameraFeed from './components/CameraFeed';
import CameraFullscreenModal from './components/CameraFullscreenModal';
import AlertFeedPanel from './components/AlertFeedPanel';
import WatchlistDrawer from './components/WatchlistDrawer';
import ConfigDrawer from './components/ConfigDrawer';
import SystemStatusModal from './components/SystemStatusModal';
import { MapContainer, TileLayer, Marker, Popup, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { CURRENT_NODE_LOCATION } from './config/location';

import {
  Shield, Bell, Radio, MapPin, UserPlus, Sliders,
  AlertOctagon, RefreshCw, Layers, CheckCircle, Clock, Car, Camera, Video,
  Activity, Grid, Maximize2
} from 'lucide-react';

// Marker Icons Setup for Leaflet Tactical Map
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

const userLocationIcon = new L.DivIcon({
  className: 'custom-user-icon',
  html: `<div class="relative flex items-center justify-center w-8 h-8 rounded-full bg-slate-950 border-2 border-cyan-400 shadow-2xl text-cyan-400">
    <span class="absolute w-12 h-12 rounded-full border border-cyan-400/30 animate-ping"></span>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
      <line x1="8" y1="21" x2="16" y2="21"/>
      <line x1="12" y1="17" x2="12" y2="21"/>
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

// Helper function to compress target photos into 256x256 JPEG base64 strings (~20 KB)
function compressTargetPortrait(imageSource, maxDim = 256) {
  return new Promise((resolve) => {
    if (!imageSource) {
      resolve(null);
      return;
    }
    const img = new Image();
    let blobUrl = null;
    if (typeof imageSource === 'string' && (imageSource.startsWith('http://') || imageSource.startsWith('https://'))) {
      img.crossOrigin = 'anonymous';
    }
    img.onload = () => {
      try {
        const offCanvas = document.createElement('canvas');
        offCanvas.width = maxDim;
        offCanvas.height = maxDim;
        const ctx = offCanvas.getContext('2d');
        ctx.fillStyle = '#000000';
        ctx.fillRect(0, 0, maxDim, maxDim);

        const srcW = img.naturalWidth || img.width || maxDim;
        const srcH = img.naturalHeight || img.height || maxDim;

        const cropW = Math.min(srcW, srcH);
        const cropH = cropW;
        const sx = (srcW - cropW) / 2;
        const sy = Math.max(0, (srcH - cropH) / 4);

        ctx.drawImage(img, sx, sy, cropW, cropH, 0, 0, maxDim, maxDim);
        const compressedBase64 = offCanvas.toDataURL('image/jpeg', 0.82);
        if (blobUrl) URL.revokeObjectURL(blobUrl);
        resolve(compressedBase64);
      } catch (e) {
        resolve(null);
      }
    };
    img.onerror = () => {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      resolve(null);
    };

    if (imageSource instanceof File || imageSource instanceof Blob) {
      blobUrl = URL.createObjectURL(imageSource);
      img.src = blobUrl;
    } else if (typeof imageSource === 'string') {
      img.src = imageSource;
    } else {
      resolve(null);
    }
  });
}

function RecenterMap({ coords }) {
  const map = useMap();
  useEffect(() => {
    if (coords && coords[0] && coords[1]) {
      map.flyTo(coords, map.getZoom(), { animate: true, duration: 1.2 });
    }
  }, [coords, map]);
  return null;
}

function isTimeInWindow(dateObj, startTimeStr, endTimeStr) {
  try {
    if (!startTimeStr || !endTimeStr) return true;
    const nowH = dateObj.getHours();
    const nowM = dateObj.getMinutes();
    const nowMin = nowH * 60 + nowM;

    const [sH, sM] = startTimeStr.split(':').map(Number);
    const startMin = sH * 60 + sM;

    const [eH, eM] = endTimeStr.split(':').map(Number);
    const endMin = eH * 60 + eM;

    if (startMin === endMin) return true; // All day / 24 hour restriction

    if (startMin < endMin) {
      // Same-day range (e.g. 08:00 to 18:00)
      return nowMin >= startMin && nowMin <= endMin;
    } else {
      // Overnight range (e.g. 22:00 to 06:00)
      return nowMin >= startMin || nowMin <= endMin;
    }
  } catch (e) {
    return true;
  }
}

const getRuntimeEnv = (key, fallback = '') => {
  if (typeof window !== 'undefined' && window.__ENV__ && window.__ENV__[key]) {
    return window.__ENV__[key];
  }
  if (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env[key] !== undefined && import.meta.env[key] !== '') {
    return import.meta.env[key];
  }
  return fallback;
};

const CENTRAL_API_BASE = getRuntimeEnv('VITE_CENTRAL_API_URL', '').replace(/\/+$/, '');
const CENTRAL_API_KEY = getRuntimeEnv('VITE_CENTRAL_API_KEY', '') ||
  (typeof localStorage !== 'undefined' ? localStorage.getItem('central_api_key') || '' : '');
const AI_BACKEND_BASE = getRuntimeEnv('VITE_AI_BACKEND_URL', 'http://localhost:8002');
const CENTRAL_API_URL = `${CENTRAL_API_BASE}/api/alerts`;

const getAuthHeaders = (extraHeaders = {}) => {
  const headers = { ...extraHeaders };
  if (CENTRAL_API_KEY) {
    headers['Authorization'] = `Bearer ${CENTRAL_API_KEY}`;
    headers['X-API-Key'] = CENTRAL_API_KEY;
  }
  return headers;
};

export function formatApiError(err) {
  if (!err) return 'Connection failed';
  if (typeof err === 'string') return err;
  if (Array.isArray(err)) {
    return err
      .map(e => {
        if (!e) return '';
        if (typeof e === 'string') return e;
        if (typeof e === 'object') {
          const loc = Array.isArray(e.loc) ? e.loc.filter(l => l !== 'body').join('.') : '';
          const msg = e.msg || e.message || (typeof e === 'object' ? JSON.stringify(e) : String(e));
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(e);
      })
      .filter(Boolean)
      .join('; ') || 'Validation error';
  }
  if (typeof err === 'object') {
    if (err.msg) return err.msg;
    if (err.message) return err.message;
    if (err.detail) return formatApiError(err.detail);
    if (err.error) return formatApiError(err.error);
    try {
      return JSON.stringify(err);
    } catch {
      return String(err);
    }
  }
  return String(err);
}

const getDefaultWsUrl = () => {
  let url = 'ws://localhost:8000/ws/alerts';
  if (typeof window !== 'undefined' && window.location) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    url = `${protocol}//${window.location.host}/ws/alerts`;
  }
  return url;
};

const CENTRAL_WS_URL = getRuntimeEnv('VITE_CENTRAL_WS_URL', '') || getDefaultWsUrl();
const ENABLE_EXPERIMENTAL_BROWSER_AI = getRuntimeEnv('VITE_ENABLE_EXPERIMENTAL_BROWSER_AI', '') === 'true';

const resolveEvidenceUrl = (path) => {
  if (!path) return '';
  const base = (CENTRAL_API_BASE || '').replace(/\/+$/, '');
  return path.startsWith('http://') || path.startsWith('https://') ? path : `${base}${path.startsWith('/') ? path : `/${path}`}`;
};

const safelyGetArray = (key) => {
  try {
    const saved = localStorage.getItem(key);
    if (!saved) return [];
    const parsed = JSON.parse(saved);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    return [];
  }
};

export default function App() {
  const [activeView, setActiveView] = useState('CAMERAS'); // 'CAMERAS' | 'MAP'
  const [engineMode, setEngineMode] = useState('FOG-CLUSTER');
  const [alerts, setAlerts] = useState([]);
  const [localAlerts, setLocalAlerts] = useState(() => safelyGetArray('watchlist_alerts'));
  const [connected, setConnected] = useState(false);

  // Modal and Drawer States
  const [fullscreenCamera, setFullscreenCamera] = useState(null);
  const [isWatchlistOpen, setIsWatchlistOpen] = useState(false);
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [isStatusModalOpen, setIsStatusModalOpen] = useState(false);

  // Persist localAlerts in localStorage
  useEffect(() => {
    try {
      localStorage.setItem('watchlist_alerts', JSON.stringify(localAlerts));
    } catch (e) {
      console.warn("Failed to persist alerts in localStorage:", e);
    }
  }, [localAlerts]);

  // Watchlist & Enrollment states (Face + Plate)
  const [enrollForm, setEnrollForm] = useState({ name: '', targetPlate: '' });
  const [selectedFile, setSelectedFile] = useState(null);
  const [enrollStatus, setEnrollStatus] = useState({ loading: false, success: null, error: null });

  // Restricted Zone & Off-Hours Security Rules state
  const [restrictedRules, setRestrictedRules] = useState(() => {
    const saved = safelyGetArray('restricted_rules');
    if (saved.length > 0) return saved;
    return [
      {
        id: 'RULE_PERIMETER_NIGHT',
        name: 'Night Perimeter & Off-Hours Lock',
        cameraId: 'CAM_01',
        startTime: '22:00',
        endTime: '06:00',
        constraint: 'PERSON_OR_CAR',
        enabled: true
      },
      {
        id: 'RULE_UPTOWN_RESTRICTED',
        name: 'Uptown Restricted Area Watch',
        cameraId: 'CAM_02',
        startTime: '20:00',
        endTime: '07:00',
        constraint: 'PERSON_OR_CAR',
        enabled: true
      }
    ];
  });

  const [ruleForm, setRuleForm] = useState({
    name: '',
    cameraId: 'CAM_01',
    startTime: '22:00',
    endTime: '06:00',
    constraint: 'PERSON_OR_CAR'
  });
  const [ruleStatus, setRuleStatus] = useState({ success: null, error: null });

  useEffect(() => {
    try {
      localStorage.setItem('restricted_rules', JSON.stringify(restrictedRules));
    } catch (e) {
      console.warn("Failed to save restricted rules in localStorage:", e);
    }
  }, [restrictedRules]);

  const restrictedRulesRef = useRef(restrictedRules);
  useEffect(() => {
    restrictedRulesRef.current = restrictedRules;
  }, [restrictedRules]);

  const handleAddRule = (e) => {
    e.preventDefault();
    if (!ruleForm.name.trim()) {
      setRuleStatus({ success: null, error: "Please enter a rule designation (e.g. Server Room Off-Hours)." });
      return;
    }
    const newRule = {
      id: `RULE_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`,
      name: ruleForm.name.trim(),
      cameraId: ruleForm.cameraId,
      startTime: ruleForm.startTime,
      endTime: ruleForm.endTime,
      constraint: ruleForm.constraint,
      enabled: true
    };
    const updated = [...restrictedRules, newRule];
    setRestrictedRules(updated);
    setRuleStatus({ success: `Rule '${newRule.name}' created successfully!`, error: null });
    setRuleForm(prev => ({ ...prev, name: '' }));
  };

  const handleToggleRule = (ruleId) => {
    setRestrictedRules(prev => prev.map(r => r.id === ruleId ? { ...r, enabled: !r.enabled } : r));
  };

  const handleDeleteRule = (ruleId) => {
    setRestrictedRules(prev => prev.filter(r => r.id !== ruleId));
  };

  // Camera stream & management states
  const [connectedCameras, setConnectedCameras] = useState({});
  const [backendCameras, setBackendCameras] = useState([]);
  const [cameraLoading, setCameraLoading] = useState(false);
  const [cameraActionStatus, setCameraActionStatus] = useState(null);
  const [slotOrder, setSlotOrder] = useState(['CAM_01', 'CAM_02', 'CAM_03']);
  const [modalForm, setModalForm] = useState({
    sourceType: 'direct',
    cameraId: '',
    streamUrl: '',
    ipAddress: '',
    port: '554',
    rtspPath: '',
    username: '',
    password: '',
    targetSlot: 'Camera Slot 2'
  });
  const [modalLoading, setModalLoading] = useState(false);
  const [modalError, setModalError] = useState(null);
  const [toastMessage, setToastMessage] = useState(null);
  const [testResult, setTestResult] = useState(null);
  const [testLoading, setTestLoading] = useState(false);

  // Dynamic Geolocation state
  const [laptopLocation, setLaptopLocation] = useState(null);
  const [mapCenter, setMapCenter] = useState([CURRENT_NODE_LOCATION.lat, CURRENT_NODE_LOCATION.lng]);
  const [locationSource, setLocationSource] = useState('STATIC NODE');
  const [gpsError, setGpsError] = useState(null);

  const triggerGpsSync = () => {
    setLocationSource('LOCATING...');
    setGpsError(null);

    if (!('geolocation' in navigator)) {
      setGpsError("Geolocation is not supported by your browser.");
      return;
    }

    const optionsHigh = { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 };
    const optionsLow = { enableHighAccuracy: false, timeout: 5000, maximumAge: 0 };

    const handleSuccess = (pos) => {
      const userCoords = [pos.coords.latitude, pos.coords.longitude];
      setLaptopLocation(userCoords);
      setMapCenter(userCoords);
      setLocationSource('GPS LOCK');
      setGpsError(null);
    };

    const handleIpFallback = (err) => {
      let errorMsg = "GPS unavailable. Using IP fallback.";
      if (err.code === 1) {
        errorMsg = "GPS blocked. Please allow location permissions.";
      }
      setGpsError(errorMsg);

      fetch('https://ipapi.co/json/')
        .then(res => res.json())
        .then(data => {
          if (data.latitude && data.longitude) {
            const userCoords = [data.latitude, data.longitude];
            setLaptopLocation(userCoords);
            setMapCenter(userCoords);
            setLocationSource('IP GEOLOCATION');
          } else {
            throw new Error("Invalid IP geo data");
          }
        })
        .catch(() => {
          setLaptopLocation([CURRENT_NODE_LOCATION.lat, CURRENT_NODE_LOCATION.lng]);
          setMapCenter([CURRENT_NODE_LOCATION.lat, CURRENT_NODE_LOCATION.lng]);
          setLocationSource('STATIC NODE');
        });
    };

    navigator.geolocation.getCurrentPosition(
      handleSuccess,
      () => {
        navigator.geolocation.getCurrentPosition(
          handleSuccess,
          (errLow) => handleIpFallback(errLow),
          optionsLow
        );
      },
      optionsHigh
    );
  };

  useEffect(() => {
    let watchId = null;
    if ('geolocation' in navigator) {
      watchId = navigator.geolocation.watchPosition(
        (pos) => {
          const userCoords = [pos.coords.latitude, pos.coords.longitude];
          setLaptopLocation(userCoords);
          setMapCenter(userCoords);
          setLocationSource('GPS LOCK');
          setGpsError(null);
        },
        () => {},
        { enableHighAccuracy: false, timeout: 15000, maximumAge: 10000 }
      );
    }
    triggerGpsSync();
    return () => {
      if (watchId !== null) navigator.geolocation.clearWatch(watchId);
    };
  }, []);

  const fetchConfiguredCameras = async () => {
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/cameras`, {
        headers: getAuthHeaders()
      });
      if (res.ok) {
        const data = await res.json();
        const cams = data.cameras || [];
        setBackendCameras(cams);

        const newConnected = {};
        cams.forEach(cam => {
          const rtsp = cam.rtsp_url || cam.source?.url;
          const status = (cam.status || 'active').toLowerCase();
          if (rtsp && status !== 'inactive' && status !== 'disabled') {
            const idUpper = (cam.id || '').toUpperCase();
            const nameUpper = (cam.name || '').toUpperCase();
            if (idUpper.includes('CAM_02') || idUpper.includes('SLOT-2') || nameUpper.includes('SLOT 2') || idUpper === 'CAM02') {
              newConnected['Camera Slot 2'] = {
                cameraId: cam.id,
                streamUrl: rtsp,
                name: cam.name || cam.id
              };
            } else if (idUpper.includes('CAM_03') || idUpper.includes('SLOT-3') || nameUpper.includes('SLOT 3') || idUpper === 'CAM03') {
              newConnected['Camera Slot 3'] = {
                cameraId: cam.id,
                streamUrl: rtsp,
                name: cam.name || cam.id
              };
            } else {
              newConnected[cam.id] = {
                cameraId: cam.id,
                streamUrl: rtsp,
                name: cam.name || cam.id
              };
            }
          }
        });
        setConnectedCameras(newConnected);
      }
    } catch (err) {
      console.warn("Failed to sync cameras from central:", err);
    }
  };

  useEffect(() => {
    fetchConfiguredCameras();
  }, []);

  // CCTV Hardware registry - Dynamically composed from backend & local devices
  const authQuery = CENTRAL_API_KEY ? `?api_key=${encodeURIComponent(CENTRAL_API_KEY)}` : '';

  // Local laptop node fallback & backend sync
  const localCamBackend = backendCameras.find(c => (c.id || '').toUpperCase() === 'CAM_01');
  const localLaptopCamera = {
    id: 'CAM_01',
    name: localCamBackend?.name || 'LOCAL_LAPTOP_NODE',
    status: (localCamBackend?.status || 'active').toLowerCase(),
    lat: localCamBackend?.location?.lat ?? (laptopLocation ? laptopLocation[0] : CURRENT_NODE_LOCATION.lat),
    lng: localCamBackend?.location?.lng ?? (laptopLocation ? laptopLocation[1] : CURRENT_NODE_LOCATION.lng),
    address: localCamBackend?.location?.address || CURRENT_NODE_LOCATION.address,
    location: localCamBackend?.location || {
      lat: laptopLocation ? laptopLocation[0] : CURRENT_NODE_LOCATION.lat,
      lng: laptopLocation ? laptopLocation[1] : CURRENT_NODE_LOCATION.lng,
      address: CURRENT_NODE_LOCATION.address
    },
    streamUrl: typeof window !== 'undefined' && window.location.hostname
      ? `http://${window.location.hostname}:8085/video_feed`
      : 'http://localhost:8085/video_feed',
    rtsp_url: localCamBackend?.rtsp_url || (typeof window !== 'undefined' && window.location.hostname
      ? `http://${window.location.hostname}:8085/video_feed`
      : 'http://localhost:8085/video_feed'),
    isLiveStream: (localCamBackend?.status || 'active') !== 'disabled' && (localCamBackend?.status || 'active') !== 'inactive',
    modules: localCamBackend?.modules
  };

  const otherCameras = [];
  const handledIds = new Set(['CAM_01']);

  backendCameras.forEach(cam => {
    if ((cam.id || '').toUpperCase() === 'CAM_01') return;
    handledIds.add(cam.id);
    const idUpper = cam.id.toUpperCase();
    let slotKey = cam.id;
    if (idUpper.includes('CAM_02') || idUpper.includes('SLOT-2') || idUpper === 'CAM02') slotKey = 'Camera Slot 2';
    else if (idUpper.includes('CAM_03') || idUpper.includes('SLOT-3') || idUpper === 'CAM03') slotKey = 'Camera Slot 3';

    const activeConn = connectedCameras[slotKey] || connectedCameras[cam.id];
    const streamUrl = activeConn
      ? `${CENTRAL_API_BASE}/video_feed_slot/${encodeURIComponent(slotKey)}${authQuery}`
      : (cam.rtsp_url || cam.source?.url || null);

    const status = (cam.status || 'active').toLowerCase();
    otherCameras.push({
      id: cam.id,
      name: cam.name || cam.id,
      status: status,
      lat: cam.location?.lat ?? (laptopLocation ? laptopLocation[0] + 0.005 : CURRENT_NODE_LOCATION.lat + 0.005),
      lng: cam.location?.lng ?? (laptopLocation ? laptopLocation[1] + 0.005 : CURRENT_NODE_LOCATION.lng + 0.005),
      address: cam.location?.address || `Surveillance Node ${cam.id}`,
      location: cam.location,
      rtsp_url: cam.rtsp_url || cam.source?.url || null,
      streamUrl: streamUrl,
      isLiveStream: Boolean(streamUrl && status !== 'disabled' && status !== 'inactive'),
      modules: cam.modules
    });
  });

  const cameras = [localLaptopCamera, ...otherCameras];
  const activeCameras = cameras.filter(c => c.status !== 'disabled' && c.status !== 'inactive');

  // Synchronize slotOrder with currently active cameras
  useEffect(() => {
    setSlotOrder(prevOrder => {
      const activeIds = activeCameras.map(c => c.id);
      if (activeIds.length === 0) return [];
      const filtered = prevOrder.filter(id => activeIds.includes(id));
      activeIds.forEach(id => {
        if (!filtered.includes(id)) {
          filtered.push(id);
        }
      });
      return filtered.length > 0 ? filtered : activeIds;
    });
  }, [activeCameras.map(c => `${c.id}:${c.status}`).join(',')]);

  // FOCUS SWITCHING: Swap clicked secondary camera with primary camera
  const handleFocusCamera = (clickedId) => {
    setSlotOrder(prevOrder => {
      if (!prevOrder.length || prevOrder[0] === clickedId) {
        // Clicking primary camera does nothing!
        return prevOrder;
      }
      const clickedIdx = prevOrder.indexOf(clickedId);
      if (clickedIdx === -1) return prevOrder;

      const newOrder = [...prevOrder];
      // Swap primary camera with clicked camera's previous slot
      const temp = newOrder[0];
      newOrder[0] = newOrder[clickedIdx];
      newOrder[clickedIdx] = temp;
      return newOrder;
    });
  };

  // CAMERA MANAGEMENT: Update, Toggle Enable/Disable, and Delete
  const handleUpdateCamera = async (cameraId, updates) => {
    setCameraLoading(true);
    setCameraActionStatus(null);
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/cameras/${encodeURIComponent(cameraId)}`, {
        method: 'PUT',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(updates)
      });
      if (res.ok) {
        setToastMessage(`Camera ${cameraId} configuration updated`);
        setTimeout(() => setToastMessage(null), 3000);
        await fetchConfiguredCameras();
        setCameraActionStatus({ message: `Camera ${cameraId} updated successfully`, error: false });
      } else {
        const errData = await res.json().catch(() => ({}));
        setCameraActionStatus({ message: formatApiError(errData) || 'Failed to update camera', error: true });
      }
    } catch (err) {
      setCameraActionStatus({ message: String(err.message || err), error: true });
    } finally {
      setCameraLoading(false);
    }
  };

  const handleToggleCamera = async (cameraId) => {
    setCameraLoading(true);
    setCameraActionStatus(null);
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/cameras/${encodeURIComponent(cameraId)}/toggle`, {
        method: 'POST',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' })
      });
      if (res.ok) {
        const data = await res.json();
        setToastMessage(`Camera ${cameraId} is now ${data.new_status}`);
        setTimeout(() => setToastMessage(null), 3000);
        await fetchConfiguredCameras();
        setCameraActionStatus({ message: `Camera ${cameraId} is now ${data.new_status}`, error: false });
      } else {
        const errData = await res.json().catch(() => ({}));
        setCameraActionStatus({ message: formatApiError(errData) || 'Failed to toggle camera', error: true });
      }
    } catch (err) {
      setCameraActionStatus({ message: String(err.message || err), error: true });
    } finally {
      setCameraLoading(false);
    }
  };

  const handleDeleteCamera = async (cameraId) => {
    setCameraLoading(true);
    setCameraActionStatus(null);
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/cameras/${encodeURIComponent(cameraId)}`, {
        method: 'DELETE',
        headers: getAuthHeaders()
      });
      if (res.ok) {
        setToastMessage(`Camera ${cameraId} deleted. Historical records preserved.`);
        setTimeout(() => setToastMessage(null), 3500);
        await fetchConfiguredCameras();
        setCameraActionStatus({ message: `Camera ${cameraId} deleted. Historical records preserved.`, error: false });
      } else {
        const errData = await res.json().catch(() => ({}));
        setCameraActionStatus({ message: formatApiError(errData) || 'Failed to delete camera', error: true });
      }
    } catch (err) {
      setCameraActionStatus({ message: String(err.message || err), error: true });
    } finally {
      setCameraLoading(false);
    }
  };

  const [clearAlertsLoading, setClearAlertsLoading] = useState(false);

  const handleClearAlerts = async () => {
    setClearAlertsLoading(true);
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/alerts`, {
        method: 'DELETE',
        headers: getAuthHeaders()
      });
      if (res.ok) {
        const data = await res.json();
        setAlerts([]);
        setLocalAlerts([]);
        try {
          localStorage.removeItem('watchlist_alerts');
        } catch (_) {}
        setToastMessage(data.message || 'Alert history cleared');
        setTimeout(() => setToastMessage(null), 3500);
      } else {
        const errData = await res.json().catch(() => ({}));
        const msg = formatApiError(errData) || 'Failed to clear alerts';
        setToastMessage(msg);
        setTimeout(() => setToastMessage(null), 3500);
      }
    } catch (err) {
      setToastMessage(String(err.message || err));
      setTimeout(() => setToastMessage(null), 3500);
    } finally {
      setClearAlertsLoading(false);
    }
  };

  const highlightCameraPin = (camId) => {
    const camera = cameras.find(c => c.id === camId);
    if (camera) {
      setMapCenter([camera.lat, camera.lng]);
    }
  };

  // HANDLER: Off-Hours Restricted Zone Rules Evaluator
  const handleDetection = (detection) => {
    const activeRestrictedRules = restrictedRulesRef.current.filter(r => r.enabled);
    const isUnauthorizedPerson = detection.subject === 'UNAUTHORIZED PERSON' || detection.eventType === 'UNAUTHORIZED PRESENCE';

    const violatedRules = activeRestrictedRules.filter(rule => {
      if (rule.cameraId !== 'ALL_CAMERAS' && rule.cameraId !== detection.cameraId) {
        return false;
      }
      const now = new Date();
      if (!isTimeInWindow(now, rule.startTime, rule.endTime)) {
        return false;
      }
      if (rule.constraint === 'PERSON_ONLY' && detection.subject !== 'UNAUTHORIZED PERSON' && !detection.eventType.includes('PRESENCE')) {
        return false;
      }
      if (rule.constraint === 'VEHICLE_ONLY' && detection.eventType !== 'PLATE MATCH') {
        return false;
      }
      return true;
    });

    if (violatedRules.length > 0) {
      const highestRule = violatedRules[0];
      const alertPayload = {
        ...detection,
        eventType: 'OFF-HOURS INTRUSION',
        details: `VIOLATION: '${highestRule.name}' (${highestRule.startTime}-${highestRule.endTime}) triggered on ${detection.cameraId}`,
        severity: 'CRITICAL',
        timestamp: new Date().toISOString()
      };
      dispatchSecurityAlert(alertPayload);
      return;
    }

    if (detection.eventType === 'TARGET MATCH' || detection.eventType === 'PLATE MATCH' || isUnauthorizedPerson) {
      dispatchSecurityAlert(detection);
    }
  };

  const dispatchSecurityAlert = async (alertData) => {
    if (engineMode === 'CLIENT-SIDE') {
      setLocalAlerts(prev => {
        const isDuplicate = prev.slice(0, 5).some(a =>
          a.subject === alertData.subject &&
          a.cameraId === alertData.cameraId
        );
        if (isDuplicate) return prev;
        return [alertData, ...prev];
      });
    } else {
      try {
        const canonicalSeverity = (alertData.severity || "high").toLowerCase();
        let canonicalEventType = "intrusion";
        if (alertData.eventType === "TARGET MATCH") canonicalEventType = "face_match";
        else if (alertData.eventType === "PLATE MATCH") canonicalEventType = "anpr_match";

        await fetch(`${CENTRAL_API_BASE}/api/alerts`, {
          method: 'POST',
          headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            schema_version: "1.0",
            event_id: alertData.id || `ALERT_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
            camera_id: alertData.cameraId || 'CAM_01',
            event_type: canonicalEventType,
            severity: canonicalSeverity,
            details: alertData.details || `${alertData.eventType} triggered on ${alertData.cameraId}`,
            occurred_at: new Date().toISOString(),
            confidence: (alertData.confidence || 90) / 100,
            lat: alertData.lat,
            lng: alertData.lng
          })
        });
      } catch (e) {
        console.warn("Could not push alert to Central API:", e);
      }
    }
  };

  // Watchlist Local Storage Sync
  const [enrolledTargets, setEnrolledTargets] = useState(() => safelyGetArray('watchlist_targets'));
  const [enrolledPlates, setEnrolledPlates] = useState(() => safelyGetArray('watchlist_plates'));

  useEffect(() => {
    try {
      localStorage.setItem('watchlist_targets', JSON.stringify(enrolledTargets));
    } catch (e) {
      console.warn("Storage error:", e);
    }
  }, [enrolledTargets]);

  useEffect(() => {
    try {
      localStorage.setItem('watchlist_plates', JSON.stringify(enrolledPlates));
    } catch (e) {
      console.warn("Storage error:", e);
    }
  }, [enrolledPlates]);

  const handleAcknowledgeAlert = async (alertId) => {
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/alerts/${alertId}/acknowledge`, {
        method: 'POST',
        headers: getAuthHeaders()
      });
      if (res.ok) {
        setAlerts(prev => prev.map(a => (a.id === alertId ? { ...a, status: 'ACKNOWLEDGED' } : a)));
      }
    } catch (e) {
      console.warn("Failed to acknowledge alert:", e);
    }
  };

  // WebSocket Subscription for Real-time Central Alerts & Telemetry
  const ws = useRef(null);

  useEffect(() => {
    if (engineMode === 'FOG-CLUSTER') {
      fetchHistoricalAlerts();
      connectWebSocket();
    }
    return () => {
      if (ws.current) ws.current.close();
    };
  }, [engineMode]);

  const fetchHistoricalAlerts = async () => {
    try {
      const res = await fetch(CENTRAL_API_URL, {
        headers: getAuthHeaders()
      });
      if (res.ok) {
        const data = await res.json();
        setAlerts(data);
      }
    } catch (e) {
      console.warn("Historical alerts endpoint unreachable.");
    }
  };

  const connectWebSocket = () => {
    try {
      const protocols = CENTRAL_API_KEY ? ['cctv-auth', CENTRAL_API_KEY] : [];
      ws.current = new WebSocket(CENTRAL_WS_URL, protocols);
      ws.current.onopen = () => setConnected(true);
      ws.current.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          // High-frequency telemetry stream: dispatch custom event directly to avoid React re-render churn
          if (data.type === 'telemetry' || data.event_type === 'telemetry') {
            window.dispatchEvent(new CustomEvent('cctv-telemetry', { detail: data }));
            return;
          }
          if (data.event_type === 'ALERT_ACKNOWLEDGED') {
            setAlerts(prev => prev.map(a => (a.id === data.alert_id ? { ...a, status: 'ACKNOWLEDGED' } : a)));
            return;
          }
          if (data.event_type === 'ALERTS_CLEARED') {
            setAlerts([]);
            setLocalAlerts([]);
            try {
              localStorage.removeItem('watchlist_alerts');
            } catch (_) {}
            return;
          }
          if (data.event_type === 'ESCALATION_TO_COMMAND') {
            setAlerts(prev => prev.map(a => (a.id === data.alert_id ? { ...a, status: 'ESCALATED_TO_COMMAND', severity: 'CRITICAL' } : a)));
            return;
          }
          // New alert broadcast
          setAlerts(prev => {
            const exists = prev.some(a => (data.event_id && a.event_id === data.event_id) || (data.id && a.id === data.id));
            if (exists) return prev;
            return [data, ...prev];
          });
        } catch (err) {
          console.warn("Failed to parse WebSocket message:", err);
        }
      };
      ws.current.onclose = () => {
        setConnected(false);
        if (engineMode === 'FOG-CLUSTER') {
          setTimeout(() => { if (engineMode === 'FOG-CLUSTER') connectWebSocket(); }, 5000);
        }
      };
      ws.current.onerror = (err) => {
        console.warn("WebSocket error:", err);
      };
    } catch (e) {
      console.warn("Failed to connect WebSocket:", e);
    }
  };

  const handleOpenClip = async (e, clipPath) => {
    e.preventDefault();
    const cleanUrl = resolveEvidenceUrl(clipPath);
    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/evidence-ticket?path=${encodeURIComponent(clipPath)}`, {
        headers: getAuthHeaders()
      });
      if (res.ok) {
        const data = await res.json();
        window.open(`${cleanUrl}?ticket=${encodeURIComponent(data.ticket)}`, '_blank');
        return;
      }
    } catch (err) {}
    try {
      const r = await fetch(cleanUrl, { headers: getAuthHeaders() });
      if (r.ok) {
        const blob = await r.blob();
        window.open(URL.createObjectURL(blob), '_blank');
      }
    } catch (err) {}
  };

  const handleTestConnection = async () => {
    setTestLoading(true);
    setTestResult(null);
    try {
      let testPayload = {};
      if ((modalForm.sourceType || 'direct') === 'direct') {
        if (!modalForm.streamUrl || !modalForm.streamUrl.trim()) {
          setTestResult({ connected: false, error: 'Please enter a stream URL' });
          setTestLoading(false);
          return;
        }
        testPayload = {
          stream_url: modalForm.streamUrl.trim(),
          username: modalForm.username ? modalForm.username.trim() : null,
          password: modalForm.password ? modalForm.password.trim() : null,
          timeout: 5.0
        };
      } else {
        if (!modalForm.ipAddress || !modalForm.ipAddress.trim()) {
          setTestResult({ connected: false, error: 'Please enter an IP address' });
          setTestLoading(false);
          return;
        }
        const port = modalForm.port || '554';
        const path = modalForm.rtspPath ? (modalForm.rtspPath.startsWith('/') ? modalForm.rtspPath : `/${modalForm.rtspPath}`) : '/live';
        testPayload = {
          stream_url: `rtsp://${modalForm.ipAddress.trim()}:${port}${path}`,
          username: modalForm.username ? modalForm.username.trim() : null,
          password: modalForm.password ? modalForm.password.trim() : null,
          timeout: 5.0
        };
      }

      const res = await fetch(`${CENTRAL_API_BASE}/api/cameras/test-connection`, {
        method: 'POST',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(testPayload)
      });
      const data = await res.json();
      if (res.ok && data.connected) {
        setTestResult(data);
      } else {
        setTestResult({
          connected: false,
          error: formatApiError(data.error || data.detail || data.message || 'Connection failed')
        });
      }
    } catch (err) {
      setTestResult({ connected: false, error: formatApiError(err?.message || 'Network error or backend unreachable') });
    } finally {
      setTestLoading(false);
    }
  };

  const handleModalSubmit = async (e) => {
    e.preventDefault();
    setModalLoading(true);
    setModalError(null);

    const effectiveSlot = modalForm.targetSlot ||
      (modalForm.cameraId?.toUpperCase().includes('03') ? 'Camera Slot 3' : 'Camera Slot 2');

    try {
      const res = await fetch(`${CENTRAL_API_BASE}/api/connect-ip-camera`, {
        method: 'POST',
        headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          camera_id: modalForm.cameraId,
          source_type: modalForm.sourceType || 'direct',
          stream_url: modalForm.streamUrl,
          ip_address: modalForm.ipAddress,
          port: modalForm.port,
          rtsp_path: modalForm.rtspPath,
          username: modalForm.username,
          password: modalForm.password,
          target_slot: effectiveSlot
        })
      });

      const data = await res.json();
      if (res.ok) {
        const streamSrc = modalForm.streamUrl || `rtsp://${modalForm.ipAddress}:${modalForm.port || '554'}${modalForm.rtspPath || '/live'}`;
        setConnectedCameras(prev => ({
          ...prev,
          [effectiveSlot]: {
            cameraId: modalForm.cameraId,
            ipAddress: modalForm.ipAddress || (modalForm.streamUrl ? 'DIRECT STREAM' : ''),
            targetSlot: effectiveSlot,
            streamUrl: streamSrc,
            name: modalForm.cameraId
          },
          [modalForm.cameraId]: {
            cameraId: modalForm.cameraId,
            ipAddress: modalForm.ipAddress || (modalForm.streamUrl ? 'DIRECT STREAM' : ''),
            targetSlot: effectiveSlot,
            streamUrl: streamSrc,
            name: modalForm.cameraId
          }
        }));
        await fetchConfiguredCameras();
        setIsConfigOpen(false);
        setToastMessage("Camera connected & streaming successfully!");
        setTimeout(() => setToastMessage(null), 4000);
        setModalForm({
          sourceType: 'direct',
          cameraId: '',
          streamUrl: '',
          ipAddress: '',
          port: '554',
          rtspPath: '',
          username: '',
          password: '',
          targetSlot: 'Camera Slot 2'
        });
        setTestResult(null);
      } else {
        setModalError(formatApiError(data.detail || data.error || data.message || "Failed to ping IP stream."));
      }
    } catch (err) {
      setModalError(formatApiError(err?.message || "Failed to connect to backend server."));
    } finally {
      setModalLoading(false);
    }
  };

  const handleEnrollSubmit = async (e) => {
    e.preventDefault();
    if (!enrollForm.name && !selectedFile && !enrollForm.targetPlate) {
      setEnrollStatus({ loading: false, success: null, error: "Please enter a subject name + photo OR a license plate number." });
      return;
    }

    setEnrollStatus({ loading: true, success: null, error: null });

    try {
      let updatedTargets = [...enrolledTargets];
      let updatedPlates = [...enrolledPlates];

      if (enrollForm.name && selectedFile) {
        const compressedImage = await compressTargetPortrait(selectedFile, 256);
        if (compressedImage) {
          const targetName = enrollForm.name.trim() || 'TARGET';
          const newTarget = {
            name: targetName,
            imageSrc: compressedImage
          };
          updatedTargets = [...updatedTargets.filter(t => t.name !== targetName), newTarget];
          setEnrolledTargets(updatedTargets);
          try {
            await fetch(`${CENTRAL_API_BASE}/api/watchlists/faces`, {
              method: "POST",
              headers: getAuthHeaders({ "Content-Type": "application/json" }),
              body: JSON.stringify({
                name: targetName,
                threat_level: "critical",
                notes: "Enrolled via dashboard",
                image_base64: compressedImage
              })
            });
          } catch (apiErr) {
            console.warn("Failed to persist face enrollment to Central:", apiErr);
          }
        }
      }

      if (enrollForm.targetPlate.trim()) {
        const cleanedPlate = enrollForm.targetPlate.trim().toUpperCase();
        if (!updatedPlates.includes(cleanedPlate)) {
          updatedPlates = [...updatedPlates, cleanedPlate];
          setEnrolledPlates(updatedPlates);
        }
        try {
          await fetch(`${CENTRAL_API_BASE}/api/watchlists/plates`, {
            method: "POST",
            headers: getAuthHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
              plate: cleanedPlate,
              owner: "Dashboard Operator",
              threat_level: "critical",
              notes: "Enrolled via dashboard"
            })
          });
        } catch (apiErr) {
          console.warn("Failed to persist plate enrollment to Central:", apiErr);
        }
      }

      setEnrollStatus({
        loading: false,
        success: `Target(s) registered to Watchlist successfully!`,
        error: null
      });

      setEnrollForm({ name: '', targetPlate: '' });
      setSelectedFile(null);
    } catch (err) {
      setEnrollStatus({ loading: false, success: null, error: "Failed to enroll target." });
    }
  };

  const handleSnapWebcamFace = async () => {
    const targetName = enrollForm.name.trim() || 'OPERATOR_SELF';
    setEnrollStatus({ loading: true, success: null, error: null });

    try {
      const imgEl = document.querySelector('img[alt*="CAM_01"]') || document.querySelector('video');
      if (!imgEl) {
        setEnrollStatus({ loading: false, success: null, error: "Webcam feed not ready. Ensure camera is active." });
        return;
      }

      const snapCanvas = document.createElement('canvas');
      snapCanvas.width = 300;
      snapCanvas.height = 300;
      const ctx = snapCanvas.getContext('2d');

      const srcW = imgEl.naturalWidth || imgEl.videoWidth || 640;
      const srcH = imgEl.naturalHeight || imgEl.videoHeight || 480;
      const cropDim = Math.min(srcW, srcH) * 0.75;
      const sx = (srcW - cropDim) / 2;
      const sy = (srcH - cropDim) / 3;

      ctx.drawImage(imgEl, sx, sy, cropDim, cropDim, 0, 0, 300, 300);
      const compressedBase64 = snapCanvas.toDataURL('image/jpeg', 0.85);

      const newTarget = {
        name: targetName,
        imageSrc: compressedBase64
      };

      const updatedTargets = [...enrolledTargets.filter(t => t.name !== targetName), newTarget];
      setEnrolledTargets(updatedTargets);

      try {
        await fetch(`${CENTRAL_API_BASE}/api/watchlists/faces`, {
          method: "POST",
          headers: getAuthHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({
            name: targetName,
            threat_level: "critical",
            notes: "Operator webcam snapshot",
            image_base64: compressedBase64
          })
        });
      } catch (apiErr) {
        console.warn("Failed to persist webcam face enrollment to Central:", apiErr);
      }

      setEnrollStatus({
        loading: false,
        success: `Snap captured! Registered '${targetName}' to Watchlist.`,
        error: null
      });
      setEnrollForm(prev => ({ ...prev, name: '' }));
    } catch (err) {
      setEnrollStatus({ loading: false, success: null, error: "Webcam snapshot failed." });
    }
  };

  const activeAlerts = engineMode === 'CLIENT-SIDE' ? localAlerts : alerts;

  return (
    <div className="bg-slate-950 text-slate-100 min-h-screen lg:h-screen lg:overflow-hidden font-sans flex flex-col antialiased">
      {/* ========================================================= */}
      {/* 1. PROFESSIONAL SOC HEADER BAR                            */}
      {/* ========================================================= */}
      <header className="h-14 border-b border-slate-800/90 bg-slate-900/80 px-5 flex items-center justify-between sticky top-0 z-30 flex-shrink-0 select-none">
        {/* Left Brand Identity */}
        <div className="flex items-center gap-3">
          <Shield className="w-5 h-5 text-blue-400" />
          <div className="flex items-baseline gap-2">
            <h1 className="text-sm font-bold tracking-wider font-mono text-slate-100 uppercase">
              SECURE OPERATIONS CENTER
            </h1>
            <span className="text-[11px] font-mono text-slate-500 font-normal">
              SURVEILLANCE MATRIX
            </span>
          </div>
        </div>

        {/* Right Header Controls */}
        <div className="flex items-center gap-2.5">
          {/* View Switcher: Camera Grid vs GIS Map */}
          <div className="flex bg-slate-950 p-0.5 rounded-lg border border-slate-800 text-[11px] font-mono">
            <button
              onClick={() => setActiveView('CAMERAS')}
              className={`px-3 py-1 rounded transition-all cursor-pointer font-semibold flex items-center gap-1.5 ${activeView === 'CAMERAS' ? 'bg-slate-800 text-slate-100 border border-slate-700' : 'text-slate-400 hover:text-slate-200'}`}
            >
              <Grid className="w-3.5 h-3.5" />
              <span>CAMERAS</span>
            </button>
            <button
              onClick={() => setActiveView('MAP')}
              className={`px-3 py-1 rounded transition-all cursor-pointer font-semibold flex items-center gap-1.5 ${activeView === 'MAP' ? 'bg-slate-800 text-slate-100 border border-slate-700' : 'text-slate-400 hover:text-slate-200'}`}
            >
              <Layers className="w-3.5 h-3.5" />
              <span>GIS MAP</span>
            </button>
          </div>

          <div className="h-4 w-px bg-slate-800"></div>

          {/* System Status Indicator */}
          <button
            onClick={() => setIsStatusModalOpen(true)}
            className="px-2.5 py-1 rounded-lg border text-xs font-mono font-semibold flex items-center gap-1.5 bg-slate-950/80 border-slate-800 hover:border-slate-700 text-slate-300 transition-colors cursor-pointer"
            title="Inspect Cluster Health"
          >
            <span className={`w-2 h-2 rounded-full ${connected ? 'bg-emerald-400 animate-pulse' : 'bg-rose-500'}`}></span>
            <span>SYSTEM {connected ? 'OPERATIONAL' : 'DISCONNECTED'}</span>
          </button>

          {/* Watchlist Drawer Trigger */}
          <button
            onClick={() => setIsWatchlistOpen(true)}
            className="px-3 py-1 rounded-lg border text-xs font-semibold font-mono flex items-center gap-1.5 bg-slate-950/80 border-slate-800 hover:border-blue-500/50 hover:text-blue-300 text-slate-300 transition-colors cursor-pointer"
          >
            <UserPlus className="w-3.5 h-3.5 text-blue-400" />
            <span>WATCHLIST</span>
            <span className="text-[10px] text-cyan-400 font-bold ml-0.5">({enrolledTargets.length + enrolledPlates.length})</span>
          </button>

          {/* Configuration Drawer Trigger */}
          <button
            onClick={() => setIsConfigOpen(true)}
            className="px-3 py-1 rounded-lg border text-xs font-semibold font-mono flex items-center gap-1.5 bg-slate-950/80 border-slate-800 hover:border-rose-500/50 hover:text-rose-300 text-slate-300 transition-colors cursor-pointer"
          >
            <Sliders className="w-3.5 h-3.5 text-rose-400" />
            <span>CONFIG</span>
          </button>
        </div>
      </header>

      {/* ========================================================= */}
      {/* 2. MAIN WORKSPACE: LARGE CAMERA MATRIX + ALERT COLUMN      */}
      {/* ========================================================= */}
      <div className="flex-1 flex flex-col lg:flex-row min-h-0 min-w-0 overflow-hidden">
        {/* Left / Center Dominant Camera Workspace (~70-75% Width) */}
        <main className="flex-1 flex flex-col min-h-0 min-w-0 p-3 lg:p-3.5 overflow-hidden">
          {activeView === 'CAMERAS' ? (
            activeCameras.length === 0 ? (
              <div className="flex-1 h-full min-h-0 flex flex-col items-center justify-center p-6 text-center bg-slate-950/80 border border-slate-800 rounded-xl">
                <Video className="w-12 h-12 text-slate-600 mb-3" />
                <h3 className="text-sm font-mono font-bold text-slate-300 uppercase tracking-wide">
                  No Active Cameras
                </h3>
                <p className="text-xs text-slate-500 font-mono mt-1 max-w-sm">
                  All surveillance nodes are disabled or unconfigured. Enable or add a camera in System Configuration.
                </p>
                <button
                  onClick={() => setIsConfigOpen(true)}
                  className="mt-4 px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg text-xs font-mono font-bold transition-colors cursor-pointer"
                >
                  + Open Camera Management
                </button>
              </div>
            ) : (
              <div
                className="flex-1 h-full min-h-0 grid gap-3 overflow-hidden"
                style={{
                  gridTemplateRows: activeCameras.length > 1 ? '65fr 35fr' : '1fr',
                  gridTemplateColumns: `repeat(${Math.max(2, activeCameras.length - 1)}, minmax(0, 1fr))`
                }}
              >
                {activeCameras.map((cam) => {
                  const isPrimary = cam.id === (slotOrder[0] || activeCameras[0]?.id);
                  const secondaryIdx = slotOrder.slice(1).indexOf(cam.id);
                  const colIdx = secondaryIdx >= 0 ? secondaryIdx + 1 : 1;

                  return (
                    <div
                      key={cam.id}
                      className={`w-full h-full min-h-0 min-w-0 bg-slate-950 border border-slate-800/80 rounded-xl overflow-hidden relative shadow-lg flex items-center justify-center transition-all duration-300 ${
                        !isPrimary ? 'cursor-pointer hover:border-cyan-500/60' : ''
                      }`}
                      style={{
                        gridRow: isPrimary ? 1 : 2,
                        gridColumn: isPrimary ? '1 / -1' : `${colIdx} / span 1`
                      }}
                      onClick={() => {
                        if (!isPrimary) {
                          handleFocusCamera(cam.id);
                        }
                      }}
                    >
                      {cam.isLiveStream ? (
                        <CameraFeed
                          cameraId={cam.id}
                          cameraName={cam.name}
                          streamUrl={cam.streamUrl}
                          latitude={cam.lat}
                          longitude={cam.lng}
                          enrolledTargets={enrolledTargets}
                          enrolledPlates={enrolledPlates}
                          onDetection={handleDetection}
                          onLocationClick={highlightCameraPin}
                          onFullscreen={() => setFullscreenCamera(cam)}
                          isPrimary={isPrimary}
                          onFocus={handleFocusCamera}
                        />
                      ) : (
                        <div className="relative w-full h-full flex flex-col items-center justify-center p-3 text-center gap-1.5 select-none group">
                          <div className="absolute top-2.5 right-2.5 z-20 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setFullscreenCamera(cam);
                              }}
                              title="Expand Fullscreen"
                              className="p-1 rounded bg-slate-950/80 border border-slate-800 text-slate-400 hover:text-white hover:border-slate-600 backdrop-blur-sm transition-colors cursor-pointer"
                            >
                              <Maximize2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                          <Video className="w-6 h-6 text-slate-700 group-hover:text-slate-500 transition-colors" />
                          <span className="text-xs font-mono font-bold text-slate-400 uppercase tracking-wider">
                            {cam.id} · {cam.name}
                          </span>
                          <span className="text-[10px] font-mono text-slate-600">
                            STANDBY · Slot Available for RTSP / IP Stream
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setModalForm(prev => ({ ...prev, targetSlot: cam.id.includes('03') ? 'Camera Slot 3' : 'Camera Slot 2' }));
                              setIsConfigOpen(true);
                            }}
                            className="mt-1 text-[10px] font-mono font-bold text-cyan-400 hover:text-cyan-300 border border-slate-800 bg-slate-900/80 px-2.5 py-1 rounded transition-colors cursor-pointer"
                          >
                            + Configure IP Stream
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )
          ) : (
            /* Tactical Spatio-Temporal GIS Map View */
            <div className="flex-1 w-full bg-slate-950 rounded-xl relative border border-slate-800/80 overflow-hidden min-h-0 flex flex-col">
              <MapContainer
                center={mapCenter}
                zoom={14}
                scrollWheelZoom={true}
                className="w-full h-full min-h-[400px] z-0"
              >
                <TileLayer
                  attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                  url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                />
                <RecenterMap coords={mapCenter} />

                {laptopLocation && (
                  <Marker
                    position={laptopLocation}
                    icon={userLocationIcon}
                    draggable={true}
                    eventHandlers={{
                      dragend: (e) => {
                        const marker = e.target;
                        const position = marker.getLatLng();
                        const newCoords = [position.lat, position.lng];
                        setLaptopLocation(newCoords);
                        setMapCenter(newCoords);
                        setLocationSource('DRAGGED');
                      }
                    }}
                  >
                    <Popup>
                      <div className="text-xs font-mono text-slate-200">
                        <div className="font-bold text-cyan-400">💻 CURRENT LAPTOP LOCATION</div>
                        <div className="mt-1">GPS: {laptopLocation[0].toFixed(5)}, {laptopLocation[1].toFixed(5)}</div>
                      </div>
                    </Popup>
                  </Marker>
                )}

                {cameras.map((cam) => (
                  <Marker key={cam.id} position={[cam.lat, cam.lng]} icon={cameraIcon}>
                    <Popup>
                      <div className="text-xs font-mono text-slate-200">
                        <div className="font-bold text-blue-400">📹 {cam.id} // {cam.name}</div>
                        <div className="mt-1">📍 {cam.address}</div>
                      </div>
                    </Popup>
                  </Marker>
                ))}

                {activeAlerts.map((alert, idx) => {
                  if (!alert.lat || !alert.lng) return null;
                  return (
                    <Marker
                      key={alert.id || alert.timestamp || idx}
                      position={[alert.lat, alert.lng]}
                      icon={alertIcon}
                    >
                      <Popup>
                        <div className="text-xs font-mono text-slate-200">
                          <div className="font-bold text-rose-400 uppercase">
                            🚨 {alert.event_type || 'ALERT'}
                          </div>
                          <div>TARGET: {alert.subject || 'UNKNOWN'}</div>
                          <div>SEVERITY: {alert.severity || 'HIGH'}</div>
                        </div>
                      </Popup>
                    </Marker>
                  );
                })}
              </MapContainer>
            </div>
          )}
        </main>

        {/* Right Fixed-Width Alert Column (~25-30% Width) */}
        <div className="w-full lg:w-80 xl:w-96 2xl:w-[440px] flex-shrink-0 h-80 lg:h-full overflow-hidden">
          <AlertFeedPanel
            alerts={activeAlerts}
            onAcknowledgeAlert={handleAcknowledgeAlert}
            onOpenClip={handleOpenClip}
            onCameraSelect={highlightCameraPin}
            engineMode={engineMode}
            onClearLocalAlerts={() => {
              setLocalAlerts([]);
              localStorage.removeItem('watchlist_alerts');
            }}
            resolveEvidenceUrl={resolveEvidenceUrl}
            getAuthHeaders={getAuthHeaders}
          />
        </div>
      </div>

      {/* ========================================================= */}
      {/* 3. MODULAR FULLSCREEN CAMERA LIGHTBOX MODAL               */}
      {/* ========================================================= */}
      {fullscreenCamera && (
        <CameraFullscreenModal
          camera={fullscreenCamera}
          onClose={() => setFullscreenCamera(null)}
          enrolledTargets={enrolledTargets}
          enrolledPlates={enrolledPlates}
          onDetection={handleDetection}
          onLocationClick={highlightCameraPin}
        />
      )}

      {/* ========================================================= */}
      {/* 4. WATCHLIST & ENROLLMENT SLIDE-OVER DRAWER               */}
      {/* ========================================================= */}
      <WatchlistDrawer
        isOpen={isWatchlistOpen}
        onClose={() => setIsWatchlistOpen(false)}
        enrollForm={enrollForm}
        setEnrollForm={setEnrollForm}
        selectedFile={selectedFile}
        setSelectedFile={setSelectedFile}
        enrollStatus={enrollStatus}
        handleEnrollSubmit={handleEnrollSubmit}
        handleSnapWebcamFace={handleSnapWebcamFace}
        enrolledTargets={enrolledTargets}
        enrolledPlates={enrolledPlates}
      />

      {/* ========================================================= */}
      {/* 5. CONFIGURATION & RESTRICTED RULES SLIDE-OVER DRAWER      */}
      {/* ========================================================= */}
      <ConfigDrawer
        isOpen={isConfigOpen}
        onClose={() => setIsConfigOpen(false)}
        cameras={cameras}
        onUpdateCamera={handleUpdateCamera}
        onToggleCamera={handleToggleCamera}
        onDeleteCamera={handleDeleteCamera}
        cameraLoading={cameraLoading}
        cameraActionStatus={cameraActionStatus}
        alertCount={activeAlerts.length}
        onClearAlerts={handleClearAlerts}
        clearAlertsLoading={clearAlertsLoading}
        restrictedRules={restrictedRules}
        ruleForm={ruleForm}
        setRuleForm={setRuleForm}
        ruleStatus={ruleStatus}
        handleAddRule={handleAddRule}
        handleToggleRule={handleToggleRule}
        handleDeleteRule={handleDeleteRule}
        modalForm={modalForm}
        setModalForm={setModalForm}
        modalLoading={modalLoading}
        modalError={modalError}
        handleModalSubmit={handleModalSubmit}
        laptopLocation={laptopLocation}
        locationSource={locationSource}
        triggerGpsSync={triggerGpsSync}
        gpsError={gpsError}
        testLoading={testLoading}
        testResult={testResult}
        handleTestConnection={handleTestConnection}
        setTestResult={setTestResult}
      />

      {/* ========================================================= */}
      {/* 6. SYSTEM STATUS HEALTH INSPECTION MODAL                  */}
      {/* ========================================================= */}
      <SystemStatusModal
        isOpen={isStatusModalOpen}
        onClose={() => setIsStatusModalOpen(false)}
        connected={connected}
        CENTRAL_API_BASE={CENTRAL_API_BASE}
        getAuthHeaders={getAuthHeaders}
      />

      {/* Toast Notification */}
      {toastMessage && (
        <div className="fixed bottom-5 right-5 bg-slate-900 border border-emerald-800 text-emerald-400 px-4 py-2.5 rounded-xl shadow-2xl flex items-center gap-2 z-50 animate-fadeIn">
          <CheckCircle className="w-4 h-4 text-emerald-400" />
          <span className="text-xs font-mono font-bold">{toastMessage}</span>
        </div>
      )}
    </div>
  );
}
