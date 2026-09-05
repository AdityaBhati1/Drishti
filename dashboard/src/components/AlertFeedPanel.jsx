import React, { useState, useEffect } from 'react';
import { Bell, MapPin, Clock, Video, Check, AlertOctagon, CheckCheck } from 'lucide-react';

function SnapshotThumbnail({ path, alt = "Event Snapshot", className = "", resolveEvidenceUrl, getAuthHeaders }) {
    const [blobUrl, setBlobUrl] = useState(null);

    useEffect(() => {
        if (!path) {
            setBlobUrl(null);
            return;
        }
        const cleanUrl = resolveEvidenceUrl ? resolveEvidenceUrl(path) : path;
        let active = true;
        let createdUrl = null;

        const headers = getAuthHeaders ? getAuthHeaders() : {};
        fetch(cleanUrl, { headers })
            .then((res) => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.blob();
            })
            .then((blob) => {
                if (!active) return;
                createdUrl = URL.createObjectURL(blob);
                setBlobUrl(createdUrl);
            })
            .catch(() => {
                if (active) setBlobUrl(cleanUrl);
            });

        return () => {
            active = false;
            if (createdUrl) URL.revokeObjectURL(createdUrl);
        };
    }, [path, resolveEvidenceUrl, getAuthHeaders]);

    if (!blobUrl) {
        return (
            <div className="w-full h-20 bg-slate-950/80 rounded border border-slate-800 flex items-center justify-center text-slate-600 text-[10px] font-mono">
                Loading Evidence...
            </div>
        );
    }

    return (
        <img
            src={blobUrl}
            alt={alt}
            className={className}
            onClick={() => window.open(blobUrl, '_blank')}
            onError={(e) => { e.target.style.display = 'none'; }}
        />
    );
}

