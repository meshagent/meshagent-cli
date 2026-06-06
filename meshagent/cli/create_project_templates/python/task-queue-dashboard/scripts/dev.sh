#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import aiohttp, meshagent.api' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
PORT="${PORT:-8000}"
LOCAL_URL="http://127.0.0.1:$PORT/"
TASK_QUEUE_DASHBOARD_LOCAL_URL="$LOCAL_URL"
TASK_QUEUE_DASHBOARD_OPEN_BROWSER="${TASK_QUEUE_DASHBOARD_OPEN_BROWSER:-1}"
export TASK_QUEUE_DASHBOARD_LOCAL_URL TASK_QUEUE_DASHBOARD_OPEN_BROWSER
echo "Pick a room, then the dashboard will launch at $LOCAL_URL"
set +e
meshagent room connect "$@" -- "$VENV_PYTHON" -u server.py
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped task queue dashboard."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
