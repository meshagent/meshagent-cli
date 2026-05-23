from __future__ import annotations

import os
from typing import Any

from aiohttp import ClientTimeout
from rich.console import Console
from rich.markup import escape

from meshagent.api.http import new_client_session
from meshagent.cli.async_typer import _run_coroutine_sync
from meshagent.cli.local_settings import resolve_api_url
from meshagent.cli.version import __version__ as MESHAGENT_CLI_VERSION

_VERSION_CHECK_TIMEOUT_SECONDS = 1.5
_version_check_done = False


def _version_key(value: str) -> tuple[int, int, int] | None:
    parts = value.strip().split(".")
    if len(parts) < 2:
        return None

    parsed: list[int] = []
    for part in parts[:3]:
        digits = []
        for char in part:
            if not char.isdigit():
                break
            digits.append(char)
        if not digits:
            return None
        parsed.append(int("".join(digits)))

    while len(parsed) < 3:
        parsed.append(0)
    return tuple(parsed)


def _cli_is_behind_server(*, cli_version: str, server_version: str) -> bool:
    cli_key = _version_key(cli_version)
    server_key = _version_key(server_version)
    if cli_key is None or server_key is None:
        return False
    return cli_key < server_key


def _server_version_from_config_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if not isinstance(version, str):
        return None
    normalized = version.strip()
    if normalized == "":
        return None
    return normalized


async def _fetch_server_version() -> str | None:
    timeout = ClientTimeout(total=_VERSION_CHECK_TIMEOUT_SECONDS)
    async with new_client_session(timeout=timeout) as session:
        async with session.get(f"{resolve_api_url()}/config") as response:
            if response.status >= 400:
                return None
            return _server_version_from_config_payload(await response.json())


async def get_server_version_best_effort() -> str | None:
    try:
        return await _fetch_server_version()
    except Exception:
        return None


async def _maybe_warn_if_cli_out_of_date() -> None:
    server_version = await get_server_version_best_effort()
    if server_version is None:
        return
    if not _cli_is_behind_server(
        cli_version=MESHAGENT_CLI_VERSION,
        server_version=server_version,
    ):
        return

    Console(stderr=True, width=200).print(
        "[yellow]"
        "Warning: this meshagent CLI is older than the server "
        f"(CLI {escape(MESHAGENT_CLI_VERSION)}, server {escape(server_version)}). "
        "Update meshagent to get the latest compatible behavior."
        "[/yellow]"
    )


def warn_if_cli_out_of_date() -> None:
    global _version_check_done
    if _version_check_done:
        return
    _version_check_done = True

    if os.getenv("MESHAGENT_DISABLE_VERSION_CHECK") == "1":
        return
    if os.getenv("PYTEST_CURRENT_TEST") is not None:
        return

    _run_coroutine_sync(_maybe_warn_if_cli_out_of_date())
