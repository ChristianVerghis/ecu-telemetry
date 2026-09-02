#!/usr/bin/env bash
# Start ecu-telemetry backend on http://localhost:8780 (UDP :8781, TCP :8782)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/backend"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

PORT="${PORT:-${ECU_HTTP_PORT:-8780}}"
echo "starting ecu-telemetry on http://localhost:${PORT}"
exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
