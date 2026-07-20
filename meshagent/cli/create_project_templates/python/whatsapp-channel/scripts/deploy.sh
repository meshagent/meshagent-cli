#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_whatsapp_env() {
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
  WHATSAPP_ACCESS_TOKEN="${WHATSAPP_ACCESS_TOKEN:-}"
  WHATSAPP_PHONE_NUMBER_ID="${WHATSAPP_PHONE_NUMBER_ID:-}"
  WHATSAPP_APP_SECRET="${WHATSAPP_APP_SECRET:-}"
  WHATSAPP_VERIFY_TOKEN="${WHATSAPP_VERIFY_TOKEN:-}"
  MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_NAME="${MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_NAME:-python-whatsapp-channel}"
  MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL="${MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL:-}"
  MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID="${MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID:-}"
  MESHAGENT_WHATSAPP_APP_SECRET_ID="${MESHAGENT_WHATSAPP_APP_SECRET_ID:-}"
  MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID="${MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID:-}"
  MESHAGENT_WHATSAPP_SKIP_SECRETS="${MESHAGENT_WHATSAPP_SKIP_SECRETS:-}"
  MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS="${MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS:-}"
}
load_whatsapp_env
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-whatsapp-channel:dev}"
if [ -z "$WHATSAPP_PHONE_NUMBER_ID" ]; then
  echo "Missing WHATSAPP_PHONE_NUMBER_ID. Set it in .env or the shell before deploy." >&2
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

if [ "${MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM:-}" != "1" ] && [ -z "${MESHAGENT_ROOM:-}" ]; then
  if [ -n "$ROOM_NAME" ] && [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM=1 \
      meshagent room connect --project-id "$PROJECT_ID" --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$ROOM_NAME" ]; then
    exec env MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM=1 \
      meshagent room connect --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM=1 \
      meshagent room connect --project-id "$PROJECT_ID" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  exec env MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM=1 \
    meshagent room connect -- "$ROOT/scripts/deploy.sh" "$@"
fi

ROOM_NAME="${MESHAGENT_ROOM:-$ROOM_NAME}"
PROJECT_ID="${MESHAGENT_PROJECT_ID:-$PROJECT_ID}"
if [ "${MESHAGENT_WHATSAPP_DEPLOY_IN_ROOM:-}" = "1" ]; then
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
    meshagent "$@" --project-id "$PROJECT_ID"
  else
    meshagent "$@"
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

resolve_whatsapp_service_account() {
  if [ -n "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL" ]; then
    printf '%s' "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL"
    return
  fi

  WHATSAPP_SERVICE_ACCOUNT_NAME_VALUE="$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_NAME" \
  WHATSAPP_PROJECT_ID_VALUE="$PROJECT_ID" \
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
    name = os.environ["WHATSAPP_SERVICE_ACCOUNT_NAME_VALUE"].strip()
    project_arg = os.environ.get("WHATSAPP_PROJECT_ID_VALUE") or None
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
            display_name="Python WhatsApp Channel",
            description="Runs the Python WhatsApp channel service.",
            metadata=None,
            annotations=None,
        )
        print(_run_as_value(created, name))
    finally:
        await client.close()


try:
    asyncio.run(main())
except Exception as exc:
    print(f"Could not resolve service account {os.environ['WHATSAPP_SERVICE_ACCOUNT_NAME_VALUE']}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

find_service_account_secret_id() {
  secret_name="$1"
  secret_json="$(
    meshagent_with_project secret search \
      --subject "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL" \
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
  secret_file="$(mktemp "${TMPDIR:-/tmp}/meshagent-whatsapp-secret.XXXXXX")"
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
        --subject "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL" \
        --type opaque \
        --http-only \
        --value-file "$secret_file" \
        -o json
    )"
    secret_id="$(printf '%s' "$secret_json" | json_secret_id)"
  else
    meshagent_with_project secret add-version \
      --subject "$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL" \
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

MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL="$(resolve_whatsapp_service_account)"
if [ -n "$WHATSAPP_ACCESS_TOKEN" ]; then
  MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID="$(
    upsert_service_account_secret whatsapp-access-token "$WHATSAPP_ACCESS_TOKEN"
  )"
elif [ -z "$MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID" ]; then
  MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID="$(find_service_account_secret_id whatsapp-access-token)"
fi
if [ -n "$WHATSAPP_APP_SECRET" ]; then
  MESHAGENT_WHATSAPP_APP_SECRET_ID="$(
    upsert_service_account_secret whatsapp-app-secret "$WHATSAPP_APP_SECRET"
  )"
elif [ -z "$MESHAGENT_WHATSAPP_APP_SECRET_ID" ]; then
  MESHAGENT_WHATSAPP_APP_SECRET_ID="$(find_service_account_secret_id whatsapp-app-secret)"
fi
if [ -n "$WHATSAPP_VERIFY_TOKEN" ]; then
  MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID="$(
    upsert_service_account_secret whatsapp-verify-token "$WHATSAPP_VERIFY_TOKEN"
  )"
elif [ -z "$MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID" ]; then
  MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID="$(find_service_account_secret_id whatsapp-verify-token)"
fi
if [ -z "$MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID" ]; then
  echo "Missing whatsapp-access-token service account secret." >&2
  echo "Set WHATSAPP_ACCESS_TOKEN once, or set MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID." >&2
  exit 1
fi
if [ -z "$MESHAGENT_WHATSAPP_APP_SECRET_ID" ]; then
  echo "Missing whatsapp-app-secret service account secret." >&2
  echo "Set WHATSAPP_APP_SECRET once, or set MESHAGENT_WHATSAPP_APP_SECRET_ID." >&2
  exit 1
fi
if [ -z "$MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID" ]; then
  echo "Missing whatsapp-verify-token service account secret." >&2
  echo "Set WHATSAPP_VERIFY_TOKEN once, or set MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID." >&2
  exit 1
fi
unset WHATSAPP_ACCESS_TOKEN
unset WHATSAPP_APP_SECRET
unset WHATSAPP_VERIFY_TOKEN
if [ "$ROOM_ARG_PRESENT" = "true" ]; then
  set -- "$@" --tag "$IMAGE_TAG" --meshagent-token agentDefault
else
  set -- "$@" --room "$ROOM_NAME" --tag "$IMAGE_TAG" --meshagent-token agentDefault
fi
set -- "$@" --set "whatsapp_service_account_email=$MESHAGENT_WHATSAPP_SERVICE_ACCOUNT_EMAIL"
set -- "$@" --set "whatsapp_access_token_secret_id=$MESHAGENT_WHATSAPP_ACCESS_TOKEN_SECRET_ID"
set -- "$@" --set "whatsapp_app_secret_id=$MESHAGENT_WHATSAPP_APP_SECRET_ID"
set -- "$@" --set "whatsapp_verify_token_secret_id=$MESHAGENT_WHATSAPP_VERIFY_TOKEN_SECRET_ID"
set -- "$@" --set "whatsapp_phone_number_id=$WHATSAPP_PHONE_NUMBER_ID"
if [ -n "$MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS" ]; then
  set -- "$@" --set "whatsapp_allowed_from_numbers=$MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS"
fi
exec meshagent deploy . "$@" --wait
