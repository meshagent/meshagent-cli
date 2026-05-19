#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "Missing $VENV_PYTHON. Run the install script first." >&2
  exit 1
fi
meshagent room connect -- "$VENV_PYTHON" -u server.py
