#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${QUANT_AKSHARE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
PYTHON_BIN="${QUANT_AKSHARE_PYTHON:-$PROJECT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="${PYTHON:-python3}"
fi

if ! curl -fsS http://127.0.0.1:18765/healthz >/dev/null 2>&1; then
  cd "$PROJECT_DIR"
  nohup "$PYTHON_BIN" -m quant_akshare.cli ai-server >> /tmp/a_stock_ai_server.log 2>&1 &
  echo "A-share AI service started: http://127.0.0.1:18765"
fi

if curl -fsS http://127.0.0.1:18766/healthz >/dev/null 2>&1; then
  echo "A-share dashboard is already running: http://127.0.0.1:18766/dashboard"
  exit 0
fi

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" -m quant_akshare.cli qclaw-service
