from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Sequence

import click
from rich import print

from meshagent.api.helpers import meshagent_base_url
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room


@dataclass(frozen=True)
class _ConnectedRoomEnv:
    api_url: str
    room_name: str
    room_url: str
    token: str


def _normalize_room_url(*, room_url: str) -> str:
    normalized = room_url.strip().rstrip("/")
    if normalized.startswith("wss:"):
        return "https:" + normalized.removeprefix("wss:")
    if normalized.startswith("ws:"):
        return "http:" + normalized.removeprefix("ws:")
    return normalized


async def _connect_room_env(
    *,
    project_id: str | None,
    room: str | None,
) -> _ConnectedRoomEnv:
    account_client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        resolved_room = resolve_room(room)
        if resolved_room is None:
            print("[red]--room is required (or set MESHAGENT_ROOM).[/red]")
            raise click.exceptions.Exit(1)

        connection = await account_client.connect_room(
            project_id=resolved_project_id,
            room=resolved_room,
        )
        return _ConnectedRoomEnv(
            api_url=os.getenv("MESHAGENT_API_URL") or meshagent_base_url(),
            room_name=connection.room_name,
            room_url=_normalize_room_url(room_url=connection.room_url),
            token=connection.jwt,
        )
    finally:
        await account_client.close()


def _run_connected_command(
    *,
    command: Sequence[str],
    room_env: _ConnectedRoomEnv,
) -> int:
    child_env = os.environ.copy()
    child_env["MESHAGENT_API_URL"] = room_env.api_url
    child_env["MESHAGENT_TOKEN"] = room_env.token
    child_env["MESHAGENT_ROOM"] = room_env.room_name
    child_env["OPENAI_BASE_URL"] = f"{room_env.room_url}/openai/v1"
    child_env["ANTHROPIC_BASE_URL"] = f"{room_env.room_url}/anthropic"
    child_env["OPENAI_API_KEY"] = room_env.token
    child_env["ANTHROPIC_API_KEY"] = room_env.token

    try:
        result = subprocess.run(
            list(command),
            check=False,
            env=child_env,
        )
    except OSError as exc:
        error_message = exc.strerror or str(exc)
        raise click.ClickException(
            f"Failed to start {command[0]}: {error_message}"
        ) from exc

    return result.returncode


@click.command(
    "connect",
    help=(
        "Connect to a room and run a local command with "
        "MESHAGENT_API_URL, MESHAGENT_TOKEN, and MESHAGENT_ROOM set. "
        "Use -- before the local command."
    ),
)
@click.option(
    "--project-id",
    help="A MeshAgent project id. If empty, the activated project will be used.",
)
@click.option("--room", help="Room name")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def connect_command(
    project_id: str | None,
    room: str | None,
    command: tuple[str, ...],
) -> None:
    if len(command) == 0:
        raise click.UsageError(
            "Pass the local command after --, for example: "
            "meshagent room connect -- python script.py"
        )

    room_env = async_typer._run_coroutine_sync(
        _connect_room_env(project_id=project_id, room=room)
    )
    raise click.exceptions.Exit(
        _run_connected_command(command=command, room_env=room_env)
    )
