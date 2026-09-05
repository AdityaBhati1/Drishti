import React, { useRef, useEffect, useState } from 'react';
import { Camera, RefreshCw, AlertCircle, Eye, AlertTriangle, ShieldCheck, MapPin, Maximize2, Minimize2 } from 'lucide-react';
import { CURRENT_NODE_LOCATION } from '../config/location';

const AI_BACKEND_BASE = import.meta.env.VITE_AI_BACKEND_URL || 'http://localhost:8002';
// The browser-upload scanner is retained only for explicit experimental work.
// Canonical production inference happens at Edge/Fog, not in the browser.
const ENABLE_EXPERIMENTAL_BROWSER_AI = import.meta.env.VITE_ENABLE_EXPERIMENTAL_BROWSER_AI === 'true';
const HAS_AI_BACKEND = Boolean(AI_BACKEND_BASE);

// Singletons for offscreen canvas operations to completely avoid GPU/memory leaks & GC freezes
const sharedFrameCanvas = typeof document !== 'undefined' ? document.createElement('canvas') : null;
const sharedSkinCanvas = typeof document !== 'undefined' ? document.createElement('canvas') : null;
const sharedStructureCanvas = typeof document !== 'undefined' ? document.createElement('canvas') : null;
const sharedSignatureCanvas = typeof document !== 'undefined' ? document.createElement('canvas') : null;

