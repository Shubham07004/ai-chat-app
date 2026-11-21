#!/bin/sh
# Writes /config.js from the API_URL environment variable each time the container starts.
set -eu
url="${API_URL:-http://localhost:8000}"
escaped=$(printf '%s' "$url" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf 'window.__APP_CONFIG__ = { API_URL: "%s" };\n' "$escaped" > /usr/share/nginx/html/config.js
