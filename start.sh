#!/bin/bash
# Startup script: run Flask initialization then start gunicorn
echo "🚅 THSRC Sniper — Starting..."

# Use gunicorn with preload to trigger startup()
exec gunicorn \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --preload \
    "wsgi:app"
