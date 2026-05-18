import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
import json
import asyncio

from meshagent.api.helpers import websocket_room_url
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
    RoomException,
)
from meshagent.api.messaging import Content, FileContent
from meshagent.cli.helper import resolve_project_id
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_room

app = async_typer.LazyTyper(help="Interact with agents and toolkits in a room")
app.add_lazy_command(
    name="call",
    module="meshagent.cli.call",
    help="Trigger agent/tool calls in a room",
)


def _chunk_to_output(chunk: Content) -> dict:
    payload = chunk.to_json()
    if isinstance(chunk, FileContent):
        payload = {
            **payload,
            "size_bytes": len(chunk.data),
        }
    return payload


@app.async_command("invoke-tool", help="Invoke a specific tool from a toolkit")
async def invoke_tool(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    toolkit: Annotated[str, typer.Option(..., help="Toolkit name")],
    tool: Annotated[str, typer.Option(..., help="Tool name")],
    arguments: Annotated[
        str, typer.Option(..., help="JSON string with arguments for the tool")
    ],
    participant_id: Annotated[
        Optional[str],
        typer.Option(..., help="Optional participant ID to invoke the tool on"),
    ] = None,
    on_behalf_of_id: Annotated[
        Optional[str], typer.Option(..., help="Optional 'on_behalf_of' participant ID")
    ] = None,
    timeout: Annotated[
        Optional[int],
        typer.Option(
            ...,
            help="How long to wait for the toolkit if the toolkit is not in the room",
        ),
    ] = 30,
):
    """
    Invoke a specific tool from a given toolkit with arguments.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            found = timeout == 0
            for i in range(timeout):
                if found:
                    break

                if i == 1:
                    print("[magenta]Waiting for toolkit...[/magenta]")

                agents = await client.agents.list_toolkits(
                    participant_id=participant_id
                )
                await asyncio.sleep(1)

                for a in agents:
                    if a.name == toolkit:
                        found = True
                        break

            if not found:
                print("[red]Timed out waiting for toolkit to join the room[/red]")
                raise typer.Exit(1)

            print("[bold green]Invoking tool...[/bold green]")
            response = await client.agents.invoke_tool(
                toolkit=toolkit,
                tool=tool,
                input=json.loads(arguments),
                participant_id=participant_id,
                on_behalf_of_id=on_behalf_of_id,
            )
            if not isinstance(response, Content):
                print("[bold green]Tool response stream opened[/bold green]")
                async for chunk in response:
                    print(json.dumps(_chunk_to_output(chunk), indent=2, default=str))
            else:
                print(json.dumps(_chunk_to_output(response), indent=2, default=str))
    except RoomException as e:
        print(e)
    finally:
        await account_client.close()


@app.async_command(
    "list-toolkits", help="List toolkits (and tools) available in the room"
)
async def list_toolkits_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "user",
    participant_id: Annotated[
        Optional[str], typer.Option(..., help="Optional participant ID")
    ] = None,
):
    """
    List all toolkits (and tools within them) available in the room.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)
        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            print("[bold green]Fetching list of toolkits...[/bold green]")
            toolkits = await client.agents.list_toolkits(participant_id=participant_id)

            # Format and output as JSON
            output = []
            for tk in toolkits:
                output.append(
                    {
                        "name": tk.name,
                        "title": tk.title,
                        "description": tk.description,
                        "tools": [
                            {
                                "name": tool.name,
                                "title": tool.title,
                                "description": tool.description,
                                "input_schema": tool.input_schema,
                                "defs": tool.defs,
                            }
                            for tool in tk.tools
                        ],
                    }
                )
            print(json.dumps(output, indent=2))

    finally:
        await account_client.close()