export default function AlertFeedPanel({
    alerts = [],
    onAcknowledgeAlert = null,
    onOpenClip = null,
    onCameraSelect = null,
    engineMode = 'FOG-CLUSTER',
    onClearLocalAlerts = null,
    resolveEvidenceUrl,
    getAuthHeaders
}) {
    const [filterSeverity, setFilterSeverity] = useState('ALL');

    const getSeverityStyles = (severity = '') => {
        const s = severity.toUpperCase();
        switch (s) {
            case 'CRITICAL':
                return {
                    border: 'border-rose-900/50 hover:border-rose-800/80',
                    dot: 'bg-rose-500',
                    badge: 'bg-rose-950/60 text-rose-300 border-rose-800/60'
                };
            case 'HIGH':
                return {
                    border: 'border-amber-900/50 hover:border-amber-800/80',
                    dot: 'bg-amber-500',
                    badge: 'bg-amber-950/60 text-amber-300 border-amber-800/60'
                };
            case 'MEDIUM':
                return {
                    border: 'border-blue-900/40 hover:border-blue-800/70',
                    dot: 'bg-blue-400',
                    badge: 'bg-blue-950/60 text-blue-300 border-blue-800/50'
                };
            default:
                return {
                    border: 'border-slate-800 hover:border-slate-700',
                    dot: 'bg-slate-400',
                    badge: 'bg-slate-800/60 text-slate-300 border-slate-700/50'
                };
        }
    };

    const filteredAlerts = filterSeverity === 'ALL'
        ? alerts
        : alerts.filter(a => (a.severity || '').toUpperCase() === filterSeverity);

    return (
        <aside className="w-full h-full flex flex-col bg-slate-900/40 border-l border-slate-800/80 overflow-hidden select-none">
            {/* Alert Column Header */}
            <div className="px-3.5 py-3 border-b border-slate-800/80 flex items-center justify-between flex-shrink-0 bg-slate-900/60">
                <div className="flex items-center gap-2">
                    <Bell className="w-4 h-4 text-slate-400" />
                    <span className="text-xs font-mono font-bold tracking-wider text-slate-200 uppercase">
                        INTERACTIVE ALERTS
                    </span>
                </div>

                <div className="flex items-center gap-1.5">
                    {engineMode === 'CLIENT-SIDE' && alerts.length > 0 && (
                        <button
                            onClick={onClearLocalAlerts}
                            className="text-[9px] text-rose-400 hover:text-rose-300 font-mono px-1.5 py-0.5 rounded border border-rose-900/50 bg-rose-950/30 cursor-pointer"
                        >
                            CLEAR
                        </button>
                    )}
                    <span className="text-[10px] font-mono font-semibold text-slate-400 px-2 py-0.5 rounded bg-slate-800/70 border border-slate-700/60">
                        {alerts.length}
                    </span>
                </div>
            </div>

            {/* Severity Filter Tabs */}
            <div className="px-3 py-1.5 border-b border-slate-800/50 bg-slate-950/40 flex items-center gap-1 text-[10px] font-mono overflow-x-auto flex-shrink-0">
                {['ALL', 'CRITICAL', 'HIGH', 'MEDIUM'].map((sev) => (
                    <button
                        key={sev}
                        onClick={() => setFilterSeverity(sev)}
                        className={`px-2 py-0.5 rounded transition-all cursor-pointer ${filterSeverity === sev
                            ? 'bg-slate-800 text-slate-100 font-bold border border-slate-700'
                            : 'text-slate-400 hover:text-slate-200'
                            }`}
                    >
                        {sev}
                    </button>
                ))}
            </div>

            {/* Scrollable Alert List */}
            <div className="flex-1 overflow-y-auto p-3 space-y-2.5 min-h-0">
                {filteredAlerts.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-48 border border-dashed border-slate-800/80 rounded-lg text-slate-500 text-xs gap-2 text-center p-4">
                        <AlertOctagon className="w-6 h-6 text-slate-600" />
                        <span>No alerts matching current filter.</span>
                    </div>
                ) : (
                    filteredAlerts.map((alert) => {
                        const sev = getSeverityStyles(alert.severity);
                        const isAck = alert.status === 'ACKNOWLEDGED';
                        const timeStr = alert.timestamp ? String(alert.timestamp).split('T').pop().substring(0, 8) : '';

                        return (
                            <article
                                key={alert.id || alert.event_id || Math.random()}
                                className={`p-2.5 rounded-lg border bg-slate-900/50 transition-all text-xs space-y-1.5 shadow-sm ${sev.border}`}
                            >
                                {/* Top Header: Event Type & Severity / ACK */}
                                <div className="flex items-center justify-between gap-2">
                                    <div className="flex items-center gap-1.5 font-mono font-bold text-[11px] text-slate-200 uppercase tracking-wide">
                                        <span className={`w-1.5 h-1.5 rounded-full ${sev.dot} flex-shrink-0`}></span>
                                        <span className="truncate">{alert.event_type || 'SECURITY ALERT'}</span>
                                    </div>

                                    <div className="flex items-center gap-1.5 flex-shrink-0">
                                        {isAck ? (
                                            <span className="text-[9px] font-mono text-emerald-400 bg-emerald-950/40 border border-emerald-800/40 px-1.5 py-0.5 rounded flex items-center gap-0.5">
                                                <Check className="w-2.5 h-2.5" /> ACK
                                            </span>
                                        ) : (
                                            onAcknowledgeAlert && alert.id && (
                                                <button
                                                    onClick={() => onAcknowledgeAlert(alert.id)}
                                                    className="text-[9px] font-mono font-semibold text-cyan-300 hover:text-white bg-slate-800/90 hover:bg-slate-700 border border-slate-700 px-1.5 py-0.5 rounded cursor-pointer transition-colors"
                                                >
                                                    ACK
                                                </button>
                                            )
                                        )}
                                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-mono font-bold uppercase border ${sev.badge}`}>
                                            {alert.severity || 'INFO'}
                                        </span>
                                    </div>
                                </div>

                                {/* Subject & Details */}
                                <div className="text-slate-200 text-xs font-semibold leading-snug">
                                    {alert.subject ? (
                                        <span className={alert.event_type === 'anpr_match' || alert.event_type === 'PLATE MATCH' ? 'text-amber-300 font-mono' : 'text-slate-100'}>
                                            {alert.subject}
                                        </span>
                                    ) : (
                                        <span className="text-slate-300">{alert.details || 'Event logged'}</span>
                                    )}
                                </div>

                                {alert.details && alert.subject && (
                                    <div className="text-[10px] text-slate-400 line-clamp-2 leading-relaxed">
                                        {alert.details}
                                    </div>
                                )}

                                {/* Metadata: Location & Timestamp */}
                                <div className="pt-1 border-t border-slate-800/50 flex items-center justify-between text-[9px] font-mono text-slate-400">
                                    <button
                                        onClick={() => onCameraSelect && onCameraSelect(alert.camera_id || alert.node_id)}
                                        className="flex items-center gap-1 text-slate-400 hover:text-cyan-400 transition-colors cursor-pointer truncate max-w-[150px]"
                                        title="Click to view camera"
                                    >
                                        <MapPin className="w-3 h-3 text-slate-400 flex-shrink-0" />
                                        <span className="truncate">{alert.camera_id || alert.node_id || 'CAM_01'}</span>
                                    </button>

                                    <div className="flex items-center gap-1 text-slate-400 flex-shrink-0">
                                        <Clock className="w-3 h-3 text-slate-400" />
                                        <span>{timeStr || alert.timestamp}</span>
                                    </div>
                                </div>

                                {/* Evidence Snapshot Preview */}
                                {alert.snapshot_path && (
                                    <div className="mt-1.5 rounded overflow-hidden border border-slate-800 bg-black max-h-24">
                                        <SnapshotThumbnail
                                            path={alert.snapshot_path}
                                            alt="Evidence Snapshot"
                                            className="w-full h-20 object-cover rounded hover:scale-105 transition-transform cursor-pointer"
                                            resolveEvidenceUrl={resolveEvidenceUrl}
                                            getAuthHeaders={getAuthHeaders}
                                        />
                                    </div>
                                )}

                                {/* Video Clip Link */}
                                {alert.clip_path && onOpenClip && (
                                    <div className="pt-0.5">
                                        <a
                                            href={resolveEvidenceUrl ? resolveEvidenceUrl(alert.clip_path) : alert.clip_path}
                                            onClick={(e) => onOpenClip(e, alert.clip_path)}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="inline-flex items-center gap-1 text-[10px] font-mono text-cyan-400 hover:text-cyan-300 hover:underline cursor-pointer"
                                        >
                                            <Video className="w-3 h-3 text-cyan-400" />
                                            <span>View Event Clip</span>
                                        </a>
                                    </div>
                                )}
                            </article>
                        );
                    })
                )}
            </div>
        </aside>
    );
}
