import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption, RoomOption
import json as _json

from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api import (
    RoomClient,
    ParticipantToken,
    WebSocketClientProtocol,
    RoomException,
    ApiScope,
)
from meshagent.cli.helper import resolve_project_id, resolve_api_key, resolve_room
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client

app = async_typer.AsyncTyper(help="Use queues in a room")


@app.async_command("send")
async def send(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
    queue: Annotated[str, typer.Option(..., help="Queue name")],
    json: Optional[str] = typer.Option(..., help="a JSON message to send to the queue"),
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a JSON file"),
    ] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)

        key = (
            await account_client.decrypt_project_api_key(
                project_id=project_id, id=api_key_id
            )
        )["token"]

        token = ParticipantToken(
            name=name, project_id=project_id, api_key_id=api_key_id
        )
        token.add_api_grant(ApiScope.agent_default())
        token.add_role_grant(role=role)
        token.add_room_grant(room)
        token.add_api_grant(ApiScope.agent_default())

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=token.to_jwt(token=key),
            )
        ) as client:
            if file is not None:
                with open(file, "rb") as f:
                    message = f.read()
            else:
                message = _json.loads(json)

            await client.queues.send(name=queue, message=message)

    except RoomException as e:
        print(f"[red]{e}[/red]")
    finally:
        await account_client.close()


@app.async_command("receive")
async def receive(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "user",
    queue: Annotated[str, typer.Option(..., help="Queue name")],
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)

        key = (
            await account_client.decrypt_project_api_key(
                project_id=project_id, id=api_key_id
            )
        )["token"]

        token = ParticipantToken(
            name=name, project_id=project_id, api_key_id=api_key_id
        )
        token.add_api_grant(ApiScope.agent_default())
        token.add_role_grant(role=role)
        token.add_room_grant(room)

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=token.to_jwt(token=key),
            )
        ) as client:
            response = await client.queues.receive(name=queue, wait=False)
            if response is None:
                print("[bold yellow]Queue did not contain any messages.[/bold yellow]")
                raise typer.Exit(1)
            else:
                print(response)

    except RoomException as e:
        print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()
