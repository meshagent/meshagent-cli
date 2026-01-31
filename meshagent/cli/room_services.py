from rich import print
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.api.room_server_client import ServicesClient
from meshagent.api.specs.service import ServiceSpec


app = async_typer.AsyncTyper(help="Manage services inside a room")


@app.async_command("list", help="List services running in a room")
async def room_services_list_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output: OutputFormatOption = "table",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            )
        ) as client:
            print("[bold green]Fetching services...[/bold green]")
            services_client = ServicesClient(room=client)
            services: list[ServiceSpec] = await services_client.list()

            if output == "json":
                print({"services": [svc.model_dump(mode="json") for svc in services]})
            else:
                print_json_table(
                    [
                        {
                            "id": svc.id,
                            "name": svc.metadata.name,
                            "image": svc.container.image
                            if svc.container is not None
                            else None,
                        }
                        for svc in services
                    ],
                    "id",
                    "name",
                    "image",
                )
    finally:
        await account_client.close()
