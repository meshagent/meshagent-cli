#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import aiofiles, meshagent.api, meshagent.tools' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
echo "Pick a room, then the Python agent toolkit will connect."
set +e
meshagent room connect "$@" -- "$VENV_PYTHON" -u server.py
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped Python agent toolkit."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
