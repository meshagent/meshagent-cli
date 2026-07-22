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
SDK_ROOT="${SDK_ROOT:-}"
is_sdk_root() {
  [ -d "$1/meshagent-api" ] && [ -d "$1/meshagent-tools" ] && [ -d "$1/meshagent-agents" ] && [ -d "$1/meshagent-openai" ] && [ -d "$1/meshagent-twilio" ]
}
OLD_IFS="$IFS"
IFS=":"
for path in ${PYTHONPATH:-}; do
  if [ -z "$SDK_ROOT" ] && is_sdk_root "$path/.."; then
    SDK_ROOT="$(cd "$path/.." && pwd)"
    break
  fi
done
IFS="$OLD_IFS"
if [ -z "$SDK_ROOT" ]; then
  search_dir="$ROOT"
  while [ "$search_dir" != "/" ]; do
    for candidate in \
      "$search_dir/meshagent-sdk" \
      "$search_dir/meshagent-server/meshagent-sdk" \
      "$search_dir/../meshagent-server/meshagent-sdk"
    do
      if is_sdk_root "$candidate"; then
        SDK_ROOT="$(cd "$candidate" && pwd)"
        break 2
      fi
    done
    search_dir="$(dirname "$search_dir")"
  done
fi
if [ -n "$SDK_ROOT" ]; then
  "$VENV_PYTHON" -m pip install -e "$SDK_ROOT/meshagent-api" -e "$SDK_ROOT/meshagent-tools" -e "$SDK_ROOT/meshagent-agents" -e "$SDK_ROOT/meshagent-openai" -e "$SDK_ROOT/meshagent-twilio"
fi
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}" "$VENV_PYTHON" -m pip install -e .
