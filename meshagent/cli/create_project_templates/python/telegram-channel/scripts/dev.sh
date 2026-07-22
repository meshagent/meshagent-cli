#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_telegram_env() {
  if [ -f .env ]; then
    set -a
    . ./.env
    set +a
  fi
  TELEGRAM_API_ID="${TELEGRAM_API_ID:-}"
  TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-}"
  TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
}
load_telegram_env
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
find_meshagent_cli() {
  if [ -n "${MESHAGENT_CLI:-}" ]; then
    return
  fi
  if [ -x "$VENV/bin/meshagent" ]; then
    MESHAGENT_CLI="$VENV/bin/meshagent"
    return
  fi
  cli_dir="$ROOT"
  while [ "$cli_dir" != "/" ]; do
    if [ -x "$cli_dir/.venv/bin/meshagent" ]; then
      MESHAGENT_CLI="$cli_dir/.venv/bin/meshagent"
      return
    fi
    cli_dir="$(dirname "$cli_dir")"
  done
  MESHAGENT_CLI="meshagent"
}
find_meshagent_cli
AGENT_NAME="${MESHAGENT_AGENT_NAME:-python-telegram-channel}"
THREAD_STORAGE="${MESHAGENT_THREAD_STORAGE:-dataset}"
IMAGE_GENERATION_MODEL="${MESHAGENT_IMAGE_GENERATION_MODEL:-gpt-image-2}"
if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ] || [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  ./scripts/configure-telegram.sh
  load_telegram_env
fi
if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ] || [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "Missing Telegram credentials." >&2
  echo "Run ./scripts/configure-telegram.sh, then retry ./scripts/dev.sh." >&2
  exit 1
fi
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import meshagent.telegram' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
SITE_PACKAGES="$("$VENV_PYTHON" -c 'import site; print(site.getsitepackages()[0])')"
PYTHONPATH_VALUE="$ROOT:$SITE_PACKAGES"
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$PYTHONPATH"
fi
CHANNEL_COMMAND="$("$VENV_PYTHON" -c 'import json, sys; print("command:" + json.dumps([sys.executable, sys.argv[1]]))' "$ROOT/server.py")"
echo "Pick a room, then the Telegram channel will start agent $AGENT_NAME."
set +e
env PYTHONPATH="$PYTHONPATH_VALUE" "$MESHAGENT_CLI" room connect "$@" -- \
  "$MESHAGENT_CLI" process join \
    --agent-name "$AGENT_NAME" \
    --channel chat \
    --channel "$CHANNEL_COMMAND" \
    --thread-storage "$THREAD_STORAGE" \
    --image-generation "$IMAGE_GENERATION_MODEL"
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped Telegram channel."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
