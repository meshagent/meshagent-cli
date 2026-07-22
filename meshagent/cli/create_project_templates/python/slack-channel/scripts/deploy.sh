#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_slack_env() {
  if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        ""|\#*|*=*)
          ;;
        *)
          continue
          ;;
      esac
      case "$line" in
        ""|\#*)
          continue
          ;;
      esac
      name="${line%%=*}"
      value="${line#*=}"
      case "$name" in
        ""|[0-9]*|*[!A-Za-z0-9_]*)
          continue
          ;;
      esac
      eval "is_set=\${$name+x}"
      if [ -z "$is_set" ]; then
        export "$name=$value"
      fi
    done <.env
  fi
  SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
  SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:-}"
  MESHAGENT_SLACK_SERVICE_ACCOUNT_NAME="${MESHAGENT_SLACK_SERVICE_ACCOUNT_NAME:-python-slack-channel}"
  MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL="${MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL:-}"
  MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID="${MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID:-}"
  MESHAGENT_SLACK_SIGNING_SECRET_ID="${MESHAGENT_SLACK_SIGNING_SECRET_ID:-}"
  MESHAGENT_SLACK_EVENTS_DOMAIN="${MESHAGENT_SLACK_EVENTS_DOMAIN:-}"
  MESHAGENT_SLACK_EVENTS_URL="${MESHAGENT_SLACK_EVENTS_URL:-}"
  MESHAGENT_SLACK_ALLOWED_CHANNELS="${MESHAGENT_SLACK_ALLOWED_CHANNELS:-}"
  MESHAGENT_SLACK_THREAD_PREFIX="${MESHAGENT_SLACK_THREAD_PREFIX:-threads/slack}"
}
load_slack_env
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
MESHAGENT_CLI="${MESHAGENT_CLI:-$VENV/bin/meshagent}"
if [ ! -x "$VENV_PYTHON" ] || [ ! -x "$MESHAGENT_CLI" ]; then
  ./scripts/install.sh
fi
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-slack-channel:dev}"
if [ "${MESHAGENT_SLACK_AUTO_CONFIGURE:-1}" = "1" ] && [ "${MESHAGENT_SLACK_SKIP_CONFIGURE:-}" != "1" ]; then
  if { [ -z "$SLACK_BOT_TOKEN" ] && [ -z "$MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID" ]; } || { [ -z "$SLACK_SIGNING_SECRET" ] && [ -z "$MESHAGENT_SLACK_SIGNING_SECRET_ID" ]; }; then
    if [ -t 0 ] && [ -t 1 ]; then
      ./scripts/configure-slack.sh
      load_slack_env
    else
      echo "Missing Slack credentials. Run ./scripts/configure-slack.sh, set SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET, or set existing Slack secret IDs before deploy." >&2
      exit 1
    fi
  fi
fi

ROOM_NAME=""
PROJECT_ID=""
ROOM_ARG_PRESENT="false"
NEXT_IS_ROOM="false"
NEXT_IS_PROJECT_ID="false"
for arg in "$@"; do
  if [ "$NEXT_IS_ROOM" = "true" ]; then
    ROOM_NAME="$arg"
    ROOM_ARG_PRESENT="true"
    NEXT_IS_ROOM="false"
    continue
  fi
  if [ "$NEXT_IS_PROJECT_ID" = "true" ]; then
    PROJECT_ID="$arg"
    NEXT_IS_PROJECT_ID="false"
    continue
  fi
  case "$arg" in
    --room)
      NEXT_IS_ROOM="true"
      ROOM_ARG_PRESENT="true"
      ;;
    --room=*)
      ROOM_NAME="${arg#--room=}"
      ROOM_ARG_PRESENT="true"
      ;;
    --project-id)
      NEXT_IS_PROJECT_ID="true"
      ;;
    --project-id=*)
      PROJECT_ID="${arg#--project-id=}"
      ;;
  esac
done

