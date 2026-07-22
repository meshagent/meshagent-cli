#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_twilio_env_file() {
  env_file="$1"
  if [ -f "$env_file" ]; then
    set -a
    . "$env_file"
    set +a
  fi
}
load_twilio_env_from_parent() {
  env_dir="$ROOT"
  while [ "$env_dir" != "/" ]; do
    env_file="$env_dir/.env-twilio"
    if [ -f "$env_file" ]; then
      load_twilio_env_file "$env_file"
      return
    fi
    env_dir="$(dirname "$env_dir")"
  done
}
load_twilio_env() {
  load_twilio_env_from_parent
  load_twilio_env_file .env
  TWILIO_ACCOUNT_SID="${TWILIO_ACCOUNT_SID:-}"
  TWILIO_AUTH_TOKEN="${TWILIO_AUTH_TOKEN:-}"
}
load_twilio_env
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
AGENT_NAME="${MESHAGENT_AGENT_NAME:-python-twilio-channel}"
THREAD_STORAGE="${MESHAGENT_THREAD_STORAGE:-dataset}"
if [ -z "${TWILIO_ACCOUNT_SID:-}" ] || [ -z "${TWILIO_AUTH_TOKEN:-}" ]; then
  echo "Missing Twilio credentials." >&2
  echo "Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env, .env-twilio, or the shell, then retry ./scripts/dev.sh." >&2
  exit 1
fi
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import meshagent.twilio' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
SITE_PACKAGES="$("$VENV_PYTHON" -c 'import site; print(site.getsitepackages()[0])')"
PYTHONPATH_VALUE="$ROOT:$SITE_PACKAGES"
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$PYTHONPATH"
fi
CHANNEL_COMMAND="$("$VENV_PYTHON" -c 'import json, sys; print("command:" + json.dumps([sys.executable, sys.argv[1]]))' "$ROOT/server.py")"
echo "Pick a room, then the Twilio channel will start agent $AGENT_NAME."
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
  echo "Stopped Twilio channel."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
