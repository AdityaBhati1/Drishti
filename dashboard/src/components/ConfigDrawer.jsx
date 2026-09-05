import React, { useEffect, useState } from 'react';
import {
    X, Sliders, AlertOctagon, Clock, Radio, RefreshCw, MapPin, CheckCircle,
    Camera, Edit3, Trash2, Power, AlertTriangle, Bell
} from 'lucide-react';
import { formatApiError } from '../App';

function isTimeInWindow(date, startStr, endStr) {
    if (!startStr || !endStr) return false;
    const [sh, sm] = startStr.split(':').map(Number);
    const [eh, em] = endStr.split(':').map(Number);

    const nowMinutes = date.getHours() * 60 + date.getMinutes();
    const startMinutes = sh * 60 + sm;
    const endMinutes = eh * 60 + em;

    if (startMinutes <= endMinutes) {
        return nowMinutes >= startMinutes && nowMinutes <= endMinutes;
    } else {
        // Crosses midnight (e.g. 22:00 to 06:00)
        return nowMinutes >= startMinutes || nowMinutes <= endMinutes;
    }
}

export default function ConfigDrawer({
    isOpen,
    onClose,
    cameras = [],
    onUpdateCamera,
    onToggleCamera,
    onDeleteCamera,
    cameraLoading = false,
    cameraActionStatus = null,
    alertCount = 0,
    onClearAlerts,
    clearAlertsLoading = false,
    restrictedRules = [],
    ruleForm,
    setRuleForm,
    ruleStatus,
    handleAddRule,
    handleToggleRule,
    handleDeleteRule,
    modalForm,
    setModalForm,
    modalLoading,
    modalError,
    handleModalSubmit,
    laptopLocation,
    locationSource,
    triggerGpsSync,
    gpsError,
    testLoading,
    testResult,
    handleTestConnection,
    setTestResult
}) {
    const [editingCameraId, setEditingCameraId] = useState(null);
    const [editCameraForm, setEditCameraForm] = useState({ name: '', rtsp_url: '', address: '', lat: '', lng: '' });
    const [deletingCameraId, setDeletingCameraId] = useState(null);
    const [isConfirmingClearAlerts, setIsConfirmingClearAlerts] = useState(false);

    useEffect(() => {
        const handleKeyDown = (e) => {
            if (e.key === 'Escape' && isOpen) {
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, onClose]);

    useEffect(() => {
        if (!isOpen) {
            setIsConfirmingClearAlerts(false);
            setDeletingCameraId(null);
            setEditingCameraId(null);
        }
    }, [isOpen]);

    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex justify-end animate-fadeIn select-none">
            {/* Backdrop overlay */}
            <div
                onClick={onClose}
                className="fixed inset-0 bg-slate-950/70 backdrop-blur-sm transition-opacity"
            />

            {/* Right-side Drawer Panel */}
            <aside className="relative z-10 w-full max-w-md bg-slate-900 border-l border-slate-800 shadow-2xl flex flex-col h-full overflow-hidden animate-slideLeft">
                {/* Header */}
                <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/90">
                    <div className="flex items-center gap-2.5">
                        <Sliders className="w-5 h-5 text-rose-400" />
                        <div>
                            <h2 className="text-sm font-bold tracking-wide text-slate-100 uppercase">
                                SYSTEM CONFIGURATION
                            </h2>
                            <p className="text-[10px] text-slate-400 font-mono">
                                Security Rules, Streams & Node Parameters
                            </p>
                        </div>
                    </div>

                    <button
                        onClick={onClose}
                        className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-colors cursor-pointer"
                        title="Close Drawer (Esc)"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {/* Body */}
                <div className="flex-1 overflow-y-auto p-5 space-y-6 min-h-0">
                    {/* Section 0: Camera Management (Edit, Enable/Disable, Delete) */}
                    <section className="space-y-4 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
                        <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                            <h3 className="text-xs font-mono font-bold text-cyan-300 uppercase tracking-wider flex items-center gap-2">
                                <Camera className="w-4 h-4 text-cyan-400" />
                                Camera Management
                            </h3>
                            <span className="text-[10px] font-mono text-cyan-400 font-bold">
                                {cameras.filter(c => c.status !== 'disabled' && c.status !== 'inactive').length} / {cameras.length} Active
                            </span>
                        </div>

                        {cameraActionStatus && (
                            <div className={`p-2 rounded text-[11px] font-mono border ${
                                cameraActionStatus.error
                                    ? 'bg-rose-950/30 border-rose-800/60 text-rose-300'
                                    : 'bg-emerald-950/30 border-emerald-800/60 text-emerald-300'
                            }`}>
                                {cameraActionStatus.message}
                            </div>
                        )}

                        {/* Camera List */}
                        <div className="space-y-2.5">
                            {cameras.map((cam) => {
                                const isActive = cam.status !== 'disabled' && cam.status !== 'inactive';
                                const isEditing = editingCameraId === cam.id;
                                const isDeleting = deletingCameraId === cam.id;

                                return (
                                    <div
                                        key={cam.id}
                                        className={`p-3 rounded-xl border transition-all ${
                                            isActive
                                                ? 'bg-slate-900/90 border-slate-800/90 shadow-md'
                                                : 'bg-slate-950/40 border-slate-800/40 opacity-70'
                                        }`}
                                    >
                                        {/* Header Row */}
                                        <div className="flex items-center justify-between gap-2">
                                            <div className="flex items-center gap-2 min-w-0">
                                                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${isActive ? 'bg-emerald-400' : 'bg-slate-600'}`}></span>
                                                <div className="truncate">
                                                    <span className="text-xs font-mono font-bold text-slate-200 uppercase tracking-wide">
                                                        {cam.id}
                                                    </span>
                                                    <span className="text-[11px] text-slate-400 ml-1.5 truncate">
                                                        {cam.name || 'Unnamed Node'}
                                                    </span>
                                                </div>
                                            </div>

                                            {/* Action Buttons */}
                                            <div className="flex items-center gap-1.5 flex-shrink-0">
                                                {/* Toggle Enable/Disable Button */}
                                                <button
                                                    type="button"
                                                    onClick={() => onToggleCamera && onToggleCamera(cam.id)}
                                                    disabled={cameraLoading}
                                                    title={isActive ? "Disable Camera" : "Enable Camera"}
                                                    className={`px-2 py-0.5 rounded text-[9px] font-mono font-bold border transition-colors cursor-pointer flex items-center gap-1 ${
                                                        isActive
                                                            ? 'bg-emerald-950/80 border-emerald-700/80 text-emerald-300 hover:bg-emerald-900'
                                                            : 'bg-slate-800/80 border-slate-700 text-slate-400 hover:bg-slate-700'
                                                    }`}
                                                >
                                                    <Power className="w-2.5 h-2.5" />
                                                    {isActive ? 'ACTIVE' : 'DISABLED'}
                                                </button>

                                                {/* Edit Button */}
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        if (isEditing) {
                                                            setEditingCameraId(null);
                                                        } else {
                                                            setDeletingCameraId(null);
                                                            setEditingCameraId(cam.id);
                                                            setEditCameraForm({
                                                                name: cam.name || '',
                                                                rtsp_url: cam.rtsp_url || cam.streamUrl || cam.source?.url || '',
                                                                address: cam.location?.address || cam.address || '',
                                                                lat: cam.location?.lat ?? cam.lat ?? '',
                                                                lng: cam.location?.lng ?? cam.lng ?? ''
                                                            });
                                                        }
                                                    }}
                                                    disabled={cameraLoading}
                                                    title="Edit Camera"
                                                    className={`p-1 rounded text-slate-400 hover:text-cyan-300 hover:bg-slate-800 transition-colors cursor-pointer ${
                                                        isEditing ? 'text-cyan-300 bg-slate-800' : ''
                                                    }`}
                                                >
                                                    <Edit3 className="w-3.5 h-3.5" />
                                                </button>

                                                {/* Delete Button */}
                                                <button
                                                    type="button"
                                                    onClick={() => {
                                                        setEditingCameraId(null);
                                                        setDeletingCameraId(isDeleting ? null : cam.id);
                                                    }}
                                                    disabled={cameraLoading}
                                                    title="Delete Camera (Preserves historical alerts & recordings)"
                                                    className={`p-1 rounded text-slate-500 hover:text-rose-400 hover:bg-rose-950/40 transition-colors cursor-pointer ${
                                                        isDeleting ? 'text-rose-400 bg-rose-950/60' : ''
                                                    }`}
                                                >
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                </button>
                                            </div>
                                        </div>

                                        {/* Metadata summary (when not editing) */}
                                        {!isEditing && !isDeleting && (
                                            <div className="mt-1.5 pt-1.5 border-t border-slate-800/60 flex flex-col gap-0.5 text-[10px] font-mono text-slate-400">
                                                <div className="truncate">
                                                    <span className="text-slate-500">Stream:</span>{' '}
                                                    <span className="text-slate-300">
                                                        {cam.rtsp_url || cam.streamUrl || cam.source?.url || '(Local Ingestion)'}
                                                    </span>
                                                </div>
                                                {(cam.location?.address || cam.address) && (
                                                    <div className="truncate">
                                                        <span className="text-slate-500">Location:</span>{' '}
                                                        <span className="text-slate-300">{cam.location?.address || cam.address}</span>
                                                    </div>
                                                )}
                                            </div>
                                        )}

                                        {/* Inline Edit Form */}
                                        {isEditing && (
                                            <form
                                                onSubmit={(e) => {
                                                    e.preventDefault();
                                                    if (onUpdateCamera) {
                                                        const latVal = editCameraForm.lat !== '' && !isNaN(Number(editCameraForm.lat)) ? Number(editCameraForm.lat) : undefined;
                                                        const lngVal = editCameraForm.lng !== '' && !isNaN(Number(editCameraForm.lng)) ? Number(editCameraForm.lng) : undefined;
                                                        const payload = {
                                                            name: editCameraForm.name,
                                                            rtsp_url: editCameraForm.rtsp_url,
                                                            address: editCameraForm.address,
                                                            lat: latVal,
                                                            lng: lngVal,
                                                            location: {
                                                                address: editCameraForm.address,
                                                                lat: latVal,
                                                                lng: lngVal,
                                                            }
                                                        };
                                                        onUpdateCamera(cam.id, payload);
                                                    }
                                                    setEditingCameraId(null);
                                                }}
                                                className="mt-2.5 pt-2.5 border-t border-slate-800 space-y-2 text-xs"
                                            >
                                                <div>
                                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Camera Name</label>
                                                    <input
                                                        type="text"
                                                        value={editCameraForm.name}
                                                        onChange={(e) => setEditCameraForm(prev => ({ ...prev, name: e.target.value }))}
                                                        className="w-full bg-slate-950 border border-slate-800 rounded px-2.5 py-1 text-slate-200 text-xs font-mono focus:border-cyan-500 outline-none"
                                                    />
                                                </div>

                                                <div>
                                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Stream URL / Endpoint</label>
                                                    <input
                                                        type="text"
                                                        value={editCameraForm.rtsp_url}
                                                        onChange={(e) => setEditCameraForm(prev => ({ ...prev, rtsp_url: e.target.value }))}
                                                        placeholder="rtsp://... or http://..."
                                                        className="w-full bg-slate-950 border border-slate-800 rounded px-2.5 py-1 text-slate-200 text-xs font-mono focus:border-cyan-500 outline-none"
                                                    />
                                                </div>

                                                <div>
                                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Location / Address</label>
                                                    <input
                                                        type="text"
                                                        value={editCameraForm.address}
                                                        onChange={(e) => setEditCameraForm(prev => ({ ...prev, address: e.target.value }))}
                                                        placeholder="e.g. Northern Perimeter Gate Alpha"
                                                        className="w-full bg-slate-950 border border-slate-800 rounded px-2.5 py-1 text-slate-200 text-xs font-mono focus:border-cyan-500 outline-none"
                                                    />
                                                </div>

                                                <div className="grid grid-cols-2 gap-2">
                                                    <div>
                                                        <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Latitude</label>
                                                        <input
                                                            type="number"
                                                            step="any"
                                                            value={editCameraForm.lat}
                                                            onChange={(e) => setEditCameraForm(prev => ({ ...prev, lat: parseFloat(e.target.value) || e.target.value }))}
                                                            className="w-full bg-slate-950 border border-slate-800 rounded px-2.5 py-1 text-slate-200 text-xs font-mono focus:border-cyan-500 outline-none"
                                                        />
                                                    </div>
                                                    <div>
                                                        <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-0.5">Longitude</label>
                                                        <input
                                                            type="number"
                                                            step="any"
                                                            value={editCameraForm.lng}
                                                            onChange={(e) => setEditCameraForm(prev => ({ ...prev, lng: parseFloat(e.target.value) || e.target.value }))}
                                                            className="w-full bg-slate-950 border border-slate-800 rounded px-2.5 py-1 text-slate-200 text-xs font-mono focus:border-cyan-500 outline-none"
                                                        />
                                                    </div>
                                                </div>

                                                <div className="flex justify-end gap-2 pt-1">
                                                    <button
                                                        type="button"
                                                        onClick={() => setEditingCameraId(null)}
                                                        className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-[11px] font-mono cursor-pointer transition-colors"
                                                    >
                                                        Cancel
                                                    </button>
                                                    <button
                                                        type="submit"
                                                        disabled={cameraLoading}
                                                        className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 text-white rounded text-[11px] font-mono font-bold cursor-pointer shadow transition-colors flex items-center gap-1"
                                                    >
                                                        {cameraLoading && <RefreshCw className="w-3 h-3 animate-spin" />}
                                                        Save Changes
                                                    </button>
                                                </div>
                                            </form>
                                        )}

                                        {/* Delete Confirmation Box */}
                                        {isDeleting && (
                                            <div className="mt-2.5 pt-2.5 border-t border-rose-900/60 p-2.5 bg-rose-950/30 rounded-lg space-y-2">
                                                <div className="flex items-start gap-2">
                                                    <AlertTriangle className="w-4 h-4 text-rose-400 flex-shrink-0 mt-0.5" />
                                                    <div className="text-[11px] text-rose-200">
                                                        <p className="font-bold">Delete Camera {cam.id}?</p>
                                                        <p className="text-[10px] text-rose-300/80 mt-0.5">
                                                            Historical alerts, snapshots, clips, and database records will be <strong>preserved</strong>.
                                                        </p>
                                                    </div>
                                                </div>

                                                <div className="flex justify-end gap-2 pt-1">
                                                    <button
                                                        type="button"
                                                        onClick={() => setDeletingCameraId(null)}
                                                        className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-[11px] font-mono cursor-pointer transition-colors"
                                                    >
                                                        Cancel
                                                    </button>
                                                    <button
                                                        type="button"
                                                        onClick={() => {
                                                            if (onDeleteCamera) {
                                                                onDeleteCamera(cam.id);
                                                            }
                                                            setDeletingCameraId(null);
                                                        }}
                                                        disabled={cameraLoading}
                                                        className="px-3 py-1 bg-rose-700 hover:bg-rose-600 text-white rounded text-[11px] font-mono font-bold cursor-pointer shadow transition-colors flex items-center gap-1"
                                                    >
                                                        {cameraLoading && <RefreshCw className="w-3 h-3 animate-spin" />}
                                                        Confirm Delete
                                                    </button>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                );
                            })}
                            {cameras.length === 0 && (
                                <div className="p-4 text-center text-xs font-mono text-slate-500 bg-slate-950/40 rounded-lg border border-slate-900">
                                    No cameras configured. Add an IP stream below.
                                </div>
                            )}
                        </div>

                        {/* Alert History Maintenance / Clear Alerts */}
                        <div className="mt-4 pt-3 border-t border-slate-800/80">
                            <div className="flex items-center justify-between">
                                <div>
                                    <div className="text-[11px] font-mono font-bold text-slate-300 flex items-center gap-1.5">
                                        <Bell className="w-3.5 h-3.5 text-amber-400" />
                                        Alert History Maintenance
                                    </div>
                                    <p className="text-[10px] text-slate-400 font-mono">
                                        Purge alert events from database ({alertCount ?? 0} active alerts)
                                    </p>
                                </div>

                                {!isConfirmingClearAlerts ? (
                                    <button
                                        type="button"
                                        onClick={() => setIsConfirmingClearAlerts(true)}
                                        disabled={clearAlertsLoading}
                                        className="px-2.5 py-1 rounded bg-slate-800/90 hover:bg-rose-950/80 border border-slate-700 hover:border-rose-800 text-slate-300 hover:text-rose-300 text-[10px] font-mono font-bold transition-colors cursor-pointer flex items-center gap-1"
                                        title="Clear Alert History"
                                    >
                                        <Trash2 className="w-3 h-3 text-rose-400" />
                                        Clear Alerts
                                    </button>
                                ) : null}
                            </div>

                            {/* Confirmation Prompt */}
                            {isConfirmingClearAlerts && (
                                <div className="mt-2.5 p-3 rounded-lg bg-rose-950/40 border border-rose-800/70 space-y-2 animate-fadeIn">
                                    <div className="flex items-start gap-2 text-rose-300 text-[11px] font-mono">
                                        <AlertTriangle className="w-4 h-4 text-rose-400 flex-shrink-0 mt-0.5" />
                                        <div>
                                            <span className="font-bold">Permanent Removal:</span> This will delete all alert event records from the database.
                                            <div className="text-[10px] text-rose-300/80 mt-1 space-y-0.5">
                                                <div>✓ Camera configurations preserved</div>
                                                <div>✓ Watchlists & registered faces preserved</div>
                                                <div>✓ Historical snapshot & video evidence preserved on disk</div>
                                            </div>
                                        </div>
                                    </div>
                                    <div className="flex justify-end gap-2 pt-1 border-t border-rose-900/50">
                                        <button
                                            type="button"
                                            onClick={() => setIsConfirmingClearAlerts(false)}
                                            disabled={clearAlertsLoading}
                                            className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded text-[10px] font-mono cursor-pointer transition-colors"
                                        >
                                            Cancel
                                        </button>
                                        <button
                                            type="button"
                                            onClick={async () => {
                                                if (onClearAlerts) {
                                                    await onClearAlerts();
                                                }
                                                setIsConfirmingClearAlerts(false);
                                            }}
                                            disabled={clearAlertsLoading}
                                            className="px-3 py-1 bg-rose-700 hover:bg-rose-600 text-white rounded text-[10px] font-mono font-bold cursor-pointer shadow transition-colors flex items-center gap-1"
                                        >
                                            {clearAlertsLoading && <RefreshCw className="w-3 h-3 animate-spin" />}
                                            Confirm Clear All Alerts
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    </section>

                    {/* Section 1: Restricted Zone & Off-Hours Security Rules */}
                    <section className="space-y-4 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
                        <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                            <h3 className="text-xs font-mono font-bold text-rose-300 uppercase tracking-wider flex items-center gap-2">
                                <AlertOctagon className="w-4 h-4 text-rose-400" />
                                Restricted Zones & Off-Hours Rules
                            </h3>
                            <span className="text-[10px] font-mono text-rose-400 font-bold">
                                {restrictedRules.filter(r => r.enabled).length} Active
                            </span>
                        </div>

                        <form onSubmit={handleAddRule} className="space-y-3">
                            <div>
                                <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Rule Designation</label>
                                <input
                                    type="text"
                                    value={ruleForm.name}
                                    onChange={(e) => setRuleForm(prev => ({ ...prev, name: e.target.value }))}
                                    placeholder="e.g. Server Room Night Lock"
                                    className="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:border-rose-500 outline-none placeholder:text-slate-600"
                                />
                            </div>

                            <div className="grid grid-cols-2 gap-2">
                                <div>
                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Target Camera</label>
                                    <select
                                        value={ruleForm.cameraId}
                                        onChange={(e) => setRuleForm(prev => ({ ...prev, cameraId: e.target.value }))}
                                        className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-200 focus:border-rose-500 outline-none"
                                    >
                                        <option value="CAM_01">CAM_01 (LOCAL_NODE)</option>
                                        <option value="CAM_02">CAM_02 (UPTOWN_NODE)</option>
                                        <option value="CAM_03">CAM_03 (PERIMETER)</option>
                                        <option value="ALL_CAMERAS">ALL SURVEILLANCE NODES</option>
                                    </select>
                                </div>

                                <div>
                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Entity Filter</label>
                                    <select
                                        value={ruleForm.constraint}
                                        onChange={(e) => setRuleForm(prev => ({ ...prev, constraint: e.target.value }))}
                                        className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-200 focus:border-rose-500 outline-none"
                                    >
                                        <option value="PERSON_OR_CAR">ANY PERSON OR CAR</option>
                                        <option value="PERSON_ONLY">PERSON ONLY</option>
                                        <option value="VEHICLE_ONLY">VEHICLE ONLY</option>
                                    </select>
                                </div>
                            </div>

                            <div className="grid grid-cols-2 gap-2">
                                <div>
                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Start Time</label>
                                    <input
                                        type="time"
                                        value={ruleForm.startTime}
                                        onChange={(e) => setRuleForm(prev => ({ ...prev, startTime: e.target.value }))}
                                        className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2 py-1 text-xs text-rose-300 font-mono focus:border-rose-500 outline-none"
                                    />
                                </div>
                                <div>
                                    <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">End Time</label>
                                    <input
                                        type="time"
                                        value={ruleForm.endTime}
                                        onChange={(e) => setRuleForm(prev => ({ ...prev, endTime: e.target.value }))}
                                        className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2 py-1 text-xs text-rose-300 font-mono focus:border-rose-500 outline-none"
                                    />
                                </div>
                            </div>

                            <button
                                type="submit"
                                className="w-full py-2 bg-rose-800 hover:bg-rose-700 text-white rounded-lg text-xs font-semibold transition-all flex items-center justify-center gap-1.5 cursor-pointer mt-1"
                            >
                                <Clock className="w-3.5 h-3.5" />
                                Add Security Rule
                            </button>
                        </form>

                        {ruleStatus.success && (
                            <div className="text-[11px] p-2 bg-emerald-950/20 border border-emerald-800/30 text-emerald-400 rounded-lg">
                                {ruleStatus.success}
                            </div>
                        )}
                        {ruleStatus.error && (
                            <div className="text-[11px] p-2 bg-rose-950/20 border border-rose-800/30 text-rose-400 rounded-lg">
                                {ruleStatus.error}
                            </div>
                        )}

                        {/* Active Rules List */}
                        <div className="space-y-2 pt-2 border-t border-slate-800">
                            {restrictedRules.map((rule) => {
                                const isCurrentlyInWindow = isTimeInWindow(new Date(), rule.startTime, rule.endTime);
                                return (
                                    <div
                                        key={rule.id}
                                        className={`p-2.5 rounded-lg border text-xs font-mono flex flex-col gap-1 transition-all ${rule.enabled
                                            ? (isCurrentlyInWindow ? 'bg-rose-950/30 border-rose-800/80 text-rose-200' : 'bg-slate-900 border-slate-800 text-slate-300')
                                            : 'bg-slate-950/40 border-slate-900 text-slate-500'
                                            }`}
                                    >
                                        <div className="flex justify-between items-center">
                                            <span className="font-bold font-sans text-slate-200 flex items-center gap-1.5">
                                                <span className={`w-1.5 h-1.5 rounded-full ${rule.enabled ? (isCurrentlyInWindow ? 'bg-rose-500 animate-ping' : 'bg-amber-400') : 'bg-slate-600'}`}></span>
                                                {rule.name}
                                            </span>
                                            <div className="flex items-center gap-1.5">
                                                <button
                                                    onClick={() => handleToggleRule(rule.id)}
                                                    className={`px-1.5 py-0.5 rounded text-[9px] font-bold cursor-pointer ${rule.enabled ? 'bg-emerald-950 border border-emerald-800 text-emerald-400' : 'bg-slate-800 text-slate-500'
                                                        }`}
                                                >
                                                    {rule.enabled ? 'ENABLED' : 'PAUSED'}
                                                </button>
                                                <button
                                                    onClick={() => handleDeleteRule(rule.id)}
                                                    className="text-slate-500 hover:text-rose-400 text-xs px-1 cursor-pointer"
                                                    title="Delete Rule"
                                                >
                                                    ✕
                                                </button>
                                            </div>
                                        </div>
                                        <div className="flex justify-between text-[10px] text-slate-400">
                                            <span>📍 {rule.cameraId}</span>
                                            <span className="text-amber-300 font-bold">⏰ {rule.startTime} - {rule.endTime}</span>
                                            <span>{rule.constraint}</span>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </section>

                    {/* Section 2: Connect IP CCTV Camera Stream */}
                    <section className="space-y-4 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
                        <h3 className="text-xs font-mono font-bold text-blue-300 uppercase tracking-wider flex items-center gap-2 border-b border-slate-800 pb-2">
                            <Radio className="w-4 h-4 text-blue-400" />
                            Add IP Camera / Stream
                        </h3>

                        {modalError && (
                            <div className="text-[11px] p-2 bg-rose-950/30 border border-rose-800/40 text-rose-400 rounded-lg font-mono">
                                {typeof modalError === 'string' ? modalError : formatApiError(modalError)}
                            </div>
                        )}

                        {/* Camera Source Selector: ONVIF vs Direct Stream URL */}
                        <div className="space-y-1.5">
                            <label className="text-[10px] font-semibold text-slate-400 uppercase block">Camera Source</label>
                            <div className="flex bg-slate-900 p-1 rounded-lg border border-slate-800 text-[11px] font-mono">
                                <button
                                    type="button"
                                    onClick={() => {
                                        setModalForm(prev => ({ ...prev, sourceType: 'direct' }));
                                        if (setTestResult) setTestResult(null);
                                    }}
                                    className={`flex-1 py-1 px-2 rounded font-semibold transition-all cursor-pointer text-center ${
                                        (modalForm.sourceType || 'direct') === 'direct'
                                            ? 'bg-blue-600 text-white shadow-sm'
                                            : 'text-slate-400 hover:text-slate-200'
                                    }`}
                                >
                                    Direct Stream URL
                                </button>
                                <button
                                    type="button"
                                    onClick={() => {
                                        setModalForm(prev => ({ ...prev, sourceType: 'onvif' }));
                                        if (setTestResult) setTestResult(null);
                                    }}
                                    className={`flex-1 py-1 px-2 rounded font-semibold transition-all cursor-pointer text-center ${
                                        modalForm.sourceType === 'onvif'
                                            ? 'bg-blue-600 text-white shadow-sm'
                                            : 'text-slate-400 hover:text-slate-200'
                                    }`}
                                >
                                    ONVIF Discovery
                                </button>
                            </div>
                        </div>

                        <form onSubmit={handleModalSubmit} className="space-y-3">
                            {/* Target Camera Slot Selector */}
                            <div className="space-y-1">
                                <label className="text-[10px] font-semibold text-slate-400 uppercase block">Target Camera Slot</label>
                                <div className="flex bg-slate-900 p-1 rounded-lg border border-slate-800 text-[11px] font-mono">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setModalForm(prev => ({
                                                ...prev,
                                                targetSlot: 'Camera Slot 2',
                                                cameraId: prev.cameraId === 'CAM_03' ? 'CAM_02' : (prev.cameraId || 'CAM_02')
                                            }));
                                        }}
                                        className={`flex-1 py-1 px-2 rounded font-semibold transition-all cursor-pointer text-center ${
                                            (modalForm.targetSlot || 'Camera Slot 2') === 'Camera Slot 2'
                                                ? 'bg-blue-600 text-white shadow-sm'
                                                : 'text-slate-400 hover:text-slate-200'
                                        }`}
                                    >
                                        Slot 2 (CAM_02)
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setModalForm(prev => ({
                                                ...prev,
                                                targetSlot: 'Camera Slot 3',
                                                cameraId: prev.cameraId === 'CAM_02' ? 'CAM_03' : (prev.cameraId || 'CAM_03')
                                            }));
                                        }}
                                        className={`flex-1 py-1 px-2 rounded font-semibold transition-all cursor-pointer text-center ${
                                            modalForm.targetSlot === 'Camera Slot 3'
                                                ? 'bg-blue-600 text-white shadow-sm'
                                                : 'text-slate-400 hover:text-slate-200'
                                        }`}
                                    >
                                        Slot 3 (CAM_03)
                                    </button>
                                </div>
                            </div>

                            <div>
                                <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Camera ID</label>
                                <input
                                    type="text"
                                    required
                                    placeholder="CAM_02"
                                    value={modalForm.cameraId}
                                    onChange={(e) => {
                                        const val = e.target.value;
                                        const inferred = val.toUpperCase().includes('03') ? 'Camera Slot 3' : (val.toUpperCase().includes('02') ? 'Camera Slot 2' : modalForm.targetSlot);
                                        setModalForm(prev => ({ ...prev, cameraId: val, targetSlot: inferred }));
                                    }}
                                    className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                />
                            </div>

                            {(modalForm.sourceType || 'direct') === 'direct' ? (
                                <>
                                    <div>
                                        <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Stream URL</label>
                                        <input
                                            type="text"
                                            required
                                            placeholder="rtsp://192.168.1.50:554/stream or http://..."
                                            value={modalForm.streamUrl || ''}
                                            onChange={(e) => setModalForm(prev => ({ ...prev, streamUrl: e.target.value }))}
                                            className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                        />
                                    </div>
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Username (Optional)</label>
                                            <input
                                                type="text"
                                                placeholder="admin"
                                                value={modalForm.username || ''}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, username: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Password (Optional)</label>
                                            <input
                                                type="password"
                                                placeholder="••••••••"
                                                value={modalForm.password || ''}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, password: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                    </div>
                                </>
                            ) : (
                                <>
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">IP Address / Host</label>
                                            <input
                                                type="text"
                                                required
                                                placeholder="192.168.1.100"
                                                value={modalForm.ipAddress}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, ipAddress: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">RTSP Port</label>
                                            <input
                                                type="number"
                                                placeholder="554"
                                                value={modalForm.port}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, port: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                    </div>
                                    <div>
                                        <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">RTSP Path</label>
                                        <input
                                            type="text"
                                            placeholder="/live"
                                            value={modalForm.rtspPath}
                                            onChange={(e) => setModalForm(prev => ({ ...prev, rtspPath: e.target.value }))}
                                            className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                        />
                                    </div>
                                    <div className="grid grid-cols-2 gap-2">
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Username (Optional)</label>
                                            <input
                                                type="text"
                                                placeholder="admin"
                                                value={modalForm.username || ''}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, username: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                        <div>
                                            <label className="text-[10px] font-semibold text-slate-400 uppercase block mb-1">Password (Optional)</label>
                                            <input
                                                type="password"
                                                placeholder="••••••••"
                                                value={modalForm.password || ''}
                                                onChange={(e) => setModalForm(prev => ({ ...prev, password: e.target.value }))}
                                                className="w-full bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-blue-500 font-mono"
                                            />
                                        </div>
                                    </div>
                                </>
                            )}

                            {/* Test Connection Result Feedback */}
                            {testResult && (
                                <div className={`p-2.5 rounded-lg border text-[11px] font-mono transition-all ${
                                    testResult.connected
                                        ? 'bg-emerald-950/40 border-emerald-800/60 text-emerald-300'
                                        : 'bg-rose-950/40 border-rose-800/60 text-rose-300'
                                }`}>
                                    {testResult.connected ? (
                                        <div className="space-y-1">
                                            <div className="flex items-center gap-1.5 font-bold text-emerald-400">
                                                <CheckCircle className="w-3.5 h-3.5" />
                                                <span>Connected</span>
                                            </div>
                                            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-emerald-200/80">
                                                <span>Protocol: {testResult.protocol || 'RTSP'}</span>
                                                {testResult.resolution && <span>Resolution: {testResult.resolution}</span>}
                                                {testResult.fps && <span>FPS: {testResult.fps}</span>}
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="flex items-center gap-1.5">
                                            <AlertOctagon className="w-3.5 h-3.5 text-rose-400 flex-shrink-0" />
                                            <span>
                                                {typeof testResult.error === 'string'
                                                    ? testResult.error
                                                    : (formatApiError(testResult.error) || 'Connection refused or stream unavailable')}
                                            </span>
                                        </div>
                                    )}
                                </div>
                            )}

                            {/* Action Buttons: Test Connection & Connect Stream */}
                            <div className="flex gap-2 pt-1">
                                <button
                                    type="button"
                                    onClick={handleTestConnection}
                                    disabled={testLoading || modalLoading}
                                    className="flex-1 py-2 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-xs font-bold text-slate-200 rounded-lg flex items-center justify-center gap-1.5 cursor-pointer border border-slate-700 transition-colors"
                                >
                                    {testLoading && <RefreshCw className="w-3.5 h-3.5 animate-spin" />}
                                    Test Connection
                                </button>
                                <button
                                    type="submit"
                                    disabled={modalLoading || testLoading}
                                    className="flex-1 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-xs font-bold text-white rounded-lg flex items-center justify-center gap-1.5 cursor-pointer shadow-md transition-colors"
                                >
                                    {modalLoading && <RefreshCw className="w-3.5 h-3.5 animate-spin" />}
                                    Connect Stream to Slot
                                </button>
                            </div>
                        </form>
                    </section>

                    {/* Section 3: Tactical Node Location */}
                    <section className="space-y-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80 font-mono text-xs text-slate-400">
                        <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                            <span className="font-bold text-slate-300 text-[11px] uppercase flex items-center gap-1.5">
                                <MapPin className="w-3.5 h-3.5 text-cyan-400" />
                                Tactical Node GPS
                            </span>
                            <span className={`w-2 h-2 rounded-full ${locationSource === 'GPS LOCK' ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`}></span>
                        </div>

                        <div className="space-y-1 text-slate-300">
                            <div className="flex justify-between">
                                <span>Lock Source:</span>
                                <span className="font-bold text-cyan-400">{locationSource}</span>
                            </div>
                            <div className="flex justify-between">
                                <span>Coordinates:</span>
                                <span className="text-white">
                                    {laptopLocation ? `${laptopLocation[0].toFixed(5)}, ${laptopLocation[1].toFixed(5)}` : 'N/A'}
                                </span>
                            </div>
                        </div>

                        {gpsError && (
                            <div className="text-rose-400 text-[10px] pt-1">
                                ⚠️ {gpsError}
                            </div>
                        )}

                        <button
                            onClick={triggerGpsSync}
                            className="w-full bg-slate-800 hover:bg-slate-700 text-slate-200 py-1.5 rounded text-[10px] uppercase font-bold tracking-wider transition-colors flex items-center justify-center gap-1.5 cursor-pointer mt-1"
                        >
                            <RefreshCw className="w-3 h-3" />
                            Resync GPS Coordinates
                        </button>
                    </section>
                </div>
            </aside>
        </div>
    );
}
