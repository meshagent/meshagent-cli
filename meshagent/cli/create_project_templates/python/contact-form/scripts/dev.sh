#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import aiohttp' >/dev/null 2>&1; then
  ./scripts/install.sh
fi
CONTACT_FORM_FROM="${CONTACT_FORM_FROM:-}"
CONTACT_FORM_TO="${CONTACT_FORM_TO:-you@example.com}"
CONTACT_FORM_DELIVERY_TO="${CONTACT_FORM_DELIVERY_TO:-}"
PORT="${PORT:-8000}"
SMTP_HOSTNAME="${SMTP_HOSTNAME:-${MESHAGENT_MAIL_DOMAIN:-mail.meshagent.com}}"
SMTP_PORT="${SMTP_PORT:-587}"
SMTP_USERNAME="${SMTP_USERNAME:-meshagent-create-python-contact-form}"
LOCAL_URL="http://127.0.0.1:$PORT/"
CONTACT_FORM_LOCAL_URL="$LOCAL_URL"
CONTACT_FORM_OPEN_BROWSER="${CONTACT_FORM_OPEN_BROWSER:-1}"
export CONTACT_FORM_FROM CONTACT_FORM_TO CONTACT_FORM_DELIVERY_TO PORT SMTP_HOSTNAME SMTP_PORT SMTP_USERNAME CONTACT_FORM_LOCAL_URL CONTACT_FORM_OPEN_BROWSER
echo "Pick a room, then the contact form will launch at $LOCAL_URL"
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
  echo "Run ./scripts/dev.sh to use the MeshAgent room picker." >&2
  exit "$status"
fi
