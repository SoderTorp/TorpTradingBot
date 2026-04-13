#!/bin/bash
set -e

# Expose environment variables to cron jobs
printenv | grep -v "no_proxy" | grep -v "^_=" >> /etc/environment

# Ensure log file exists so tail doesn't fail on first run
touch /app/logs/cron.log

# Start cron daemon
service cron start

echo "TradingBot started. Cron jobs active."
echo "Tailing /app/logs/cron.log — use 'docker logs' to follow."

# Start web UI in background
cd /app && python web/app.py &

echo "Web UI available at http://0.0.0.0:6060"

# Keep container alive
tail -f /app/logs/cron.log
