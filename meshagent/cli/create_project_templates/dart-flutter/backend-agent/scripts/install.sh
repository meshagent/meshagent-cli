#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if command -v dart >/dev/null 2>&1; then
  dart pub get
else
  echo "The Dart SDK is required on the host. Install dart, then rerun this script." >&2
  exit 127
fi
