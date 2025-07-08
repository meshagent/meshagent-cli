import asyncio
import json
import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_project_id, resolve_api_key
from meshagent.api import RoomClient, ParticipantToken, WebSocketClientProtocol
from meshagent.api.helpers import meshagent_base_url, websocket_room_url

app = async_typer.AsyncTyper()


@app.async_command("watch")
async def watch_logs(
    *,
    project_id: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Project ID (if not set, will try to use the active project)"
        ),
    ] = None,
    room: Annotated[str, typer.Option(..., help="Name of the room to connect to")],
    api_key_id: Annotated[Optional[str], typer.Option(..., help="API Key ID")] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: Annotated[
        str, typer.Option(..., help="Role to assign to this participant")
    ] = "user",
):
    """
    Watch logs from the developer feed in the specified room.
    """

    account_client = await get_client()
    try:
        # Resolve project ID (or fetch from the active project if not provided)
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        # Decrypt the project's API key
        key = (
            await account_client.decrypt_project_api_key(
                project_id=project_id, id=api_key_id
            )
        )["token"]

        # Build a participant token
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
            # Create a developer client from the room client

            # Define how to handle the incoming log events
            def handle_log(type: str, data: dict):
                # You can customize this print to suit your needs
                print(f"[magenta]{type}[/magenta]: {json.dumps(data, indent=2)}")

            # Attach our handler to the "log" event
            client.developer.on("log", handle_log)

            # Enable watching
            await client.developer.enable()
            print("[bold cyan]watching enabled. Press Ctrl+C to stop.[/bold cyan]")

            try:
                # Block forever, until Ctrl+C
                while True:
                    await asyncio.sleep(10)
            except KeyboardInterrupt:
                print("[bold red]Stopping watch...[/bold red]")
            finally:
                # Disable watching before exiting
                await client.developer.disable()

    finally:
        await account_client.close()
