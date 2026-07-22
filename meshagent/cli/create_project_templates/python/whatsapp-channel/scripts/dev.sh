#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_whatsapp_env() {
  if [ -f .env ]; then
    set -a
    . ./.env
    set +a
  fi
  WHATSAPP_ACCESS_TOKEN="${WHATSAPP_ACCESS_TOKEN:-}"
  WHATSAPP_PHONE_NUMBER_ID="${WHATSAPP_PHONE_NUMBER_ID:-}"
}
load_whatsapp_env
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
AGENT_NAME="${MESHAGENT_AGENT_NAME:-python-whatsapp-channel}"
THREAD_STORAGE="${MESHAGENT_THREAD_STORAGE:-dataset}"
MESHAGENT_SAMPLE_QUEUE_MODE=1
export MESHAGENT_SAMPLE_QUEUE_MODE
if [ -z "${WHATSAPP_ACCESS_TOKEN:-}" ] || [ -z "${WHATSAPP_PHONE_NUMBER_ID:-}" ]; then
  echo "Missing WhatsApp credentials." >&2
  echo "Copy .env.example to .env, fill WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID, then retry ./scripts/dev.sh." >&2
  exit 1
fi
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import channel' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
SITE_PACKAGES="$("$VENV_PYTHON" -c 'import site; print(site.getsitepackages()[0])')"
PYTHONPATH_VALUE="$ROOT:$SITE_PACKAGES"
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$PYTHONPATH"
fi
CHANNEL_COMMAND="$("$VENV_PYTHON" -c 'import json, sys; print("command:" + json.dumps([sys.executable, sys.argv[1]]))' "$ROOT/server.py")"
echo "Pick a room, then the WhatsApp channel will start agent $AGENT_NAME."
set +e
env PYTHONPATH="$PYTHONPATH_VALUE" "$MESHAGENT_CLI" room connect "$@" -- \
  "$MESHAGENT_CLI" process join \
    --agent-name "$AGENT_NAME" \
    --channel chat \
    --channel "$CHANNEL_COMMAND" \
    --thread-storage "$THREAD_STORAGE"
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped WhatsApp channel."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
