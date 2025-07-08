import typer
from rich import print
from typing import Annotated, Optional
import json

from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
    resolve_token_jwt,
    resolve_room,
)

app = async_typer.AsyncTyper()


@app.async_command("list-participants")
async def messaging_list_participants_command(
    *,
    project_id: str = None,
    room: Annotated[Optional[str], typer.Option()] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option()] = "cli",
    role: str = "user",
):
    """
    List all messaging-enabled participants in the room.
    """
    account_client = await get_client()
    try:
        # Resolve project_id if not provided
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        room = resolve_room(room)

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
            # Must enable before we can see who else is enabled
            await client.messaging.enable()
            await client.messaging.start()

            participants = client.messaging.get_participants()
            output = []
            for p in participants:
                output.append({"id": p.id, "role": p.role, "attributes": p._attributes})

            print(json.dumps(output, indent=2))

            await client.messaging.stop()

    finally:
        await account_client.close()


@app.async_command("send")
async def messaging_send_command(
    *,
    project_id: str = None,
    room: Annotated[Optional[str], typer.Option()] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option()] = "cli",
    role: str = "user",
    to_participant_id: Annotated[
        str, typer.Option(..., help="Participant ID to send a message to")
    ],
    data: Annotated[str, typer.Option(..., help="JSON message to send")],
):
    """
    Send a direct message to a single participant in the room.
    """
    account_client = await get_client()
    try:
        # Resolve project_id if not provided
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        room = resolve_room(room)

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
            # Create and enable messaging
            await client.messaging.enable()
            await client.messaging.start()

            # Find the participant we want to message
            participant = None
            for p in client.messaging.get_participants():
                if p.id == to_participant_id:
                    participant = p
                    break

            if participant is None:
                print(
                    f"[bold red]Participant with ID {to_participant_id} not found or not messaging-enabled.[/bold red]"
                )
            else:
                # Send the message
                await client.messaging.send_message(
                    to=participant,
                    type="chat.message",
                    message=json.loads(data),
                    attachment=None,
                )
                print("[bold cyan]Message sent successfully.[/bold cyan]")

            await client.messaging.stop()
    finally:
        await account_client.close()


@app.async_command("broadcast")
async def messaging_broadcast_command(
    *,
    project_id: str = None,
    room: Annotated[Optional[str], typer.Option()] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option()] = "cli",
    role: str = "user",
    data: Annotated[str, typer.Option(..., help="JSON message to broadcast")],
):
    """
    Broadcast a message to all messaging-enabled participants in the room.
    """
    account_client = await get_client()
    try:
        # Resolve project_id if not provided
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        room = resolve_room(room)
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
            # Create and enable messaging
            await client.messaging.enable()
            await client.messaging.start()

            # Broadcast the message
            await client.messaging.broadcast_message(
                type="chat.broadcast", message=json.loads(data), attachment=None
            )
            print("[bold cyan]Broadcast message sent successfully.[/bold cyan]")

            await client.messaging.stop()
    finally:
        await account_client.close()
