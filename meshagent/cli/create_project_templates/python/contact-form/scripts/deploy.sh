#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-contact-form:dev}"
CONTACT_FORM_FROM="${CONTACT_FORM_FROM:-}"
CONTACT_FORM_TO="${CONTACT_FORM_TO:-you@example.com}"
CONTACT_FORM_DELIVERY_TO="${CONTACT_FORM_DELIVERY_TO:-}"
exec meshagent deploy . \
  "$@" \
  --tag "$IMAGE_TAG" \
  --meshagent-token agentDefault \
  --set "from_email=$CONTACT_FORM_FROM" \
  --set "to_email=$CONTACT_FORM_TO" \
  --set "delivery_email=$CONTACT_FORM_DELIVERY_TO" \
  --wait
