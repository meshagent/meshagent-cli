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
SDK_ROOT=""
OLD_IFS="$IFS"
IFS=":"
for path in ${PYTHONPATH:-}; do
  if [ -d "$path/../meshagent-api" ] && [ -d "$path/../meshagent-tools" ]; then
    SDK_ROOT="$(cd "$path/.." && pwd)"
    break
  fi
done
IFS="$OLD_IFS"
if [ -n "$SDK_ROOT" ]; then
  "$VENV_PYTHON" -m pip install -e "$SDK_ROOT/meshagent-api" -e "$SDK_ROOT/meshagent-tools"
fi
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}" "$VENV_PYTHON" -m pip install -e .
