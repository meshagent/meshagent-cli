#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
SETUP_LOG="${MESHAGENT_TELEGRAM_SETUP_LOG:-.meshagent/telegram-setup-install.log}"
fail_bootstrap() {
  echo "Failed to prepare Telegram BotFather helper." >&2
  echo "Install log: $ROOT/$SETUP_LOG" >&2
  echo "" >&2
  echo "Last 20 log lines:" >&2
  tail -n 20 "$SETUP_LOG" >&2 || true
  exit 1
}
if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c 'import telethon' >/dev/null 2>&1; then
  mkdir -p "$(dirname "$SETUP_LOG")"
  echo "Preparing Telegram BotFather helper..."
  ./scripts/install.sh >>"$SETUP_LOG" 2>&1 || fail_bootstrap
fi
exec "$VENV_PYTHON" scripts/create-bot-token.py "$@"
