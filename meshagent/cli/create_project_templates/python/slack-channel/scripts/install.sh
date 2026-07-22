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
sdk_root_has_packages() {
  candidate="$1"
  [ -d "$candidate/meshagent-api" ] && [ -d "$candidate/meshagent-tools" ] && [ -d "$candidate/meshagent-agents" ] && [ -d "$candidate/meshagent-openai" ] && [ -d "$candidate/meshagent-anthropic" ] && [ -d "$candidate/meshagent-llm-proxy" ] && [ -d "$candidate/meshagent-codex" ] && [ -d "$candidate/meshagent-cli" ] && [ -d "$candidate/meshagent-slack-channel" ]
}
set_sdk_root() {
  candidate="$1"
  if sdk_root_has_packages "$candidate"; then
    SDK_ROOT="$(cd "$candidate" && pwd)"
    return 0
  fi
  return 1
}
if [ -n "${MESHAGENT_SDK_ROOT:-}" ]; then
  set_sdk_root "$MESHAGENT_SDK_ROOT" || {
    echo "MESHAGENT_SDK_ROOT does not point to a MeshAgent SDK checkout with the Slack channel dependencies." >&2
    exit 1
  }
fi
OLD_IFS="$IFS"
IFS=":"
for path in ${PYTHONPATH:-}; do
  if [ -n "$SDK_ROOT" ]; then
    break
  fi
  set_sdk_root "$path/.." || true
done
IFS="$OLD_IFS"
if [ -z "$SDK_ROOT" ]; then
  search_dir="$ROOT"
  while [ "$search_dir" != "/" ] && [ -z "$SDK_ROOT" ]; do
    set_sdk_root "$search_dir/meshagent-sdk" || true
    set_sdk_root "$search_dir/meshagent-server/meshagent-sdk" || true
    set_sdk_root "$search_dir/../meshagent-server/meshagent-sdk" || true
    search_dir="$(dirname "$search_dir")"
  done
fi
if [ -n "$SDK_ROOT" ]; then
  "$VENV_PYTHON" -m pip install -e "$SDK_ROOT/meshagent-api" -e "$SDK_ROOT/meshagent-tools" -e "$SDK_ROOT/meshagent-agents" -e "$SDK_ROOT/meshagent-openai" -e "$SDK_ROOT/meshagent-anthropic" -e "$SDK_ROOT/meshagent-llm-proxy" -e "$SDK_ROOT/meshagent-codex" -e "$SDK_ROOT/meshagent-cli" -e "$SDK_ROOT/meshagent-slack-channel"
fi
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}" "$VENV_PYTHON" -m pip install -e .
