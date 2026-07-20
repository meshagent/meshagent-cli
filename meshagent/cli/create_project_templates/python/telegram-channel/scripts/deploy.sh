#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
load_telegram_env() {
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
  TELEGRAM_API_ID="${TELEGRAM_API_ID:-}"
  TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-}"
  TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_NAME="${MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_NAME:-python-telegram-channel}"
  MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL="${MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL:-}"
  MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID="${MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID:-}"
  MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID="${MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID:-}"
  MESHAGENT_TELEGRAM_WEBHOOK_SECRET="${MESHAGENT_TELEGRAM_WEBHOOK_SECRET:-}"
  MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN="${MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN:-}"
  MESHAGENT_TELEGRAM_WEBHOOK_URL="${MESHAGENT_TELEGRAM_WEBHOOK_URL:-}"
  MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX="${MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX:-}"
  MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES="${MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES:-}"
  MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS="${MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS:-}"
}
load_telegram_env
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-telegram-channel:dev}"
if [ "${MESHAGENT_TELEGRAM_AUTO_CONFIGURE:-1}" = "1" ] && [ "${MESHAGENT_TELEGRAM_SKIP_CONFIGURE:-}" != "1" ]; then
  if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    if [ -t 0 ] && [ -t 1 ]; then
      ./scripts/configure-telegram.sh
      load_telegram_env
    else
      echo "Missing TELEGRAM_BOT_TOKEN. Run ./scripts/configure-telegram.sh or set TELEGRAM_BOT_TOKEN before deploy." >&2
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

