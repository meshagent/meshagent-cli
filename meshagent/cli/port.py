from __future__ import annotations

import asyncio

from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url

from typing import Annotated, Optional

import typer
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id

from meshagent.api.port_forward import port_forward

app = async_typer.AsyncTyper(help="Port forwarding into room containers")


def _parse_port_mapping(value: str) -> tuple[int, int]:
    raw = value.strip()
    if raw == "":
        raise typer.BadParameter("--port cannot be empty")

    parts = [part.strip() for part in raw.split(":")]
    if len(parts) != 2:
        raise typer.BadParameter("Expected --port in LOCAL:REMOTE format")

    local_raw, remote_raw = parts
    if local_raw == "" or remote_raw == "":
        raise typer.BadParameter("Expected --port in LOCAL:REMOTE format")

    try:
        local_port = int(local_raw)
    except ValueError as exc:
        raise typer.BadParameter(
            f"LOCAL port must be an integer, got: {local_raw}"
        ) from exc

    try:
        remote_port = int(remote_raw)
    except ValueError as exc:
        raise typer.BadParameter(
            f"REMOTE port must be an integer, got: {remote_raw}"
        ) from exc

    if not (0 <= local_port <= 65535):
        raise typer.BadParameter("LOCAL port must be between 0 and 65535")
    if not (1 <= remote_port <= 65535):
        raise typer.BadParameter("REMOTE port must be between 1 and 65535")

    return local_port, remote_port


@app.async_command("forward", help="Forward a container port to localhost")
async def forward(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        str,
        typer.Option(
            "--room",
            "-r",
            help="Room name containing the target container",
        ),
    ],
    name: Annotated[
        Optional[str],
        typer.Option(
            "-n",
            "--name",
            help="Container name to port-forward into",
        ),
    ] = None,
    container_id: Annotated[
        Optional[str],
        typer.Option(
            "-c",
            "--container-id",
            help="Container ID to port-forward into",
        ),
    ] = None,
    port: Annotated[
        str,
        typer.Option(
            "--port",
            "-p",
            help="Port mapping in the form LOCAL:REMOTE",
        ),
    ],
):
    """Create a local TCP listener forwarding into a room container."""

    client = await get_client()
    try:
        local_port, remote_port = _parse_port_mapping(port)
        project_id = await resolve_project_id(project_id)

        connection = await client.connect_room(project_id=project_id, room=room)

        if name is not None:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
                    token=connection.jwt,
                )
            ) as r:
                containers = await r.containers.list()
                for container in containers:
                    if container.name == name:
                        container_id = container.id

        if container_id is None:
            if name is not None:
                print(f"[red]container not found {name}[/red]")
            else:
                print("[red]container not specified[/red]")

            raise typer.Exit(1)

        handler = await port_forward(
            listen_port=local_port,
            port=remote_port,
            container_id=container_id,
            token=connection.jwt,
        )

        await asyncio.sleep(10000)

        await handler.close()
    except typer.BadParameter as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    finally:
        await client.close()
