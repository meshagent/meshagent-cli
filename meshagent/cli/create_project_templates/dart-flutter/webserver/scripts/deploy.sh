#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-flutter:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --room-mount /:/data:rw --wait
