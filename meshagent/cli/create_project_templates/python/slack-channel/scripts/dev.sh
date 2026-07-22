#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_slack_env() {
  if [ -f .env ]; then
    set -a
    . ./.env
    set +a
  fi
  SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
  SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:-}"
  MESHAGENT_SLACK_SIGNING_SECRET_ID="${MESHAGENT_SLACK_SIGNING_SECRET_ID:-}"
  MESHAGENT_SLACK_DRY_RUN="${MESHAGENT_SLACK_DRY_RUN:-0}"
}
load_slack_env
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
MESHAGENT_CLI="${MESHAGENT_CLI:-$VENV/bin/meshagent}"
AGENT_NAME="${MESHAGENT_AGENT_NAME:-python-slack-channel}"
THREAD_STORAGE="${MESHAGENT_THREAD_STORAGE:-dataset}"
IMAGE_GENERATION_MODEL="${MESHAGENT_IMAGE_GENERATION_MODEL:-gpt-image-2}"
MESHAGENT_SLACK_EVENT_STDOUT="${MESHAGENT_SLACK_EVENT_STDOUT:-1}"
export MESHAGENT_SLACK_EVENT_STDOUT
if [ -z "${SLACK_BOT_TOKEN:-}" ] && [ "$MESHAGENT_SLACK_DRY_RUN" != "1" ]; then
  ./scripts/configure-slack.sh
  load_slack_env
fi
if [ -z "${SLACK_BOT_TOKEN:-}" ] && [ "$MESHAGENT_SLACK_DRY_RUN" != "1" ]; then
  echo "Missing Slack bot token." >&2
  echo "Run ./scripts/configure-slack.sh, then retry ./scripts/dev.sh." >&2
  echo "For local queue-only tests, set MESHAGENT_SLACK_DRY_RUN=1." >&2
  exit 1
fi
if [ ! -x "$VENV_PYTHON" ] || [ ! -x "$MESHAGENT_CLI" ] || ! "$VENV_PYTHON" -c 'import meshagent.slack_channel' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
SITE_PACKAGES="$("$VENV_PYTHON" -c 'import site; print(site.getsitepackages()[0])')"
PYTHONPATH_VALUE="$ROOT:$SITE_PACKAGES"
if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH_VALUE="$PYTHONPATH_VALUE:$PYTHONPATH"
fi
echo "Pick a room, then the Slack channel will start agent $AGENT_NAME."
echo "The dev script will configure a Slack Events API Request URL for the selected room."
set +e
env PYTHONPATH="$PYTHONPATH_VALUE" "$MESHAGENT_CLI" room connect "$@" -- \
  sh -c '
    set -eu
    env -u MESHAGENT_TOKEN -u OPENAI_API_KEY -u ANTHROPIC_API_KEY "$0" "$1"
    channel_command="$("$0" -c '\''import json, sys; print("command:" + json.dumps([sys.executable, sys.argv[1]]))'\'' "$4/server.py")"
    exec "$2" process join \
      --agent-name "$3" \
      --channel chat \
      --channel "$channel_command" \
      --thread-storage "$5" \
      --image-generation "$6"
  ' \
    "$VENV_PYTHON" \
    "$ROOT/scripts/setup-slack-dev-route.py" \
    "$MESHAGENT_CLI" \
    "$AGENT_NAME" \
    "$ROOT" \
    "$THREAD_STORAGE" \
    "$IMAGE_GENERATION_MODEL"
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped Slack channel."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
