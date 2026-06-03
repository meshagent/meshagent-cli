#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "Missing $VENV_PYTHON. Run the install script first." >&2
  exit 1
fi
CONTACT_FORM_TO="${CONTACT_FORM_TO:-you@example.com}"
CONTACT_FORM_DELIVERY_TO="${CONTACT_FORM_DELIVERY_TO:-}"
PORT="${PORT:-8000}"
SMTP_HOSTNAME="${SMTP_HOSTNAME:-${MESHAGENT_MAIL_DOMAIN:-mail.meshagent.com}}"
SMTP_PORT="${SMTP_PORT:-587}"
SMTP_USERNAME="${SMTP_USERNAME:-meshagent-create-python-contact-form}"
mailbox_from_room() {
  room_slug="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-+/-/g' | cut -c 1-56)"
  if [ -z "$room_slug" ]; then
    room_slug="room"
  fi
  printf 'contact-%s@mail.meshagent.com\n' "$room_slug"
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
    CONTACT_FORM_FROM="contact@mail.meshagent.com"
  fi
fi
export CONTACT_FORM_FROM CONTACT_FORM_TO CONTACT_FORM_DELIVERY_TO PORT SMTP_HOSTNAME SMTP_PORT SMTP_USERNAME
if [ -n "$ROOM_NAME" ]; then
  meshagent rooms create "$ROOM_NAME" --if-not-exists
fi
LOCAL_URL="http://127.0.0.1:$PORT/"
launch_browser_when_ready() {
  (
    attempt=0
    while [ "$attempt" -lt 50 ]; do
      if "$VENV_PYTHON" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=0.4).close()' "$LOCAL_URL" >/dev/null 2>&1; then
        "$VENV_PYTHON" -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' "$LOCAL_URL" >/dev/null 2>&1 || true
        exit 0
      fi
      attempt=$((attempt + 1))
      sleep 0.2
    done
    "$VENV_PYTHON" -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' "$LOCAL_URL" >/dev/null 2>&1 || true
  ) &
}
if [ "${CONTACT_FORM_OPEN_BROWSER:-1}" != "0" ] && [ -t 0 ]; then
  launch_browser_when_ready
  echo "Browser will launch at $LOCAL_URL"
fi
set +e
meshagent room connect "$@" -- "$VENV_PYTHON" -u server.py
status=$?
set -e
if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then
  echo "Stopped contact form."
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "If the room does not exist yet, create it first:" >&2
  echo "  meshagent rooms create <room> --if-not-exists" >&2
  echo "Then run:" >&2
  echo "  ./scripts/dev.sh --room <room>" >&2
  exit "$status"
fi
