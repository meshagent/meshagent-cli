#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-contact-form:dev}"
CONTACT_FORM_TO="${CONTACT_FORM_TO:-you@example.com}"
SMTP_USERNAME="${SMTP_USERNAME:-meshagent-create-python-contact-form}"
mailbox_from_room() {
  room_slug="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g' | cut -c 1-56)"
  if [ -z "$room_slug" ]; then
    room_slug="room"
  fi
  printf 'contact-%s@mail.meshagent.life\n' "$room_slug"
}
room_name_from_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --room=*)
        printf '%s\n' "${1#--room=}"
        return 0
        ;;
      --room)
        if [ "$#" -ge 2 ]; then
          shift
          printf '%s\n' "$1"
          return 0
        fi
        return 1
        ;;
    esac
    shift
  done
  if [ -n "${MESHAGENT_ROOM:-}" ]; then
    printf '%s\n' "$MESHAGENT_ROOM"
    return 0
  fi
  return 1
}
ROOM_NAME="$(room_name_from_args "$@" || true)"
if [ -z "${CONTACT_FORM_FROM:-}" ]; then
  if [ -n "$ROOM_NAME" ]; then
    CONTACT_FORM_FROM="$(mailbox_from_room "$ROOM_NAME")"
  else
    CONTACT_FORM_FROM="contact@mail.meshagent.life"
  fi
fi
if [ -n "$ROOM_NAME" ]; then
  meshagent rooms create --name "$ROOM_NAME" --if-not-exists
fi
if ! meshagent deploy . \
  "$@" \
  --tag "$IMAGE_TAG" \
  --public \
  --liveness /health \
  --meshagent-token agentDefault \
  --env "CONTACT_FORM_FROM=$CONTACT_FORM_FROM" \
  --env "CONTACT_FORM_TO=$CONTACT_FORM_TO" \
  --env "SMTP_USERNAME=$SMTP_USERNAME" \
  --wait; then
  echo "" >&2
  echo "If the room does not exist yet, create it first:" >&2
  echo "  meshagent rooms create --name <room> --if-not-exists" >&2
  echo "Then run:" >&2
  echo "  CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh --room <room>" >&2
  exit 1
fi