function formatExactTimestamp(dateObj = new Date()) {
    const year = dateObj.getFullYear();
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    const hours = String(dateObj.getHours()).padStart(2, '0');
    const minutes = String(dateObj.getMinutes()).padStart(2, '0');
    const seconds = String(dateObj.getSeconds()).padStart(2, '0');
    const ms = String(dateObj.getMilliseconds()).padStart(3, '0');
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}.${ms}`;
}

function normalizePlateChar(char) {
    const map = { 'O': '0', 'Q': '0', 'I': '1', 'L': '1', 'Z': '2', 'S': '5', 'B': '8', 'G': '6', 'T': '7' };
    return map[char] || char;
}

function isFuzzyPlateMatch(detectedStr, enrolledStr) {
    if (!detectedStr || !enrolledStr) return false;
    const d = detectedStr.toUpperCase().replace(/[^A-Z0-9]/g, '');
    const e = enrolledStr.toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!d || !e) return false;

    if (d.includes(e) || e.includes(d)) return true;

    const normD = d.split('').map(normalizePlateChar).join('');
    const normE = e.split('').map(normalizePlateChar).join('');
    if (normD.includes(normE) || normE.includes(normD)) return true;

    if (Math.abs(normD.length - normE.length) <= 2 && normE.length >= 4) {
        let diffs = 0;
        const minLen = Math.min(normD.length, normE.length);
        for (let i = 0; i < minLen; i++) {
            if (normD[i] !== normE[i]) diffs++;
        }
        diffs += Math.abs(normD.length - normE.length);
        if (diffs <= 2) return true;
    }

    return false;
}

function getCropSkinRatio(imgSource, cropBox) {
    try {
        if (!sharedSkinCanvas) return 0;
        const sampleW = 32;
        const sampleH = 32;
        if (sharedSkinCanvas.width !== sampleW || sharedSkinCanvas.height !== sampleH) {
            sharedSkinCanvas.width = sampleW;
            sharedSkinCanvas.height = sampleH;
        }
        const ctx = sharedSkinCanvas.getContext('2d');
        if (!ctx) return 0;

        const srcW = imgSource.videoWidth || imgSource.naturalWidth || imgSource.width || 640;
        const srcH = imgSource.videoHeight || imgSource.naturalHeight || imgSource.height || 480;

        if (!srcW || !srcH) return 0;

        const sx = Math.max(0, srcW * cropBox.x);
        const sy = Math.max(0, srcH * cropBox.y);
        const sw = Math.min(srcW - sx, srcW * cropBox.w);
        const sh = Math.min(srcH - sy, srcH * cropBox.h);

        if (sw <= 0 || sh <= 0) return 0;

        ctx.drawImage(imgSource, sx, sy, sw, sh, 0, 0, sampleW, sampleH);
        const imgData = ctx.getImageData(0, 0, sampleW, sampleH).data;

        let skinPixels = 0;
        const totalPixels = sampleW * sampleH;

        for (let i = 0; i < totalPixels; i++) {
            const r = imgData[i * 4];
            const g = imgData[i * 4 + 1];
            const b = imgData[i * 4 + 2];

            const isSkin = (r > 40 && g > 25 && b > 15 &&
                (Math.max(r, g, b) - Math.min(r, g, b) > 12) &&
                Math.abs(r - g) > 12 && r > g && r > b) ||
                ((128 - 0.168 * r - 0.331 * g + 0.500 * b >= 77) &&
                 (128 - 0.168 * r - 0.331 * g + 0.500 * b <= 127) &&
                 (128 + 0.500 * r - 0.418 * g - 0.081 * b >= 133) &&
                 (128 + 0.500 * r - 0.418 * g - 0.081 * b <= 173));

            if (isSkin) skinPixels++;
        }

        return skinPixels / totalPixels;
    } catch (e) {
        return 0;
    }
}

function hasFaceStructure(imgSource, cropBox) {
    try {
        if (!sharedStructureCanvas) return false;
        const sampleW = 24;
        const sampleH = 24;
        if (sharedStructureCanvas.width !== sampleW || sharedStructureCanvas.height !== sampleH) {
            sharedStructureCanvas.width = sampleW;
            sharedStructureCanvas.height = sampleH;
        }
        const ctx = sharedStructureCanvas.getContext('2d');
        if (!ctx) return false;

        const srcW = imgSource.videoWidth || imgSource.naturalWidth || imgSource.width || 640;
        const srcH = imgSource.videoHeight || imgSource.naturalHeight || imgSource.height || 480;

        if (!srcW || !srcH) return false;

        const sx = Math.max(0, srcW * cropBox.x);
        const sy = Math.max(0, srcH * cropBox.y);
        const sw = Math.min(srcW - sx, srcW * cropBox.w);
        const sh = Math.min(srcH - sy, srcH * cropBox.h);

        if (sw <= 0 || sh <= 0) return false;

        ctx.drawImage(imgSource, sx, sy, sw, sh, 0, 0, sampleW, sampleH);
        const imgData = ctx.getImageData(0, 0, sampleW, sampleH).data;

        let totalLum = 0;
        const lums = new Float32Array(sampleW * sampleH);

        for (let i = 0; i < lums.length; i++) {
            const r = imgData[i * 4];
            const g = imgData[i * 4 + 1];
            const b = imgData[i * 4 + 2];
            lums[i] = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0;
            totalLum += lums[i];
        }

        const meanLum = totalLum / lums.length;
        let variance = 0;
        for (let i = 0; i < lums.length; i++) {
            variance += (lums[i] - meanLum) * (lums[i] - meanLum);
        }
        const stdDev = Math.sqrt(variance / lums.length);

        return stdDev >= 0.038;
    } catch (e) {
        return false;
    }
}

const targetSignatureCache = new Map();

function getCanvasImageSignature(imgSource, targetWidth = 24, targetHeight = 24, cropBox = null) {
    try {
        if (!sharedSignatureCanvas) return null;
        if (sharedSignatureCanvas.width !== targetWidth || sharedSignatureCanvas.height !== targetHeight) {
            sharedSignatureCanvas.width = targetWidth;
            sharedSignatureCanvas.height = targetHeight;
        }
        const ctx = sharedSignatureCanvas.getContext('2d');
        if (!ctx) return null;

        const srcW = imgSource.videoWidth || imgSource.naturalWidth || imgSource.width || 640;
        const srcH = imgSource.videoHeight || imgSource.naturalHeight || imgSource.height || 480;

        if (cropBox && srcW > 0 && srcH > 0) {
            const sx = Math.max(0, srcW * cropBox.x);
            const sy = Math.max(0, srcH * cropBox.y);
            const sw = Math.min(srcW - sx, srcW * cropBox.w);
            const sh = Math.min(srcH - sy, srcH * cropBox.h);
            if (sw > 0 && sh > 0) {
                ctx.drawImage(imgSource, sx, sy, sw, sh, 0, 0, targetWidth, targetHeight);
            } else {
                ctx.drawImage(imgSource, 0, 0, targetWidth, targetHeight);
            }
        } else {
            ctx.drawImage(imgSource, 0, 0, targetWidth, targetHeight);
        }

        const imgData = ctx.getImageData(0, 0, targetWidth, targetHeight).data;
        const grid = new Float32Array(targetWidth * targetHeight);

        for (let i = 0; i < grid.length; i++) {
            const r = imgData[i * 4];
            const g = imgData[i * 4 + 1];
            const b = imgData[i * 4 + 2];
            grid[i] = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0;
        }

        const vec = new Float32Array(targetWidth * targetHeight * 2);
        let idx = 0;
        let sum = 0;

        for (let y = 0; y < targetHeight; y++) {
            for (let x = 0; x < targetWidth; x++) {
                const center = grid[y * targetWidth + x];
                const right = grid[y * targetWidth + Math.min(x + 1, targetWidth - 1)];
                const down = grid[Math.min(y + 1, targetHeight - 1) * targetWidth + x];

                const gradX = right - center;
                const gradY = down - center;

                vec[idx++] = center;
                vec[idx++] = Math.sqrt(gradX * gradX + gradY * gradY);
                sum += center + vec[idx - 1];
            }
        }

        const mean = sum / vec.length;
        let sumSq = 0;
        for (let i = 0; i < vec.length; i++) {
            vec[i] -= mean;
            sumSq += vec[i] * vec[i];
        }

        const std = Math.sqrt(sumSq);
        if (std > 0.0001) {
            for (let i = 0; i < vec.length; i++) vec[i] /= std;
        }
        return vec;
    } catch (e) {
        return null;
    }
}

function getTargetImageSignature(imageSrc) {
    if (!imageSrc) return Promise.resolve(null);
    if (targetSignatureCache.has(imageSrc)) {
        return Promise.resolve(targetSignatureCache.get(imageSrc));
    }
    return new Promise((resolve) => {
        const timeoutId = setTimeout(() => resolve(null), 1200);
        const img = new Image();
        if (imageSrc.startsWith('http://') || imageSrc.startsWith('https://')) {
            img.crossOrigin = 'anonymous';
        }
        img.onload = () => {
            clearTimeout(timeoutId);
            const sig = getCanvasImageSignature(img, 24, 24, null);
            if (sig) targetSignatureCache.set(imageSrc, sig);
            resolve(sig);
        };
        img.onerror = (err) => {
            clearTimeout(timeoutId);
            console.warn("Failed to load target portrait image:", err);
            resolve(null);
        };
        img.src = imageSrc;
    });
}

export function resolveDefaultStreamUrl() {
    if (typeof window !== 'undefined' && window.__HOST_STREAM_URL__) {
        return window.__HOST_STREAM_URL__;
    }
    const envUrl = import.meta.env.VITE_HOST_STREAM_URL || import.meta.env.VITE_CAMERA_STREAM_URL;
    if (envUrl) return envUrl;
    const host = (typeof window !== 'undefined' && window.location && window.location.hostname)
        ? window.location.hostname
        : 'localhost';
    return `http://${host}:8085/video_feed`;
}

