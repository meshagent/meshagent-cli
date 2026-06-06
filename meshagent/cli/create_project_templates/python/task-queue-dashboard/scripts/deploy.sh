#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-task-queue-dashboard:dev}"
exec meshagent deploy . \
  "$@" \
  --tag "$IMAGE_TAG" \
  --public \
  --liveness /health \
  --meshagent-token agentDefault \
  --wait
