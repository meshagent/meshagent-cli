import typer
from rich import print
from typing import Annotated, Optional
import json
import asyncio

from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api import (
    RoomClient,
    ParticipantToken,
    WebSocketClientProtocol,
    RoomException,
)
from meshagent.cli.helper import resolve_project_id, resolve_api_key
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_token_jwt

app = async_typer.AsyncTyper()


@app.async_command("ask")
async def ask(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option()],
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
    agent: Annotated[str, typer.Option()],
    input: Annotated[str, typer.Option()],
    timeout: Annotated[
        Optional[int],
        typer.Option(
            ..., help="How long to wait for the agent if the agent is not in the room"
        ),
    ] = 30,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        key = (
            await account_client.decrypt_project_api_key(
                project_id=project_id, id=api_key_id
            )
        )["token"]

        token = ParticipantToken(
            name=name, project_id=project_id, api_key_id=api_key_id
        )

        token.add_role_grant(role=role)
        token.add_room_grant(room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=token.to_jwt(token=key),
            )
        ) as client:
            found = timeout == 0
            for i in range(30):
                if found:
                    break

                if i == 1:
                    print("[magenta]Waiting for agent...[/magenta]")

                agents = await client.agents.list_agents()
                await asyncio.sleep(1)

                for a in agents:
                    if a.name == agent:
                        found = True
                        break

            if not found:
                print("[red]Timed out waiting for agent to join the room[/red]")
                raise typer.Exit(1)

            print("[magenta]Asking agent...[/magenta]")

            response = await client.agents.ask(agent=agent, arguments=json.loads(input))
            print(json.dumps(response.json))
    except RoomException as e:
        print(f"[red]{e}[/red]")
    finally:
        await account_client.close()


@app.async_command("invoke-tool")
async def invoke_tool(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option()],
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
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
    caller_context: Annotated[
        Optional[str], typer.Option(..., help="Optional JSON for caller context")
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
        api_key_id = await resolve_api_key(project_id, api_key_id)

        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
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
            parsed_context = json.loads(caller_context) if caller_context else None
            response = await client.agents.invoke_tool(
                toolkit=toolkit,
                tool=tool,
                arguments=json.loads(arguments),
                participant_id=participant_id,
                on_behalf_of_id=on_behalf_of_id,
                caller_context=parsed_context,
            )
            # The response is presumably a dictionary or similar
            print(response.to_json())
    except RoomException as e:
        print(f"[red]{e}[/red]")
    finally:
        await account_client.close()


@app.async_command("list-agents")
async def list_agents_command(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option()],
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
):
    """
    List all agents available in the room.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        ) as client:
            print("[bold green]Fetching list of agents...[/bold green]")
            agents = await client.agents.list_agents()
            # Format the output as JSON
            output = []
            for agent in agents:
                output.append(
                    {
                        "name": agent.name,
                        "title": agent.title,
                        "description": agent.description,
                        "requires": [r.to_json() for r in agent.requires],
                        "supports_tools": agent.supports_tools,
                        "labels": agent.labels,
                    }
                )
            print(json.dumps(output, indent=2))

    finally:
        await account_client.close()


@app.async_command("list-toolkits")
async def list_toolkits_command(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option()],
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
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
        api_key_id = await resolve_api_key(project_id, api_key_id)
        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
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
                        "thumbnail_url": tk.thumbnail_url,
                        "tools": [
                            {
                                "name": tool.name,
                                "title": tool.title,
                                "description": tool.description,
                                "input_schema": tool.input_schema,
                                "thumbnail_url": tool.thumbnail_url,
                                "defs": tool.defs,
                                "supports_context": tool.supports_context,
                            }
                            for tool in tk.tools
                        ],
                    }
                )
            print(json.dumps(output, indent=2))

    finally:
        await account_client.close()
