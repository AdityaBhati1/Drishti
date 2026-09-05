import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error("SOC App Crash caught by ErrorBoundary:", error, errorInfo);
  }

  handleReset = () => {
    try {
      localStorage.clear();
      sessionStorage.clear();
    } catch (e) {}
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div style={{
          minHeight: '100vh',
          backgroundColor: '#020617',
          color: '#f8fafc',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'sans-serif',
          padding: '2rem',
          textAlign: 'center'
        }}>
          <div style={{
            maxWidth: '500px',
            backgroundColor: '#0f172a',
            border: '1px solid #1e293b',
            borderRadius: '1rem',
            padding: '2rem',
            boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)'
          }}>
            <h1 style={{ fontSize: '1.25rem', fontWeight: 'bold', color: '#f43f5e', marginBottom: '1rem' }}>
              ⚠️ SOC Dashboard Recovery Mode
            </h1>
            <p style={{ fontSize: '0.875rem', color: '#94a3b8', marginBottom: '1.5rem', lineHeight: '1.5' }}>
              An unexpected render exception occurred. Click below to clear stored state and restore system live feeds.
            </p>
            <div style={{
              backgroundColor: '#020617',
              border: '1px solid #334155',
              padding: '0.75rem',
              borderRadius: '0.5rem',
              fontSize: '0.75rem',
              color: '#fda4af',
              fontFamily: 'monospace',
              marginBottom: '1.5rem',
              wordBreak: 'break-all',
              textAlign: 'left'
            }}>
              {this.state.error?.toString() || 'Unknown Runtime Exception'}
            </div>
            <button
              onClick={this.handleReset}
              style={{
                width: '100%',
                padding: '0.75rem',
                backgroundColor: '#2563eb',
                color: '#ffffff',
                fontWeight: '600',
                borderRadius: '0.5rem',
                border: 'none',
                cursor: 'pointer'
              }}
            >
              🔄 Reset App & Reload Feeds
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
