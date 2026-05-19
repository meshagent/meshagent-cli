#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-dart-agent:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait
