#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3.13}"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  "$PYTHON" -m venv "$VENV"
  "$VENV_PYTHON" -m pip install --upgrade pip
fi
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}" "$VENV_PYTHON" -m pip install -e .
