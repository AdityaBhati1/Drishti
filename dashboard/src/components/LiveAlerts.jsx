import React from 'react';
import { Bell, Clock, Compass, ShieldAlert, MapPin } from 'lucide-react';

export default function LiveAlerts({ alerts = [] }) {
  return (
    <div className="alerts-panel glass">
      <div className="panel-header">
        <h2>
          <Bell size={18} className="text-blue-400" />
          Live Security Feed
        </h2>
        <span className="status-badge">
          <div className="status-indicator"></div>
          Active
        </span>
      </div>

      <div className="alert-list">
        {alerts.length === 0 ? (
          <div className="no-alerts">
            <ShieldAlert size={36} className="text-gray-500" />
            <span>No alerts recorded in this session.</span>
            <span style={{ fontSize: '0.75rem', color: '#64748b' }}>Waiting for detections...</span>
          </div>
        ) : (
          alerts.map((alert) => (
            <div
              key={alert.id || alert.timestamp}
              className={`alert-card ${(alert.severity || 'CRITICAL').toLowerCase()}`}
            >
              <div className="alert-card-header">
                <div>
                  <div className="alert-type">{alert.eventType || alert.event_type}</div>
                  <div className="alert-node">{alert.cameraId || alert.node_id}</div>
                </div>
                <span className={`alert-severity ${(alert.severity || 'CRITICAL').toLowerCase()}`}>
                  {alert.severity || 'CRITICAL'}
                </span>
              </div>

              <div className="alert-details">
                {alert.details}
              </div>

              {/* Exact Location Address Display */}
              {alert.address && (
                <div style={{ fontSize: '0.8rem', color: '#38bdf8', marginTop: '0.35rem', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                  <MapPin size={12} />
                  <span>{alert.address}</span>
                </div>
              )}

              {alert.extra_info && (
                <div style={{ fontSize: '0.8rem', color: '#94a3b8', fontStyle: 'italic', marginTop: '0.25rem' }}>
                  Info: {alert.extra_info}
                </div>
              )}

              <div className="alert-meta">
                <span className="alert-geo">
                  <Compass size={12} />
                  {typeof alert.lat === 'number' ? alert.lat.toFixed(4) : alert.lat}, {typeof alert.lng === 'number' ? alert.lng.toFixed(4) : alert.lng}
                </span>

                {/* Renders exact capture timestamp string down to milliseconds */}
                <span style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
                  <Clock size={12} />
                  {alert.timestamp}
                </span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}