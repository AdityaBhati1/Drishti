import React, { useEffect } from 'react';
import { X, Maximize2, Minimize2, Radio } from 'lucide-react';
import CameraFeed from './CameraFeed';

export default function CameraFullscreenModal({
    camera,
    onClose,
    enrolledTargets = [],
    enrolledPlates = [],
    onDetection = null,
    onLocationClick = null
}) {
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (e.key === 'Escape') {
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [onClose]);

    if (!camera) return null;

    return (
        <div className="fixed inset-0 z-50 bg-slate-950/95 backdrop-blur-md flex flex-col animate-fadeIn select-none">
            {/* Fullscreen Header Bar */}
            <header className="h-12 border-b border-slate-800/80 bg-slate-900/60 px-5 flex items-center justify-between flex-shrink-0">
                <div className="flex items-center gap-3">
                    <Radio className="w-4 h-4 text-emerald-400 animate-pulse" />
                    <span className="text-xs font-mono font-bold tracking-wider text-slate-200 uppercase">
                        FULLSCREEN INSPECTION · {camera.id} · {camera.name}
                    </span>
                    <span className="text-xs text-slate-500 font-mono">
                        ({camera.address || 'SURVEILLANCE NODE'})
                    </span>
                </div>

                <div className="flex items-center gap-3">
                    <span className="text-[10px] font-mono text-slate-400 bg-slate-800/60 px-2 py-0.5 rounded border border-slate-700/50">
                        PRESS ESC TO EXIT
                    </span>
                    <button
                        onClick={onClose}
                        title="Close Fullscreen (Esc)"
                        className="p-1.5 rounded-lg bg-slate-800/80 text-slate-400 hover:text-white hover:bg-slate-700 transition-all cursor-pointer"
                    >
                        <X className="w-4 h-4" />
                    </button>
                </div>
            </header>

            {/* Fullscreen Camera Body */}
            <div className="flex-1 flex items-center justify-center p-4 min-h-0 min-w-0 overflow-hidden">
                <div className="w-full h-full max-w-7xl max-h-[88vh] aspect-video bg-black rounded-lg overflow-hidden border border-slate-800/80 shadow-2xl relative">
                    <CameraFeed
                        cameraId={camera.id}
                        cameraName={camera.name}
                        streamUrl={camera.streamUrl}
                        latitude={camera.lat}
                        longitude={camera.lng}
                        enrolledTargets={enrolledTargets}
                        enrolledPlates={enrolledPlates}
                        onDetection={onDetection}
                        onLocationClick={onLocationClick}
                        onFullscreen={onClose}
                        isFullscreen={true}
                        showControls={true}
                    />
                </div>
            </div>
        </div>
    );
}
