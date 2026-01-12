from __future__ import annotations

import asyncio


from typing import Annotated

import typer

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id

from meshagent.api.port_forward import port_forward

app = async_typer.AsyncTyper(help="Port forwarding into room containers")


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
    container_id: Annotated[
        str,
        typer.Option(
            "--container-id",
            "-c",
            help="Container ID to port-forward into",
        ),
    ],
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
        project_id = await resolve_project_id(project_id)

        connection = await client.connect_room(project_id=project_id, room=room)

        ports = port.split(":")

        handler = await port_forward(
            listen_port=int(ports[0]),
            port=int(ports[1]),
            container_id=container_id,
            token=connection.jwt,
        )

        await asyncio.sleep(10000)

        await handler.close()

    finally:
        await client.close()