if [ "${MESHAGENT_SLACK_DEPLOY_IN_ROOM:-}" != "1" ] && [ -z "${MESHAGENT_ROOM:-}" ]; then
  if [ -n "$ROOM_NAME" ] && [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_SLACK_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --project-id "$PROJECT_ID" --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$ROOM_NAME" ]; then
    exec env MESHAGENT_SLACK_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_SLACK_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --project-id "$PROJECT_ID" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  exec env MESHAGENT_SLACK_DEPLOY_IN_ROOM=1 \
    "$MESHAGENT_CLI" room connect -- "$ROOT/scripts/deploy.sh" "$@"
fi

ROOM_NAME="${MESHAGENT_ROOM:-$ROOM_NAME}"
PROJECT_ID="${MESHAGENT_PROJECT_ID:-$PROJECT_ID}"
if [ "${MESHAGENT_SLACK_DEPLOY_IN_ROOM:-}" = "1" ]; then
  unset MESHAGENT_TOKEN
  unset OPENAI_API_KEY
  unset ANTHROPIC_API_KEY
fi
if [ -z "$ROOM_NAME" ]; then
  echo "Deploy needs a MeshAgent room. Run ./scripts/deploy.sh to use the room picker." >&2
  echo "For non-interactive deploys, pass --room <room> or set MESHAGENT_ROOM." >&2
  exit 1
fi

meshagent_with_project() {
  if [ -n "$PROJECT_ID" ]; then
    "$MESHAGENT_CLI" "$@" --project-id "$PROJECT_ID"
  else
    "$MESHAGENT_CLI" "$@"
  fi
}

prepare_slack_deploy_app() {
  if [ "${MESHAGENT_SLACK_SKIP_VENDOR:-}" = "1" ]; then
    return
  fi
  deploy_app_dir="$ROOT/.meshagent/deploy-app"
  echo "Preparing Slack deploy package..."
  rm -rf "$deploy_app_dir"
  mkdir -p "$deploy_app_dir"
  "$VENV_PYTHON" -m pip install --disable-pip-version-check --no-warn-conflicts \
    --target "$deploy_app_dir" \
    --no-deps \
    --upgrade \
    "$ROOT"
  find "$deploy_app_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
}

json_secret_id_by_name() {
  secret_name="$1"
  "$VENV_PYTHON" -c 'import json, sys
name = sys.argv[1]
data = json.load(sys.stdin)
for secret in data.get("secrets", []):
    if secret.get("name") == name and secret.get("id"):
        print(secret["id"])
        break
' "$secret_name"
}

json_secret_id() {
  "$VENV_PYTHON" -c 'import json, sys; data=json.load(sys.stdin); print(data.get("id") or "")'
}

route_domain_from_url() {
  if [ -z "$MESHAGENT_SLACK_EVENTS_URL" ]; then
    printf ''
    return
  fi
  SLACK_EVENTS_URL_VALUE="$MESHAGENT_SLACK_EVENTS_URL" "$VENV_PYTHON" - <<'PY'
import os
import urllib.parse

url = os.environ["SLACK_EVENTS_URL_VALUE"].strip()
parsed = urllib.parse.urlparse(url)
print(parsed.netloc or "")
PY
}

route_subdomain_from_room() {
  SLACK_ROOM_NAME_VALUE="$ROOM_NAME" "$VENV_PYTHON" - <<'PY'
import os
import re

room_name = os.environ["SLACK_ROOM_NAME_VALUE"].strip().lower()
subdomain = re.sub(r"[^a-z0-9-]+", "-", room_name)
subdomain = re.sub(r"-+", "-", subdomain).strip("-")
print(subdomain or "python-slack-channel")
PY
}

configured_pages_domain() {
  pages_domain="$(
    "$MESHAGENT_CLI" config get domains.pages 2>/dev/null || true
  )"
  printf '%s' "$pages_domain" | "$VENV_PYTHON" -c 'import sys; print(sys.stdin.read().strip().lower().removeprefix("."))'
}

