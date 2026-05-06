from __future__ import annotations

import os
from urllib.parse import urlparse

from meshagent.cli.local_settings import (
    DEFAULT_API_URL,
    get_active_api_url,
    normalize_api_url,
)


PROD_MESHAGENT_IMAGE_PREFIX = "us-central1-docker.pkg.dev/meshagent-public/images/"
DEV_MESHAGENT_IMAGE_PREFIX = (
    "us-central1-docker.pkg.dev/meshagent-life/meshagent-public/"
)
MESHAGENT_IMAGE_PREFIX_TEMPLATE = "__MESHAGENT_IMAGE_PREFIX__"


def _with_trailing_slash(value: str) -> str:
    return value.rstrip("/") + "/"


def _api_url_uses_dev_images(api_url: str) -> bool:
    host = urlparse(api_url).hostname or ""
    return host == "meshagent.life" or host.endswith(".meshagent.life")


def _active_or_environment_api_url() -> str:
    active_api_url = get_active_api_url()
    if active_api_url is not None:
        return active_api_url

    env_api_url = normalize_api_url(os.getenv("MESHAGENT_API_URL"))
    if env_api_url is not None:
        return env_api_url

    return DEFAULT_API_URL


def meshagent_image_prefix() -> str:
    explicit_prefix = os.getenv("MESHAGENT_IMAGE_PREFIX")
    if explicit_prefix is not None and explicit_prefix.strip() != "":
        return _with_trailing_slash(explicit_prefix.strip())

    if _api_url_uses_dev_images(_active_or_environment_api_url()):
        return DEV_MESHAGENT_IMAGE_PREFIX

    return PROD_MESHAGENT_IMAGE_PREFIX


def render_meshagent_image_prefix_template(value: str) -> str:
    return value.replace(MESHAGENT_IMAGE_PREFIX_TEMPLATE, meshagent_image_prefix())
