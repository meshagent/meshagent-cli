#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import aiofiles, aiohttp, meshagent.api, meshagent.tools' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
PORT="${PORT:-8000}"
LOCAL_URL="http://127.0.0.1:$PORT/"
PYTHON_WEBSERVER_LOCAL_URL="$LOCAL_URL"
PYTHON_WEBSERVER_OPEN_BROWSER="${PYTHON_WEBSERVER_OPEN_BROWSER:-1}"
export PYTHON_WEBSERVER_LOCAL_URL PYTHON_WEBSERVER_OPEN_BROWSER
echo "Pick a room, then the web app will launch at $LOCAL_URL"
set +e
meshagent room connect "$@" -- "$VENV_PYTHON" -u server.py
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped Python web app."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
