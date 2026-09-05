#!/bin/sh
set -e

# Generate runtime env-config.js from environment variables
cat <<EOF > /usr/share/nginx/html/env-config.js
window.__ENV__ = {
  VITE_CENTRAL_API_KEY: "${VITE_CENTRAL_API_KEY:-}",
  VITE_CENTRAL_API_URL: "${VITE_CENTRAL_API_URL:-}",
  VITE_CENTRAL_WS_URL: "${VITE_CENTRAL_WS_URL:-}"
};
EOF

exec "$@"