resolve_slack_events_domain() {
  if [ -n "$MESHAGENT_SLACK_EVENTS_DOMAIN" ]; then
    printf '%s' "$MESHAGENT_SLACK_EVENTS_DOMAIN"
    return
  fi
  url_domain="$(route_domain_from_url)"
  if [ -n "$url_domain" ]; then
    printf '%s' "$url_domain"
    return
  fi
  pages_domain="$(configured_pages_domain)"
  if [ -z "$pages_domain" ]; then
    printf ''
    return
  fi
  printf '%s.%s' "$(route_subdomain_from_room)" "$pages_domain"
}

resolve_slack_service_account() {
  if [ -n "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" ]; then
    printf '%s' "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL"
    return
  fi

  SLACK_SERVICE_ACCOUNT_NAME_VALUE="$MESHAGENT_SLACK_SERVICE_ACCOUNT_NAME" \
  SLACK_PROJECT_ID_VALUE="$PROJECT_ID" \
  "$VENV_PYTHON" - <<'PY'
import asyncio
import os
import sys

from meshagent.api.client import ServiceAccount
from meshagent.cli.helper import get_client, resolve_project_id


def _matches(account: ServiceAccount, name: str) -> bool:
    candidates = {
        account.id,
        account.key,
        account.name,
        account.email,
        account.display_name,
    }
    return name in candidates


def _run_as_value(account: ServiceAccount, fallback: str) -> str:
    value = account.email or account.name or fallback
    return str(value).strip()


async def main():
    name = os.environ["SLACK_SERVICE_ACCOUNT_NAME_VALUE"].strip()
    project_arg = os.environ.get("SLACK_PROJECT_ID_VALUE") or None
    project_id = await resolve_project_id(project_id=project_arg)
    client = await get_client()
    try:
        for filter_value in (name, ""):
            page = await client.list_service_accounts(
                project_id=project_id,
                page_size=100,
                filter=filter_value or None,
            )
            for account in page.service_accounts:
                if _matches(account, name):
                    print(_run_as_value(account, name))
                    return

        created = await client.create_service_account(
            project_id=project_id,
            name=name,
            display_name="Python Slack Channel",
            description="Runs the Python Slack channel service.",
            metadata=None,
            annotations=None,
        )
        print(_run_as_value(created, name))
    finally:
        await client.close()


try:
    asyncio.run(main())
except Exception as exc:
    print(f"Could not resolve service account {os.environ['SLACK_SERVICE_ACCOUNT_NAME_VALUE']}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

find_service_account_secret_id() {
  secret_name="$1"
  secret_json="$(
    meshagent_with_project secret search \
      --subject "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" \
      --name "$secret_name" \
      -o json 2>/dev/null || true
  )"
  if [ -z "$secret_json" ]; then
    printf ''
    return
  fi
  printf '%s' "$secret_json" | json_secret_id_by_name "$secret_name"
}

write_temp_secret_file() {
  secret_value="$1"
  secret_file="$(mktemp "${TMPDIR:-/tmp}/meshagent-slack-secret.XXXXXX")"
  chmod 600 "$secret_file"
  printf '%s' "$secret_value" >"$secret_file"
  printf '%s' "$secret_file"
}

upsert_service_account_secret() {
  secret_name="$1"
  secret_value="$2"
  secret_id="$(find_service_account_secret_id "$secret_name")"
  secret_file="$(write_temp_secret_file "$secret_value")"
  trap 'rm -f "$secret_file"' EXIT HUP INT TERM
  if [ -z "$secret_id" ]; then
    secret_json="$(
      meshagent_with_project secret create \
        "$secret_name" \
        --subject "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" \
        --type opaque \
        --http-only \
        --value-file "$secret_file" \
        -o json
    )"
    secret_id="$(printf '%s' "$secret_json" | json_secret_id)"
  else
    meshagent_with_project secret add-version \
      --subject "$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL" \
      "$secret_id" \
      --value-file "$secret_file" \
      -o json >/dev/null
  fi
  rm -f "$secret_file"
  trap - EXIT HUP INT TERM
  if [ -z "$secret_id" ]; then
    echo "Could not resolve secret ID for $secret_name." >&2
    exit 1
  fi
  printf '%s' "$secret_id"
}

MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL="$(resolve_slack_service_account)"
MESHAGENT_SLACK_EVENTS_DOMAIN="$(resolve_slack_events_domain)"
if [ -z "$MESHAGENT_SLACK_EVENTS_DOMAIN" ]; then
  echo "Missing Slack Events API route domain." >&2
  echo "Set MESHAGENT_SLACK_EVENTS_DOMAIN, set MESHAGENT_SLACK_EVENTS_URL, or configure a MeshAgent pages domain." >&2
  exit 1
fi
if [ -z "$MESHAGENT_SLACK_EVENTS_URL" ]; then
  MESHAGENT_SLACK_EVENTS_URL="https://$MESHAGENT_SLACK_EVENTS_DOMAIN/"
fi
if [ -n "$SLACK_BOT_TOKEN" ]; then
  MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID="$(
    upsert_service_account_secret slack-bot-token "$SLACK_BOT_TOKEN"
  )"
elif [ -z "$MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID" ]; then
  MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID="$(find_service_account_secret_id slack-bot-token)"
fi
if [ -z "$MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID" ]; then
  echo "Missing slack-bot-token service account secret." >&2
  echo "Set SLACK_BOT_TOKEN once, or set MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID." >&2
  exit 1
fi

if [ -n "$SLACK_SIGNING_SECRET" ]; then
  MESHAGENT_SLACK_SIGNING_SECRET_ID="$(
    upsert_service_account_secret slack-signing-secret "$SLACK_SIGNING_SECRET"
  )"
elif [ -z "$MESHAGENT_SLACK_SIGNING_SECRET_ID" ]; then
  MESHAGENT_SLACK_SIGNING_SECRET_ID="$(find_service_account_secret_id slack-signing-secret)"
fi
if [ -z "$MESHAGENT_SLACK_SIGNING_SECRET_ID" ]; then
  echo "Missing slack-signing-secret service account secret." >&2
  echo "Set SLACK_SIGNING_SECRET once, or set MESHAGENT_SLACK_SIGNING_SECRET_ID." >&2
  exit 1
fi

unset SLACK_BOT_TOKEN
unset SLACK_SIGNING_SECRET
if [ "$ROOM_ARG_PRESENT" = "true" ]; then
  set -- "$@" --tag "$IMAGE_TAG" --meshagent-token agentDefault
else
  set -- "$@" --room "$ROOM_NAME" --tag "$IMAGE_TAG" --meshagent-token agentDefault
fi
set -- "$@" --set "slack_service_account_email=$MESHAGENT_SLACK_SERVICE_ACCOUNT_EMAIL"
set -- "$@" --set "slack_bot_token_secret_id=$MESHAGENT_SLACK_BOT_TOKEN_SECRET_ID"
set -- "$@" --set "slack_signing_secret_id=$MESHAGENT_SLACK_SIGNING_SECRET_ID"
set -- "$@" --set "slack_thread_prefix=$MESHAGENT_SLACK_THREAD_PREFIX"
set -- "$@" --set "domain=$MESHAGENT_SLACK_EVENTS_DOMAIN"
if [ -n "$MESHAGENT_SLACK_ALLOWED_CHANNELS" ]; then
  set -- "$@" --set "slack_allowed_channels=$MESHAGENT_SLACK_ALLOWED_CHANNELS"
fi
prepare_slack_deploy_app
echo ""
echo "Slack Events API Request URL:"
echo "  $MESHAGENT_SLACK_EVENTS_URL"
echo ""
echo "Paste this URL into the @meshagent Slack app Event Subscriptions Request URL, then save and reinstall the app if Slack prompts for it."
exec "$MESHAGENT_CLI" deploy . "$@" --wait
