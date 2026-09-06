#!/bin/bash
# Wrapper for cron/launchd: activates venv, loads env vars, runs scanner, logs output.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && source .env && set +a
mkdir -p logs
./venv/bin/python put_scanner.py >> "logs/run_$(date +%Y-%m-%d).log" 2>&1
