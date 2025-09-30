import asyncio
import json
from rich import print
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
)
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url

app = async_typer.AsyncTyper()


@app.async_command("watch")
async def watch_logs(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
):
    """
    Watch logs from the developer feed in the specified room.
    """

    account_client = await get_client()
    try:
        # Resolve project ID (or fetch from the active project if not provided)
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=connection.jwt,
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
