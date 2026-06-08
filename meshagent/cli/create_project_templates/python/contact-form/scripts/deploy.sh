#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-create-python-contact-form:dev}"
CONTACT_FORM_FROM="${CONTACT_FORM_FROM:-}"
CONTACT_FORM_TO="${CONTACT_FORM_TO:-you@example.com}"
CONTACT_FORM_DELIVERY_TO="${CONTACT_FORM_DELIVERY_TO:-}"
set -- "$@" --tag "$IMAGE_TAG" --meshagent-token agentDefault
if [ -n "$CONTACT_FORM_FROM" ]; then
  set -- "$@" --set "from_email=$CONTACT_FORM_FROM"
fi
set -- "$@" --set "to_email=$CONTACT_FORM_TO"
if [ -n "$CONTACT_FORM_DELIVERY_TO" ]; then
  set -- "$@" --set "delivery_email=$CONTACT_FORM_DELIVERY_TO"
fi
exec meshagent deploy . "$@" --wait
