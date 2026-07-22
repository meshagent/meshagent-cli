#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3.13}"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
SETUP_LOG="${MESHAGENT_TELEGRAM_SETUP_LOG:-.meshagent/telegram-setup-install.log}"
fail_bootstrap() {
  echo "Failed to prepare Telegram setup UI." >&2
  echo "Install log: $ROOT/$SETUP_LOG" >&2
  echo "" >&2
  echo "Last 20 log lines:" >&2
  tail -n 20 "$SETUP_LOG" >&2 || true
  exit 1
}
bootstrap_tui() {
  mkdir -p "$(dirname "$SETUP_LOG")"
  if [ ! -x "$VENV_PYTHON" ]; then
    echo "Preparing Telegram setup UI..."
    "$PYTHON" -m venv "$VENV" >>"$SETUP_LOG" 2>&1 || fail_bootstrap
  fi
  if ! "$VENV_PYTHON" -c 'import textual' >/dev/null 2>&1; then
    echo "Preparing Telegram setup UI..."
    "$VENV_PYTHON" -m pip install --disable-pip-version-check 'textual>=8.2.3,<9.0' >>"$SETUP_LOG" 2>&1 || fail_bootstrap
  fi
}
bootstrap_tui
exec "$VENV_PYTHON" scripts/configure-telegram.py "$@"
