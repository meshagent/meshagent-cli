#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if command -v flutter >/dev/null 2>&1; then
  flutter pub get
else
  echo "The Flutter SDK is required on the host. Install flutter, then rerun this script." >&2
  exit 127
fi
