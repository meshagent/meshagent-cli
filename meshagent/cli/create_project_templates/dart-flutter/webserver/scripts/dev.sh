#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if [ -n "${MESHAGENT_CREATE_DEV_PROBE:-}" ] && [ -n "${MESHAGENT_CREATE_DEV_READY_PATH:-}" ]; then
  if command -v dart >/dev/null 2>&1; then
    meshagent room connect -- dart run tool/dev_room_proof.dart
  else
    echo "The Dart SDK is required on the host for the Flutter dev proof. Install dart, then rerun this script." >&2
    exit 127
  fi
elif command -v flutter >/dev/null 2>&1 && command -v dart >/dev/null 2>&1; then
  meshagent room connect -- sh -c 'dart run tool/dev_room_proof.dart & flutter run -d web-server --web-hostname 0.0.0.0 --web-port 3000'
else
  echo "The Flutter SDK and Dart SDK are required on the host. Install flutter and dart, then rerun this script." >&2
  exit 127
fi
