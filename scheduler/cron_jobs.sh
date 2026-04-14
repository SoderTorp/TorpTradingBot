#!/usr/bin/env bash
# TorpTradingBot cron job definitions.
# Install with: crontab scheduler/cron_jobs.sh
# All times are UTC.

# Refresh wallet discovery and scores — daily at 06:00 UTC
0 6 * * * cd /app && python main.py --task discover_wallets >> /app/logs/cron.log 2>&1

# Check tracked wallets for new trades — every 15 minutes
*/15 * * * * cd /app && python main.py --task copy_trades >> /app/logs/cron.log 2>&1

# Scan for suspicious new accounts — every 6 hours
0 */6 * * * cd /app && python main.py --task scan_suspicious >> /app/logs/cron.log 2>&1

# Copy trades from suspicious wallets — every 15 minutes
*/15 * * * * cd /app && python main.py --task copy_suspicious >> /app/logs/cron.log 2>&1