export default function CameraFeed({
    cameraId = 'CAM_01',
    cameraName = 'DOWNTOWN_NODE',
    latitude = null,
    longitude = null,
    streamUrl = null,
    enrolledTargets = [],
    enrolledPlates = [],
    onDetection = null,
    onLocationClick = null,
    onFullscreen = null,
    isFullscreen = false,
    showControls = true,
    isPrimary = false,
    onFocus = null
}) {
    const imgRef = useRef(null);
    const canvasRef = useRef(null);
    const timestampRef = useRef(null);
    const activeDetectionsRef = useRef([]);
    const telemetryTracksRef = useRef([]);
    const [trackCount, setTrackCount] = useState(0);
    const lastAlertTimeRef = useRef(new Map());
    const isScanningFaceRef = useRef(false);
    const isScanningPlateRef = useRef(false);
    const frameCounterRef = useRef(0);
    const lastProcessedFaceFrameRef = useRef(0);
    const lastProcessedPlateFrameRef = useRef(0);
    const lastScanTickRef = useRef(Date.now());

    // Match incoming camera IDs (e.g. CAM_01 vs cam-main-entrance)
    const isMatchingCamera = (incomingCamId, myCamId) => {
        if (!incomingCamId || !myCamId) return false;
        if (incomingCamId === myCamId) return true;
        const normIn = incomingCamId.toLowerCase().replace(/[^a-z0-9]/g, '');
        const normMy = myCamId.toLowerCase().replace(/[^a-z0-9]/g, '');
        if (normIn === normMy) return true;
        if ((normIn.includes('cam01') || normIn.includes('mainentrance')) && (normMy.includes('cam01') || normMy.includes('mainentrance'))) return true;
        if ((normIn.includes('cam02') || normIn.includes('slot2')) && (normMy.includes('cam02') || normMy.includes('slot2'))) return true;
        if ((normIn.includes('cam03') || normIn.includes('slot3')) && (normMy.includes('cam03') || normMy.includes('slot3'))) return true;
        return false;
    };

    // Real-time perception telemetry subscriber (Edge YOLO + Fog FRS + Fog ANPR)
    useEffect(() => {
        const handleTelemetry = (e) => {
            const data = e.detail;
            if (!data || !isMatchingCamera(data.camera_id, cameraId)) return;
            const now = Date.now();
            const incomingTracks = data.tracks || [];
            telemetryTracksRef.current = incomingTracks.map(t => ({
                ...t,
                receivedAt: now
            }));
            setTrackCount(incomingTracks.length);
        };

        window.addEventListener('cctv-telemetry', handleTelemetry);
        return () => window.removeEventListener('cctv-telemetry', handleTelemetry);
    }, [cameraId]);

    // Stream URL resolution & robust connection handling
    const isLocalWebcamNode = cameraId === 'CAM_01';
    const [currentStreamUrl, setCurrentStreamUrl] = useState(() => streamUrl || (isLocalWebcamNode ? resolveDefaultStreamUrl() : null));
    const [streamLoaded, setStreamLoaded] = useState(false);
    const [fallbackAttempted, setFallbackAttempted] = useState(false);

    useEffect(() => {
        if (streamUrl) {
            setCurrentStreamUrl(streamUrl);
        } else if (isLocalWebcamNode) {
            setCurrentStreamUrl(resolveDefaultStreamUrl());
        } else {
            setCurrentStreamUrl(null);
        }
    }, [streamUrl, cameraId, isLocalWebcamNode]);

    const triggerAlertThrottled = (subjectKey, alertData) => {
        const now = Date.now();
        const lastTime = lastAlertTimeRef.current.get(subjectKey) || 0;
        if (now - lastTime > 4000) {
            lastAlertTimeRef.current.set(subjectKey, now);
            if (onDetectionRef.current) {
                onDetectionRef.current(alertData);
            }
        }
    };

    const onDetectionRef = useRef(onDetection);
    const onLocationClickRef = useRef(onLocationClick);
    const enrolledPlatesRef = useRef(enrolledPlates);
    const enrolledTargetsRef = useRef(enrolledTargets);

    useEffect(() => { onDetectionRef.current = onDetection; }, [onDetection]);
    useEffect(() => { onLocationClickRef.current = onLocationClick; }, [onLocationClick]);
    useEffect(() => { enrolledPlatesRef.current = enrolledPlates; }, [enrolledPlates]);
    useEffect(() => { enrolledTargetsRef.current = enrolledTargets; }, [enrolledTargets]);

    const activeLocation = {
        address: 'Primary Surveillance Hub',
        lat: latitude ?? CURRENT_NODE_LOCATION.lat,
        lng: longitude ?? CURRENT_NODE_LOCATION.lng
    };

    const activeLocationRef = useRef(activeLocation);
    useEffect(() => {
        activeLocationRef.current = activeLocation;
    }, [latitude, longitude, activeLocation.address]);

    const [error, setError] = useState(null);
    const [isScanning, setIsScanning] = useState(false);
    const [lastMatch, setLastMatch] = useState(null);
    const [aiBackendOffline, setAiBackendOffline] = useState(false);

    useEffect(() => {
        if (lastMatch) {
            const timer = setTimeout(() => {
                setLastMatch(null);
            }, 1200);
            return () => clearTimeout(timer);
        }
    }, [lastMatch]);

    // Clear stale bounding box overlays after 1.5 seconds of no update
    useEffect(() => {
        const cleanupInterval = setInterval(() => {
            if (activeDetectionsRef.current.length > 0) {
                const now = Date.now();
                activeDetectionsRef.current = activeDetectionsRef.current.filter(d => (now - d.timestamp) < 1500);
            }
        }, 500);
        return () => clearInterval(cleanupInterval);
    }, []);

    // Initialize overlay canvas dimensions and handle stream events
    useEffect(() => {
        if (canvasRef.current && (!canvasRef.current.width || canvasRef.current.width === 300)) {
            canvasRef.current.width = 640;
            canvasRef.current.height = 480;
        }
    }, []);

    const handleImageLoad = () => {
        setStreamLoaded(true);
        setError(null);
        if (imgRef.current && canvasRef.current) {
            const nw = imgRef.current.naturalWidth || 640;
            const nh = imgRef.current.naturalHeight || 480;
            if (canvasRef.current.width !== nw || canvasRef.current.height !== nh) {
                canvasRef.current.width = nw;
                canvasRef.current.height = nh;
            }
        }
    };

    const handleImageError = () => {
        if (!currentStreamUrl) return;
        setStreamLoaded(false);
        // Fallback between localhost and 127.0.0.1 if initial host fails (exclusively for CAM_01 local streamer)
        if (isLocalWebcamNode && !fallbackAttempted && typeof window !== 'undefined') {
            const hostname = window.location.hostname || 'localhost';
            if (hostname === 'localhost' && currentStreamUrl?.includes('localhost')) {
                setFallbackAttempted(true);
                setCurrentStreamUrl('http://127.0.0.1:8085/video_feed');
                return;
            } else if (hostname === '127.0.0.1' && currentStreamUrl?.includes('127.0.0.1')) {
                setFallbackAttempted(true);
                setCurrentStreamUrl('http://localhost:8085/video_feed');
                return;
            }
        }
        if (isLocalWebcamNode || (currentStreamUrl && currentStreamUrl.includes(':8085'))) {
            setError('Connecting to camera streamer (http://localhost:8085/video_feed)...');
        } else {
            setError(`Connecting to camera stream (${cameraId})...`);
        }
        setTimeout(() => {
            if (imgRef.current && currentStreamUrl) {
                const url = currentStreamUrl;
                imgRef.current.src = '';
                imgRef.current.src = url;
            }
        }, 2000);
    };

    const captureFrameBlob = (mediaSource, width, height) => {
        return new Promise((resolve) => {
            try {
                if (!sharedFrameCanvas) {
                    resolve(null);
                    return;
                }
                if (sharedFrameCanvas.width !== width || sharedFrameCanvas.height !== height) {
                    sharedFrameCanvas.width = width;
                    sharedFrameCanvas.height = height;
                }
                const ctx = sharedFrameCanvas.getContext('2d');
                if (!ctx) {
                    resolve(null);
                    return;
                }
                ctx.drawImage(mediaSource, 0, 0, width, height);
                sharedFrameCanvas.toBlob((blob) => resolve(blob), 'image/jpeg', 0.90);
            } catch (e) {
                resolve(null);
            }
        });
    };

    // 2. Snapshot Scanning Loop (Plates, Watchlist Faces, & Un-enrolled Intruder Detection)
    useEffect(() => {
        if (!ENABLE_EXPERIMENTAL_BROWSER_AI) {
            return undefined;
        }
        let isProcessingFrame = false;

        const scanInterval = setInterval(async () => {
            const plates = enrolledPlatesRef.current;
            const targets = enrolledTargetsRef.current;
            const activeLoc = activeLocationRef.current;

            // Watchdog lock self-healing: if stalled >3.0s, force-reset locks
            if (Date.now() - lastScanTickRef.current > 3000) {
                isScanningFaceRef.current = false;
                isScanningPlateRef.current = false;
                isProcessingFrame = false;
            }
            lastScanTickRef.current = Date.now();

            if (isProcessingFrame) return;

            const mediaSource = imgRef.current;
            if (!mediaSource) return;

            const srcWidth = mediaSource.naturalWidth || 640;
            const srcHeight = mediaSource.naturalHeight || 480;

            if (!srcWidth || !srcHeight) return;

            if (canvasRef.current && (canvasRef.current.width !== srcWidth || canvasRef.current.height !== srcHeight)) {
                canvasRef.current.width = srcWidth;
                canvasRef.current.height = srcHeight;
            }

            try {
                isProcessingFrame = true;
                setIsScanning(true);

                const blob = await captureFrameBlob(mediaSource, srcWidth, srcHeight);
                if (!blob) {
                    isProcessingFrame = false;
                    setIsScanning(false);
                    return;
                }

                frameCounterRef.current += 1;
                const currentFrameId = frameCounterRef.current;
                const currentTimestamp = Date.now() / 1000.0;
                let newDetections = [];

                // Define parallel scanner tasks with AbortController & safe finally locks
                const scanPlateTask = async () => {
                    if (!plates || plates.length === 0 || isScanningPlateRef.current) return;
                    isScanningPlateRef.current = true;
                    
                    const controller = new AbortController();
                    const timeoutId = setTimeout(() => controller.abort(), 2500);

                    try {
                        const plateFormData = new FormData();
                        plateFormData.append('file', blob, 'frame.jpg');
                        plateFormData.append('frame_id', currentFrameId.toString());
                        plateFormData.append('timestamp', currentTimestamp.toString());

                        const response = await fetch(`${AI_BACKEND_BASE}/api/scan-plate`, {
                            method: 'POST',
                            body: plateFormData,
                            signal: controller.signal
                        });

                        if (response.ok) {
                            setAiBackendOffline(false);
                            const data = await response.json();
                            if (data.frame_id && data.frame_id < lastProcessedPlateFrameRef.current) {
                                return;
                            }
                            if (data.frame_id) lastProcessedPlateFrameRef.current = data.frame_id;

                            if (data.results && data.results.length > 0) {
                                data.results.forEach((item) => {
                                    if (!item) return;
                                    const rawDetected = item.text || '';
                                    const matched = plates.find((plate) => isFuzzyPlateMatch(rawDetected, plate));

                                    if (matched) {
                                        const exactTime = formatExactTimestamp(new Date());
                                        setLastMatch(`PLATE: ${matched}`);

                                        if (item.bbox) {
                                            newDetections.push({
                                                type: 'PLATE',
                                                label: `PLATE: ${matched}`,
                                                bbox: item.bbox,
                                                confidence: item.confidence ? Math.round(item.confidence * 100) : 95,
                                                timestamp: currentTimestamp
                                            });
                                        }

                                        triggerAlertThrottled(`PLATE_${matched}`, {
                                            id: `ALERT_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
                                            eventType: 'PLATE MATCH',
                                            subject: matched,
                                            details: `Target plate identified on ${cameraId}: ${rawDetected}`,
                                            lat: activeLoc.lat,
                                            lng: activeLoc.lng,
                                            address: activeLoc.address,
                                            cameraId: cameraId,
                                            cameraName: cameraName,
                                            timestamp: exactTime,
                                            confidence: item.confidence ? Math.round(item.confidence * 100) : 95,
                                            severity: 'CRITICAL',
                                        });
                                    }
                                });
                            }
                        }
                    } catch (err) {
                        setAiBackendOffline(false);
                    } finally {
                        clearTimeout(timeoutId);
                        isScanningPlateRef.current = false;
                    }
                };

                const scanFaceTask = async () => {
                    if (isScanningFaceRef.current) return;
                    isScanningFaceRef.current = true;

                    try {
                        let backendAvailable = false;
                        const controller = new AbortController();
                        const timeoutId = setTimeout(() => controller.abort(), 2500);

                        try {
                            const faceFormData = new FormData();
                            faceFormData.append('file', blob, 'frame.jpg');
                            faceFormData.append('targets', typeof targets === 'string' ? targets : JSON.stringify(targets || []));
                            faceFormData.append('frame_id', currentFrameId.toString());
                            faceFormData.append('timestamp', currentTimestamp.toString());

                            const response = await fetch(`${AI_BACKEND_BASE}/api/scan-face`, {
                                method: 'POST',
                                body: faceFormData,
                                signal: controller.signal
                            });

                            if (response.ok) {
                                backendAvailable = true;
                                setAiBackendOffline(false);
                                const data = await response.json();
                                if (data.frame_id && data.frame_id < lastProcessedFaceFrameRef.current) {
                                    return;
                                }
                                if (data.frame_id) lastProcessedFaceFrameRef.current = data.frame_id;

                                if (data.matches && data.matches.length > 0) {
                                    data.matches.forEach((face) => {
                                        const exactTime = formatExactTimestamp(new Date());
                                        const targetName = face.name || face.label || 'UNKNOWN';
                                        const isUnauthorized = targetName === 'UNAUTHORIZED PERSON' || targetName === 'UNKNOWN';

                                        setLastMatch(isUnauthorized ? 'INTRUDER DETECTED' : `TARGET: ${targetName}`);

                                        if (face.bbox) {
                                            newDetections.push({
                                                type: 'FACE',
                                                label: isUnauthorized ? `FACE: UNAUTHORIZED PERSON` : `FACE: ${targetName}`,
                                                bbox: face.bbox,
                                                confidence: face.confidence ? Math.round(face.confidence * 100) : (isUnauthorized ? 88 : 92),
                                                timestamp: currentTimestamp
                                            });
                                        }

                                        triggerAlertThrottled(isUnauthorized ? `INTRUDER_${cameraId}` : `FACE_${targetName}`, {
                                            id: `ALERT_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
                                            eventType: isUnauthorized ? 'UNAUTHORIZED PRESENCE' : 'TARGET MATCH',
                                            subject: targetName,
                                            details: isUnauthorized
                                                ? `Unenrolled person spotted in live camera feed on ${cameraId}`
                                                : `High-precision facial match identified on ${cameraId}`,
                                            lat: activeLoc.lat,
                                            lng: activeLoc.lng,
                                            address: activeLoc.address,
                                            cameraId: cameraId,
                                            cameraName: cameraName,
                                            timestamp: exactTime,
                                            confidence: face.confidence ? Math.round(face.confidence * 100) : (isUnauthorized ? 88 : 92),
                                            severity: isUnauthorized ? 'HIGH' : 'CRITICAL',
                                        });
                                    });
                                }
                            }
                        } catch (backendErr) {
                            backendAvailable = false;
                        } finally {
                            clearTimeout(timeoutId);
                        }

                        // Robust Browser AI Scanner Fallback: Runs whenever backend server is offline or unreachable
                        if (!backendAvailable) {
                            const mediaSource = imgRef.current;
                            if (mediaSource) {
                                const candidateCrops = [
                                    { x: 0.15, y: 0.05, w: 0.70, h: 0.85 },
                                    { x: 0.20, y: 0.10, w: 0.60, h: 0.75 },
                                    { x: 0.05, y: 0.05, w: 0.90, h: 0.90 },
                                    { x: 0.25, y: 0.15, w: 0.50, h: 0.60 }
                                ];

                                const targetList = typeof targets === 'string' ? JSON.parse(targets || '[]') : targets;
                                let bestEnrolledMatch = null;
                                let maxScore = -1.0;
                                let detectedFaceCrop = null;

                                for (const crop of candidateCrops) {
                                    const skinRatio = getCropSkinRatio(mediaSource, crop);
                                    if (skinRatio < 0.12) continue;

                                    const hasStructure = hasFaceStructure(mediaSource, crop);
                                    if (!hasStructure) continue;

                                    detectedFaceCrop = crop;

                                    const frameSig = getCanvasImageSignature(mediaSource, 24, 24, crop);
                                    if (!frameSig || !targetList || targetList.length === 0) break;

                                    for (const target of targetList) {
                                        if (target.imageSrc) {
                                            const targetSig = await getTargetImageSignature(target.imageSrc);
                                            if (targetSig && targetSig.length === frameSig.length) {
                                                let dot = 0;
                                                for (let i = 0; i < frameSig.length; i++) {
                                                    dot += frameSig[i] * targetSig[i];
                                                }
                                                if (dot > maxScore) {
                                                    maxScore = dot;
                                                    bestEnrolledMatch = { target, crop, score: dot };
                                                }
                                            }
                                        }
                                    }
                                }

                                if (detectedFaceCrop) {
                                    const exactTime = formatExactTimestamp(new Date());
                                    const crop = detectedFaceCrop;
                                    const bx1 = srcWidth * crop.x;
                                    const by1 = srcHeight * crop.y;
                                    const bx2 = srcWidth * (crop.x + crop.w);
                                    const by2 = srcHeight * (crop.y + crop.h);

                                    if (bestEnrolledMatch && bestEnrolledMatch.score >= 0.55) {
                                        const targetName = bestEnrolledMatch.target.name || 'WATCHLIST TARGET';
                                        setLastMatch(`TARGET: ${targetName}`);
                                        const matchConfidence = Math.min(99, Math.max(85, Math.round(bestEnrolledMatch.score * 100)));

                                        newDetections.push({
                                            type: 'FACE',
                                            label: `FACE: ${targetName}`,
                                            bbox: [bx1, by1, bx2, by2],
                                            confidence: matchConfidence,
                                            timestamp: currentTimestamp
                                        });

                                        triggerAlertThrottled(`FACE_${targetName}`, {
                                            id: `ALERT_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
                                            eventType: 'TARGET MATCH',
                                            subject: targetName,
                                            details: `Live Browser AI matched target portrait on ${cameraId}`,
                                            lat: activeLoc.lat,
                                            lng: activeLoc.lng,
                                            address: activeLoc.address,
                                            cameraId: cameraId,
                                            cameraName: cameraName,
                                            timestamp: exactTime,
                                            confidence: matchConfidence,
                                            severity: 'CRITICAL',
                                        });
                                    } else {
                                        const intruderLabel = 'UNAUTHORIZED PERSON';
                                        setLastMatch(`INTRUDER DETECTED`);

                                        newDetections.push({
                                            type: 'FACE',
                                            label: `FACE: ${intruderLabel}`,
                                            bbox: [bx1, by1, bx2, by2],
                                            confidence: 88,
                                            timestamp: currentTimestamp
                                        });

                                        triggerAlertThrottled(`INTRUDER_${cameraId}`, {
                                            id: `ALERT_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
                                            eventType: 'UNAUTHORIZED PRESENCE',
                                            subject: intruderLabel,
                                            details: `Unenrolled person spotted in live camera feed on ${cameraId}`,
                                            lat: activeLoc.lat,
                                            lng: activeLoc.lng,
                                            address: activeLoc.address,
                                            cameraId: cameraId,
                                            cameraName: cameraName,
                                            timestamp: exactTime,
                                            confidence: 88,
                                            severity: 'HIGH',
                                        });
                                    }
                                } else {
                                    setLastMatch(null);
                                }
                            }
                        }
                    } catch (err) {
                        console.warn("Scan face error:", err);
                    } finally {
                        isScanningFaceRef.current = false;
                    }
                };

                await Promise.all([scanFaceTask(), scanPlateTask()]);

                activeDetectionsRef.current = newDetections;
            } catch (e) {
                console.warn("Frame processing exception:", e);
            } finally {
                isProcessingFrame = false;
                setIsScanning(false);
            }
        }, 200);

        return () => clearInterval(scanInterval);
    }, [cameraId, cameraName, streamUrl]);

    // 3. High-Tech UI Canvas Overlay with Real-time Bounding Boxes & Perception Telemetry
    useEffect(() => {
        let animationId;

        function renderOverlay() {
            if (timestampRef.current) {
                timestampRef.current.textContent = formatExactTimestamp(new Date());
            }

            if (canvasRef.current) {
                const canvas = canvasRef.current;
                const ctx = canvas.getContext('2d');
                const img = imgRef.current;

                // Synchronize canvas resolution to actual client dimensions for crisp lines
                const displayW = canvas.clientWidth || 640;
                const displayH = canvas.clientHeight || 360;
                if (canvas.width !== displayW || canvas.height !== displayH) {
                    canvas.width = displayW;
                    canvas.height = displayH;
                }

                const width = canvas.width;
                const height = canvas.height;
                ctx.clearRect(0, 0, width, height);

                // Compute exact letterbox / pillarbox viewport for object-contain image
                const imgNaturalW = (img && img.naturalWidth) ? img.naturalWidth : 640;
                const imgNaturalH = (img && img.naturalHeight) ? img.naturalHeight : 480;
                const imgAspect = imgNaturalW / imgNaturalH;
                const containerAspect = width / height;

                let renderW = width;
                let renderH = height;
                let offsetX = 0;
                let offsetY = 0;

                if (containerAspect > imgAspect) {
                    // Pillarboxed: black bars on left & right
                    renderH = height;
                    renderW = height * imgAspect;
                    offsetX = (width - renderW) / 2;
                    offsetY = 0;
                } else {
                    // Letterboxed: black bars on top & bottom
                    renderW = width;
                    renderH = width / imgAspect;
                    offsetX = 0;
                    offsetY = (height - renderH) / 2;
                }

                const now = Date.now();
                const TTL_MS = 1400; // Bounded TTL: tracks expire after 1.4 seconds unseen
                const activeTracks = (telemetryTracksRef.current || []).filter(t => (now - t.receivedAt) < TTL_MS);

                // Draw Live Perception Tracks
                activeTracks.forEach((track) => {
                    if (!track.norm_bbox || track.norm_bbox.length !== 4) return;
                    const [nx1, ny1, nx2, ny2] = track.norm_bbox;
                    const x1 = offsetX + nx1 * renderW;
                    const y1 = offsetY + ny1 * renderH;
                    const bw = (nx2 - nx1) * renderW;
                    const bh = (ny2 - ny1) * renderH;
                    const x2 = x1 + bw;
                    const y2 = y1 + bh;

                    if (bw <= 0 || bh <= 0) return;

                    const hasFace = !!track.face;
                    const isFaceMatch = hasFace && track.face.matched;
                    const hasPlate = !!track.plate;
                    const isPlateMatch = hasPlate && track.plate.matched;

                    // Color palette according to perception entity & watchlist status
                    let strokeColor = '#00d2ff'; // Cyan for standard YOLO tracked objects
                    let bgColor = 'rgba(0, 25, 50, 0.90)';
                    let glowColor = 'rgba(0, 210, 255, 0.4)';

                    if (isFaceMatch) {
                        strokeColor = '#10b981'; // Emerald green for enrolled face match
                        bgColor = 'rgba(6, 78, 59, 0.92)';
                        glowColor = 'rgba(16, 185, 129, 0.5)';
                    } else if (hasFace) {
                        strokeColor = '#f59e0b'; // Amber orange for unknown face
                        bgColor = 'rgba(120, 53, 4, 0.92)';
                        glowColor = 'rgba(245, 158, 11, 0.4)';
                    } else if (isPlateMatch) {
                        strokeColor = '#f43f5e'; // Rose red for flagged watchlist plate
                        bgColor = 'rgba(159, 18, 57, 0.92)';
                        glowColor = 'rgba(244, 63, 94, 0.5)';
                    } else if (hasPlate) {
                        strokeColor = '#38bdf8'; // Sky blue for detected vehicle plate
                        bgColor = 'rgba(12, 74, 110, 0.92)';
                        glowColor = 'rgba(56, 189, 248, 0.4)';
                    }

                    ctx.save();

                    // Bounding Box
                    ctx.strokeStyle = strokeColor;
                    ctx.lineWidth = 2;
                    ctx.shadowColor = glowColor;
                    ctx.shadowBlur = 6;
                    ctx.strokeRect(x1, y1, bw, bh);
                    ctx.shadowBlur = 0;

                    // Corner Brackets
                    const cornerLen = Math.min(14, bw * 0.25, bh * 0.25);
                    ctx.lineWidth = 3;
                    ctx.beginPath();
                    ctx.moveTo(x1, y1 + cornerLen); ctx.lineTo(x1, y1); ctx.lineTo(x1 + cornerLen, y1);
                    ctx.moveTo(x2 - cornerLen, y1); ctx.lineTo(x2, y1); ctx.lineTo(x2, y1 + cornerLen);
                    ctx.moveTo(x1, y2 - cornerLen); ctx.lineTo(x1, y2); ctx.lineTo(x1 + cornerLen, y2);
                    ctx.moveTo(x2 - cornerLen, y2); ctx.lineTo(x2, y2); ctx.lineTo(x2, y2 - cornerLen);
                    ctx.stroke();

                    // Label Formatting
                    let primaryText = `${track.class_name ? track.class_name.toUpperCase() : 'OBJECT'} #${track.track_id} (${Math.round((track.confidence || 0.8) * 100)}%)`;
                    let secondaryText = null;

                    if (hasFace) {
                        if (isFaceMatch) {
                            primaryText = `${track.face.name}`;
                            secondaryText = `Face Match: ${track.face.similarity}`;
                        } else {
                            primaryText = `Unknown Face`;
                            secondaryText = `Face: ${track.face.similarity}`;
                        }
                    } else if (hasPlate) {
                        primaryText = `${track.plate.text}`;
                        secondaryText = isPlateMatch ? `Plate Match` : `Unknown Plate`;
                    }

                    ctx.font = 'bold 11px monospace';
                    const mainMetrics = ctx.measureText(primaryText);
                    let labelW = mainMetrics.width + 12;
                    let labelH = 18;

                    let secMetrics = null;
                    if (secondaryText) {
                        ctx.font = '9px monospace';
                        secMetrics = ctx.measureText(secondaryText);
                        labelW = Math.max(labelW, secMetrics.width + 12);
                        labelH = 32;
                    }

                    const labelX = Math.max(offsetX + 2, Math.min(x1, offsetX + renderW - labelW - 2));
                    let labelY = y1 - labelH - 2;
                    if (labelY < offsetY + 38) {
                        labelY = y1 + 4;
                    }

                    ctx.fillStyle = bgColor;
                    ctx.fillRect(labelX, labelY, labelW, labelH);
                    ctx.strokeStyle = strokeColor;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(labelX, labelY, labelW, labelH);

                    ctx.fillStyle = '#ffffff';
                    ctx.font = 'bold 11px monospace';
                    ctx.fillText(primaryText, labelX + 6, labelY + (secondaryText ? 13 : 13));

                    if (secondaryText) {
                        ctx.fillStyle = isFaceMatch ? '#a7f3d0' : (hasFace ? '#fde68a' : (isPlateMatch ? '#fecdd3' : '#bae6fd'));
                        ctx.font = '9px monospace';
                        ctx.fillText(secondaryText, labelX + 6, labelY + 26);
                    }

                    ctx.restore();
                });
            }
            animationId = requestAnimationFrame(renderOverlay);
        }

        renderOverlay();
        return () => cancelAnimationFrame(animationId);
    }, [cameraId]);

    return (
        <div
            onClick={() => {
                if (!isPrimary && onFocus) {
                    onFocus(cameraId);
                }
            }}
            className={`relative w-full h-full bg-slate-950 flex items-center justify-center overflow-hidden group ${
                isFullscreen ? 'rounded-none' : 'rounded-lg'
            } ${
                !isPrimary && onFocus
                    ? 'cursor-pointer hover:ring-2 hover:ring-cyan-500/60 transition-all'
                    : ''
            }`}
        >
            {/* Sleek, Compact Camera Info Badge in Top-Left Corner */}
            <div className="absolute top-2.5 left-2.5 z-20 flex items-center gap-2 pointer-events-none select-none">
                <div className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-slate-950/75 border border-slate-800/80 backdrop-blur-sm">
                    <span className={`w-1.5 h-1.5 rounded-full ${streamLoaded ? 'bg-emerald-400' : 'bg-amber-400 animate-ping'}`}></span>
                    <span className="text-[10px] font-mono font-semibold tracking-wider text-slate-200 uppercase">
                        {cameraId} · {cameraName}
                    </span>
                    <span className="text-slate-600 text-[9px]">·</span>
                    <span className={`text-[9px] font-mono font-bold px-1.5 py-0.5 rounded ${
                        isPrimary
                            ? 'text-cyan-300 bg-cyan-950/70 border border-cyan-800/60'
                            : 'text-slate-400 bg-slate-900/60 border border-slate-700/60'
                    }`}>
                        {isPrimary ? 'PRIMARY' : 'SECONDARY'}
                    </span>
                    <span className="text-slate-600 text-[9px]">·</span>
                    <span className="text-[9px] font-mono text-emerald-400 font-medium">
                        {streamLoaded ? 'LIVE' : 'SYNCING'}
                    </span>
                    {trackCount > 0 && (
                        <>
                            <span className="text-slate-600 text-[9px]">·</span>
                            <span className="text-[9px] font-mono text-cyan-300 font-semibold">
                                {trackCount} {trackCount === 1 ? 'TRACK' : 'TRACKS'}
                            </span>
                        </>
                    )}
                </div>
            </div>

            {/* Hover Focus Hint on Secondary Cameras */}
            {!isPrimary && onFocus && (
                <div className="absolute top-2.5 left-1/2 -translate-x-1/2 z-20 opacity-0 group-hover:opacity-100 transition-opacity duration-150 pointer-events-none select-none">
                    <span className="px-2.5 py-1 rounded-md bg-slate-950/90 border border-cyan-500/70 text-cyan-300 text-[10px] font-mono font-bold tracking-wider uppercase shadow-xl flex items-center gap-1.5 backdrop-blur-md">
                        <span className="animate-pulse text-cyan-400">⚡</span> CLICK TO FOCUS PRIMARY
                    </span>
                </div>
            )}

            {/* Hover Control Bar in Top-Right Corner */}
            {showControls && (
                <div className="absolute top-2.5 right-2.5 z-20 flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
                    {onLocationClick && (
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                onLocationClick(cameraId);
                            }}
                            title={`Focus ${cameraId} Node GPS`}
                            className="p-1 rounded bg-slate-950/80 border border-slate-800 text-slate-400 hover:text-cyan-400 hover:border-cyan-500/40 backdrop-blur-sm transition-colors cursor-pointer"
                        >
                            <MapPin className="w-3.5 h-3.5" />
                        </button>
                    )}
                    {onFullscreen && (
                        <button
                            onClick={(e) => {
                                e.stopPropagation();
                                onFullscreen(cameraId);
                            }}
                            title={isFullscreen ? "Exit Fullscreen (Esc)" : "Expand Fullscreen"}
                            className="p-1 rounded bg-slate-950/80 border border-slate-800 text-slate-400 hover:text-white hover:border-slate-600 backdrop-blur-sm transition-colors cursor-pointer"
                        >
                            {isFullscreen ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
                        </button>
                    )}
                </div>
            )}

            {/* Subtle Timestamp in Bottom-Right Corner */}
            <div className="absolute bottom-1.5 right-2 z-20 pointer-events-none select-none opacity-60">
                <span ref={timestampRef} className="text-[8px] font-mono text-slate-400">
                    {formatExactTimestamp(new Date())}
                </span>
            </div>

            {error && (
                <div className="absolute z-20 px-3 py-1.5 bg-rose-950/90 border border-rose-800 text-rose-300 text-[10px] font-mono rounded shadow-lg">
                    {error}
                </div>
            )}

            {aiBackendOffline && (
                <div className="absolute z-20 px-2 py-1 bg-rose-950/90 border border-rose-800 text-rose-300 text-[10px] font-mono rounded top-12 right-2.5">
                    ⚠️ AI Server Offline
                </div>
            )}

            <img
                ref={imgRef}
                src={currentStreamUrl}
                onLoad={handleImageLoad}
                onError={handleImageError}
                className="w-full h-full object-contain select-none pointer-events-none"
                alt={`${cameraId} stream`}
            />

            <canvas
                ref={canvasRef}
                className="absolute inset-0 w-full h-full object-contain pointer-events-none z-10"
            />
        </div>
    );
}
