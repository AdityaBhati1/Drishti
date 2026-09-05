import React, { useEffect } from 'react';
import { X, UserPlus, Car, Camera, CheckCircle2, AlertCircle } from 'lucide-react';

export default function WatchlistDrawer({
    isOpen,
    onClose,
    enrollForm,
    setEnrollForm,
    selectedFile,
    setSelectedFile,
    enrollStatus,
    handleEnrollSubmit,
    handleSnapWebcamFace,
    enrolledTargets = [],
    enrolledPlates = []
}) {
    useEffect(() => {
        const handleKeyDown = (e) => {
            if (e.key === 'Escape' && isOpen) {
                onClose();
            }
        };
        window.addEventListener('keydown', handleKeyDown);
        return () => window.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, onClose]);

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
                        <UserPlus className="w-5 h-5 text-blue-400" />
                        <div>
                            <h2 className="text-sm font-bold tracking-wide text-slate-100 uppercase">
                                WATCHLIST ENROLLMENT
                            </h2>
                            <p className="text-[10px] text-slate-400 font-mono">
                                Face Biometrics & License Plate Registry
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

                {/* Form Body */}
                <div className="flex-1 overflow-y-auto p-5 space-y-6 min-h-0">
                    <form onSubmit={handleEnrollSubmit} className="space-y-6">
                        {/* Section 1: Facial Recognition Target */}
                        <section className="space-y-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
                            <label className="text-xs font-mono font-bold text-cyan-300 uppercase tracking-wider flex items-center gap-2">
                                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400"></span>
                                Target Facial Recognition
                            </label>

                            <div className="space-y-1.5">
                                <label className="text-[11px] font-semibold text-slate-400 block">Subject Full Name</label>
                                <input
                                    type="text"
                                    value={enrollForm.name}
                                    onChange={(e) => setEnrollForm(prev => ({ ...prev, name: e.target.value }))}
                                    placeholder="e.g. John Doe / Operator"
                                    className="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs text-slate-200 focus:border-blue-500 outline-none placeholder:text-slate-600"
                                />
                            </div>

                            <div className="space-y-1.5">
                                <label className="text-[11px] font-semibold text-slate-400 block">Portrait Image File</label>
                                <input
                                    type="file"
                                    accept="image/*"
                                    onChange={(e) => setSelectedFile(e.target.files[0])}
                                    className="text-xs text-slate-400 block w-full file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-xs file:font-semibold file:bg-slate-800 file:text-slate-200 hover:file:bg-slate-700 cursor-pointer"
                                />
                            </div>

                            <div className="pt-2">
                                <button
                                    type="button"
                                    onClick={handleSnapWebcamFace}
                                    className="w-full py-2 bg-slate-800 hover:bg-slate-750 text-slate-200 border border-slate-700 rounded-lg text-xs font-semibold transition-all flex items-center justify-center gap-1.5 cursor-pointer"
                                    title="Capture face directly from live physical camera"
                                >
                                    <Camera className="w-4 h-4 text-emerald-400" />
                                    <span>Snap Face from Live Feed</span>
                                </button>
                            </div>
                        </section>

                        {/* Section 2: License Plate Target */}
                        <section className="space-y-3 bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
                            <label className="text-xs font-mono font-bold text-amber-300 uppercase tracking-wider flex items-center gap-2">
                                <Car className="w-4 h-4 text-amber-400" />
                                Target License Plate (ANPR)
                            </label>

                            <div className="space-y-1.5">
                                <label className="text-[11px] font-semibold text-slate-400 block">Vehicle Registration Number</label>
                                <input
                                    type="text"
                                    value={enrollForm.targetPlate}
                                    onChange={(e) => setEnrollForm(prev => ({ ...prev, targetPlate: e.target.value }))}
                                    placeholder="e.g. DL01AB1234 or 7XYZ123"
                                    className="w-full bg-slate-900 border border-slate-800 rounded-lg px-3 py-2 text-xs font-mono uppercase tracking-widest text-amber-300 focus:border-amber-500 outline-none placeholder:font-sans placeholder:tracking-normal placeholder:text-slate-600"
                                />
                            </div>
                        </section>

                        {/* Submit Button */}
                        <button
                            type="submit"
                            disabled={enrollStatus.loading}
                            className="w-full py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg text-xs font-bold uppercase tracking-wider transition-all shadow-md cursor-pointer flex items-center justify-center gap-2"
                        >
                            {enrollStatus.loading ? 'Enrolling Target...' : 'Enroll to Watchlist'}
                        </button>
                    </form>

                    {/* Status Feedback Alerts */}
                    {enrollStatus.success && (
                        <div className="p-3 rounded-lg bg-emerald-950/30 border border-emerald-800/40 text-emerald-400 text-xs flex items-center gap-2">
                            <CheckCircle2 className="w-4 h-4 flex-shrink-0" />
                            <span>{enrollStatus.success}</span>
                        </div>
                    )}

                    {enrollStatus.error && (
                        <div className="p-3 rounded-lg bg-rose-950/30 border border-rose-800/40 text-rose-400 text-xs flex items-center gap-2">
                            <AlertCircle className="w-4 h-4 flex-shrink-0" />
                            <span>{enrollStatus.error}</span>
                        </div>
                    )}

                    {/* Registry Summary */}
                    <div className="p-4 rounded-xl bg-slate-950/40 border border-slate-800/60 space-y-2 font-mono text-xs text-slate-400">
                        <div className="font-bold text-slate-300 text-[11px] uppercase border-b border-slate-800 pb-1">
                            Enrolled Database Summary
                        </div>
                        <div className="flex justify-between">
                            <span>Watchlist Faces:</span>
                            <span className="text-cyan-400 font-bold">{enrolledTargets.length} Registered</span>
                        </div>
                        <div className="flex justify-between">
                            <span>Flagged License Plates:</span>
                            <span className="text-amber-400 font-bold">{enrolledPlates.length} Registered</span>
                        </div>
                    </div>
                </div>
            </aside>
        </div>
    );
}
