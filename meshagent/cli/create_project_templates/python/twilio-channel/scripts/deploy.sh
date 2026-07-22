#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_twilio_env_file() {
  env_file="$1"
  if [ -f "$env_file" ]; then
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
    done <"$env_file"
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
  MESHAGENT_TWILIO_SERVICE_ACCOUNT_NAME="${MESHAGENT_TWILIO_SERVICE_ACCOUNT_NAME:-python-twilio-channel}"
  MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL="${MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL:-}"
  MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID="${MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID:-}"
  MESHAGENT_TWILIO_SKIP_SECRET="${MESHAGENT_TWILIO_SKIP_SECRET:-}"
  MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS="${MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS:-}"
  MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX="${MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX:-.threads/twilio-media}"
  MESHAGENT_TWILIO_INBOUND_MEDIA_MAX_BYTES="${MESHAGENT_TWILIO_INBOUND_MEDIA_MAX_BYTES:-25000000}"
}
load_twilio_env
find_meshagent_cli() {
  if [ -n "${MESHAGENT_CLI:-}" ]; then
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
  if command -v meshagent >/dev/null 2>&1; then
    MESHAGENT_CLI="$(command -v meshagent)"
    return
  fi
  MESHAGENT_CLI="meshagent"
}
find_meshagent_cli
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-twilio-channel:dev}"
if [ -z "$TWILIO_ACCOUNT_SID" ]; then
  echo "Missing TWILIO_ACCOUNT_SID. Set it in .env, .env-twilio, or the shell before deploy." >&2
  exit 1
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

if [ "${MESHAGENT_TWILIO_DEPLOY_IN_ROOM:-}" != "1" ] && [ -z "${MESHAGENT_ROOM:-}" ]; then
  if [ -n "$ROOM_NAME" ] && [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_TWILIO_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --project-id "$PROJECT_ID" --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$ROOM_NAME" ]; then
    exec env MESHAGENT_TWILIO_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_TWILIO_DEPLOY_IN_ROOM=1 \
      "$MESHAGENT_CLI" room connect --project-id "$PROJECT_ID" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  exec env MESHAGENT_TWILIO_DEPLOY_IN_ROOM=1 \
    "$MESHAGENT_CLI" room connect -- "$ROOT/scripts/deploy.sh" "$@"
fi

ROOM_NAME="${MESHAGENT_ROOM:-$ROOM_NAME}"
PROJECT_ID="${MESHAGENT_PROJECT_ID:-$PROJECT_ID}"
if [ "${MESHAGENT_TWILIO_DEPLOY_IN_ROOM:-}" = "1" ]; then
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

json_secret_id_by_name() {
  secret_name="$1"
  python3 -c 'import json, sys
name = sys.argv[1]
data = json.load(sys.stdin)
for secret in data.get("secrets", []):
    if secret.get("name") == name and secret.get("id"):
        print(secret["id"])
        break
' "$secret_name"
}

json_secret_id() {
  python3 -c 'import json, sys; data=json.load(sys.stdin); print(data.get("id") or "")'
}

resolve_twilio_service_account() {
  if [ -n "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL" ]; then
    printf '%s' "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL"
    return
  fi

  TWILIO_SERVICE_ACCOUNT_NAME_VALUE="$MESHAGENT_TWILIO_SERVICE_ACCOUNT_NAME" \
  TWILIO_PROJECT_ID_VALUE="$PROJECT_ID" \
  python3 - <<'PY'
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
    name = os.environ["TWILIO_SERVICE_ACCOUNT_NAME_VALUE"].strip()
    project_arg = os.environ.get("TWILIO_PROJECT_ID_VALUE") or None
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
            display_name="Python Twilio Channel",
            description="Runs the Python Twilio channel service.",
            metadata=None,
            annotations=None,
        )
        print(_run_as_value(created, name))
    finally:
        await client.close()


try:
    asyncio.run(main())
except Exception as exc:
    print(f"Could not resolve service account {os.environ['TWILIO_SERVICE_ACCOUNT_NAME_VALUE']}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

find_service_account_secret_id() {
  secret_name="$1"
  secret_json="$(
    meshagent_with_project secret search \
      --subject "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL" \
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
  secret_file="$(mktemp "${TMPDIR:-/tmp}/meshagent-twilio-secret.XXXXXX")"
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
        --subject "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL" \
        --type opaque \
        --http-only \
        --value-file "$secret_file" \
        -o json
    )"
    secret_id="$(printf '%s' "$secret_json" | json_secret_id)"
  else
    meshagent_with_project secret add-version \
      --subject "$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL" \
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

MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL="$(resolve_twilio_service_account)"
if [ -n "$TWILIO_AUTH_TOKEN" ]; then
  MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID="$(
    upsert_service_account_secret twilio-auth-token "$TWILIO_AUTH_TOKEN"
  )"
elif [ -z "$MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID" ]; then
  MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID="$(find_service_account_secret_id twilio-auth-token)"
fi
if [ -z "$MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID" ]; then
  echo "Missing twilio-auth-token service account secret." >&2
  echo "Set TWILIO_AUTH_TOKEN once, or set MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID." >&2
  exit 1
fi
unset TWILIO_AUTH_TOKEN
if [ "$ROOM_ARG_PRESENT" = "true" ]; then
  set -- "$@" --tag "$IMAGE_TAG" --meshagent-token agentDefault
else
  set -- "$@" --room "$ROOM_NAME" --tag "$IMAGE_TAG" --meshagent-token agentDefault
fi
set -- "$@" --set "twilio_service_account_email=$MESHAGENT_TWILIO_SERVICE_ACCOUNT_EMAIL"
set -- "$@" --set "twilio_auth_token_secret_id=$MESHAGENT_TWILIO_AUTH_TOKEN_SECRET_ID"
set -- "$@" --set "twilio_account_sid=$TWILIO_ACCOUNT_SID"
set -- "$@" --set "twilio_media_storage_prefix=$MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX"
set -- "$@" --set "twilio_inbound_media_max_bytes=$MESHAGENT_TWILIO_INBOUND_MEDIA_MAX_BYTES"
if [ -n "$MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS" ]; then
  set -- "$@" --set "twilio_allowed_from_numbers=$MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS"
fi
exec "$MESHAGENT_CLI" deploy . "$@" --wait