if [ "${MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM:-}" != "1" ] && [ -z "${MESHAGENT_ROOM:-}" ]; then
  if [ -n "$ROOM_NAME" ] && [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM=1 \
      meshagent room connect --project-id "$PROJECT_ID" --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$ROOM_NAME" ]; then
    exec env MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM=1 \
      meshagent room connect --room "$ROOM_NAME" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  if [ -n "$PROJECT_ID" ]; then
    exec env MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM=1 \
      meshagent room connect --project-id "$PROJECT_ID" -- \
      "$ROOT/scripts/deploy.sh" "$@"
  fi
  exec env MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM=1 \
    meshagent room connect -- "$ROOT/scripts/deploy.sh" "$@"
fi

ROOM_NAME="${MESHAGENT_ROOM:-$ROOM_NAME}"
PROJECT_ID="${MESHAGENT_PROJECT_ID:-$PROJECT_ID}"
if [ "${MESHAGENT_TELEGRAM_DEPLOY_IN_ROOM:-}" = "1" ]; then
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

telegram_sdk_root_has_packages() {
  candidate="$1"
  [ -d "$candidate/meshagent-api" ] && [ -d "$candidate/meshagent-tools" ] && [ -d "$candidate/meshagent-agents" ] && [ -d "$candidate/meshagent-openai" ] && [ -d "$candidate/meshagent-telegram" ]
}

resolve_telegram_sdk_root() {
  if [ -n "${MESHAGENT_SDK_ROOT:-}" ]; then
    if telegram_sdk_root_has_packages "$MESHAGENT_SDK_ROOT"; then
      (cd "$MESHAGENT_SDK_ROOT" && pwd)
      return
    fi
    echo "MESHAGENT_SDK_ROOT does not point to a MeshAgent SDK checkout with the Telegram dependencies." >&2
    exit 1
  fi

  search_dir="$ROOT"
  while [ "$search_dir" != "/" ]; do
    for candidate in "$search_dir/meshagent-sdk" "$search_dir/meshagent-server/meshagent-sdk" "$search_dir/../meshagent-server/meshagent-sdk"; do
      if telegram_sdk_root_has_packages "$candidate"; then
        (cd "$candidate" && pwd)
        return
      fi
    done
    search_dir="$(dirname "$search_dir")"
  done
  printf ''
}

prepare_telegram_deploy_app() {
  if [ "${MESHAGENT_TELEGRAM_SKIP_VENDOR:-}" = "1" ]; then
    return
  fi
  python_bin="${PYTHON:-python3.13}"
  deploy_app_dir="$ROOT/.meshagent/deploy-app"
  wheel_dir="$ROOT/.meshagent/deploy-wheels"
  vendor_python_version="${MESHAGENT_TELEGRAM_VENDOR_PYTHON_VERSION:-3.13}"
  vendor_implementation="${MESHAGENT_TELEGRAM_VENDOR_IMPLEMENTATION:-cp}"
  vendor_abi="${MESHAGENT_TELEGRAM_VENDOR_ABI:-cp313}"
  vendor_platform="${MESHAGENT_TELEGRAM_VENDOR_PLATFORM:-manylinux_2_28_x86_64}"
  vendor_platform_compat="${MESHAGENT_TELEGRAM_VENDOR_PLATFORM_COMPAT:-manylinux2014_x86_64}"
  sdk_root="$(resolve_telegram_sdk_root)"
  echo "Preparing Telegram deploy package..."
  rm -rf "$deploy_app_dir" "$wheel_dir"
  mkdir -p "$deploy_app_dir" "$wheel_dir"
  "$python_bin" -m pip wheel --disable-pip-version-check --no-deps --wheel-dir "$wheel_dir" pyaes
  if [ -n "$sdk_root" ]; then
    "$python_bin" -m pip wheel --disable-pip-version-check --no-deps --wheel-dir "$wheel_dir" \
      "$sdk_root/meshagent-api" \
      "$sdk_root/meshagent-tools" \
      "$sdk_root/meshagent-agents" \
      "$sdk_root/meshagent-openai" \
      "$sdk_root/meshagent-telegram" \
      "$ROOT"
  else
    "$python_bin" -m pip wheel --disable-pip-version-check --no-deps --wheel-dir "$wheel_dir" "$ROOT"
  fi
  "$python_bin" -m pip install --disable-pip-version-check --no-warn-conflicts \
    --target "$deploy_app_dir" \
    --platform "$vendor_platform" \
    --platform "$vendor_platform_compat" \
    --implementation "$vendor_implementation" \
    --python-version "$vendor_python_version" \
    --abi "$vendor_abi" \
    --only-binary=:all: \
    --find-links "$wheel_dir" \
    --upgrade \
    'meshagent-create-python-telegram-channel==0.1.0'
  rm -rf "$wheel_dir"
}

json_service_account_email() {
  python3 -c 'import json, sys; data=json.load(sys.stdin); print(data.get("email") or "")'
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

route_domain_from_url() {
  if [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_URL" ]; then
    printf ''
    return
  fi
  TELEGRAM_WEBHOOK_URL_VALUE="$MESHAGENT_TELEGRAM_WEBHOOK_URL" python3 - <<'PY'
import os
import urllib.parse

url = os.environ["TELEGRAM_WEBHOOK_URL_VALUE"].strip()
parsed = urllib.parse.urlparse(url)
print(parsed.netloc or "")
PY
}

route_subdomain_from_room() {
  TELEGRAM_ROOM_NAME_VALUE="$ROOM_NAME" python3 - <<'PY'
import os
import re

room_name = os.environ["TELEGRAM_ROOM_NAME_VALUE"].strip().lower()
subdomain = re.sub(r"[^a-z0-9-]+", "-", room_name)
subdomain = re.sub(r"-+", "-", subdomain).strip("-")
print(subdomain or "python-telegram-channel")
PY
}

configured_pages_domain() {
  pages_domain="$(
    meshagent config get domains.pages 2>/dev/null || true
  )"
  printf '%s' "$pages_domain" | python3 -c 'import sys; print(sys.stdin.read().strip().lower().removeprefix("."))'
}

resolve_telegram_webhook_domain() {
  if [ -n "$MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN" ]; then
    printf '%s' "$MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN"
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

resolve_telegram_service_account() {
  if [ -n "$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL" ]; then
    printf '%s' "$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL"
    return
  fi

  TELEGRAM_SERVICE_ACCOUNT_NAME_VALUE="$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_NAME" \
  TELEGRAM_PROJECT_ID_VALUE="$PROJECT_ID" \
  python3 - <<'PY'
import asyncio
import os
import sys

from meshagent.cli.helper import get_client, resolve_project_id


def _items(page):
    if hasattr(page, "service_accounts"):
        return list(page.service_accounts)
    if isinstance(page, dict):
        return list(page.get("service_accounts") or [])
    return []


def _value(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _matches(item, name):
    candidates = {
        _value(item, "id"),
        _value(item, "key"),
        _value(item, "name"),
        _value(item, "email"),
        _value(item, "display_name"),
    }
    return name in candidates


def _run_as_value(item, fallback):
    value = _value(item, "email") or _value(item, "name") or fallback
    return str(value).strip()


async def main():
    name = os.environ["TELEGRAM_SERVICE_ACCOUNT_NAME_VALUE"].strip()
    project_arg = os.environ.get("TELEGRAM_PROJECT_ID_VALUE") or None
    project_id = await resolve_project_id(project_id=project_arg)
    client = await get_client()
    try:
        for filter_value in (name, ""):
            page = await client.list_service_accounts(
                project_id=project_id,
                page_size=100,
                filter=filter_value or None,
            )
            for item in _items(page):
                if _matches(item, name):
                    print(_run_as_value(item, name))
                    return

        created = await client.create_service_account(
            project_id=project_id,
            name=name,
            display_name="Python Telegram Channel",
            description="Runs the Python Telegram channel service.",
            metadata=None,
            annotations=None,
        )
        print(_run_as_value(created, name))
    finally:
        await client.close()


try:
    asyncio.run(main())
except Exception as exc:
    print(f"Could not resolve service account {os.environ['TELEGRAM_SERVICE_ACCOUNT_NAME_VALUE']}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

find_service_account_secret_id() {
  secret_name="$1"
  secret_json="$(
    meshagent_with_project secret search \
      --subject "$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL" \
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
  secret_file="$(mktemp "${TMPDIR:-/tmp}/meshagent-telegram-secret.XXXXXX")"
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
        --subject "$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL" \
        --type opaque \
        --http-only \
        --value-file "$secret_file" \
        -o json
    )"
    secret_id="$(printf '%s' "$secret_json" | json_secret_id)"
  else
    meshagent_with_project secret add-version \
      --subject "$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL" \
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

derive_webhook_secret() {
  if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
    TELEGRAM_BOT_TOKEN_VALUE="$TELEGRAM_BOT_TOKEN" python3 -c 'import hashlib, os; print(hashlib.sha256(os.environ["TELEGRAM_BOT_TOKEN_VALUE"].encode("utf-8")).hexdigest())'
  else
    printf ''
  fi
}

configure_telegram_webhook() {
  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_URL" ] || [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET" ]; then
    return
  fi
  TELEGRAM_BOT_TOKEN_VALUE="$TELEGRAM_BOT_TOKEN" \
  TELEGRAM_WEBHOOK_URL_VALUE="$MESHAGENT_TELEGRAM_WEBHOOK_URL" \
  TELEGRAM_WEBHOOK_SECRET_VALUE="$MESHAGENT_TELEGRAM_WEBHOOK_SECRET" \
  python3 - <<'PY'
import asyncio
import json
import os

from aiohttp import ClientTimeout

from meshagent.api.http import new_client_session


async def main():
    token = os.environ["TELEGRAM_BOT_TOKEN_VALUE"]
    webhook_url = os.environ["TELEGRAM_WEBHOOK_URL_VALUE"]
    secret = os.environ["TELEGRAM_WEBHOOK_SECRET_VALUE"]
    body = {
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": json.dumps(["message", "edited_message"]),
    }
    timeout = ClientTimeout(total=30)
    async with new_client_session(timeout=timeout) as session:
        async with session.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            data=body,
        ) as response:
            text = await response.text()
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"Telegram setWebhook returned HTTP {response.status} with a non-JSON body."
                ) from exc
    if not payload.get("ok"):
        raise SystemExit(f"Telegram setWebhook failed: {payload}")
    print("Telegram webhook configured.")


asyncio.run(main())
PY
}

if [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET" ]; then
  MESHAGENT_TELEGRAM_WEBHOOK_SECRET="$(derive_webhook_secret)"
fi
MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN="$(resolve_telegram_webhook_domain)"
if [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN" ]; then
  echo "Missing Telegram webhook domain." >&2
  echo "Set MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN, set MESHAGENT_TELEGRAM_WEBHOOK_URL, or configure a MeshAgent pages domain." >&2
  exit 1
fi
if [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_URL" ]; then
  MESHAGENT_TELEGRAM_WEBHOOK_URL="https://$MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN/telegram/webhook"
fi
MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL="$(resolve_telegram_service_account)"
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID="$(
    upsert_service_account_secret telegram-bot-token "$TELEGRAM_BOT_TOKEN"
  )"
elif [ -z "$MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID" ]; then
  MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID="$(find_service_account_secret_id telegram-bot-token)"
fi
if [ -n "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET" ]; then
  MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID="$(
    upsert_service_account_secret telegram-webhook-secret "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET"
  )"
elif [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID" ]; then
  MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID="$(find_service_account_secret_id telegram-webhook-secret)"
fi
if [ -z "$MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID" ]; then
  echo "Missing telegram-bot-token service account secret." >&2
  echo "Set TELEGRAM_BOT_TOKEN once, or set MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID." >&2
  exit 1
fi
if [ -z "$MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID" ]; then
  echo "Missing telegram-webhook-secret service account secret." >&2
  echo "Set TELEGRAM_BOT_TOKEN once to derive it, or set MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID." >&2
  exit 1
fi
unset TELEGRAM_API_HASH
if [ "$ROOM_ARG_PRESENT" = "true" ]; then
  set -- "$@" --tag "$IMAGE_TAG" --meshagent-token agentDefault
else
  set -- "$@" --room "$ROOM_NAME" --tag "$IMAGE_TAG" --meshagent-token agentDefault
fi
set -- "$@" --set "telegram_service_account_email=$MESHAGENT_TELEGRAM_SERVICE_ACCOUNT_EMAIL"
set -- "$@" --set "telegram_bot_token_secret_id=$MESHAGENT_TELEGRAM_BOT_TOKEN_SECRET_ID"
set -- "$@" --set "telegram_webhook_secret_id=$MESHAGENT_TELEGRAM_WEBHOOK_SECRET_ID"
set -- "$@" --set "domain=$MESHAGENT_TELEGRAM_WEBHOOK_DOMAIN"
if [ -n "$MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX" ]; then
  set -- "$@" --set "telegram_media_storage_prefix=$MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX"
fi
if [ -n "$MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES" ]; then
  set -- "$@" --set "telegram_inbound_media_max_bytes=$MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES"
fi
if [ -n "$MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS" ]; then
  set -- "$@" --set "telegram_allowed_chat_ids=$MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS"
fi
if [ "${MESHAGENT_TELEGRAM_DEPLOY_PREBUILT:-}" = "1" ]; then
  meshagent deploy "$@" --wait
else
  prepare_telegram_deploy_app
  meshagent deploy . "$@" --wait
fi
configure_telegram_webhook
unset TELEGRAM_BOT_TOKEN
unset MESHAGENT_TELEGRAM_WEBHOOK_SECRET
