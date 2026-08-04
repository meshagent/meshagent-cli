import json

from rich import print

from meshagent.api import RoomClient, RoomException, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)


app = async_typer.AsyncTyper(help="List storage mounts currently ready in a room")


@app.callback()
def _mounts_callback() -> None:
    pass


@app.async_command("list")
async def list_mounts_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output: OutputFormatOption = "table",
) -> None:
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise RoomException("--room is required")
        connection = await account_client.connect_room(
            project_id=project_id,
            room=room_name,
        )
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=connection.jwt,
            ).create_factory()
        ) as room_client:
            mounts = await room_client.mounts.list()
        records = [mount.model_dump(mode="json") for mount in mounts]
        if output == "json":
            print(json.dumps(records, indent=2))
        elif records:
            print_json_table(
                records,
                "id",
                "name",
                "required",
                "description",
                "consumers",
            )
        else:
            print("No storage mounts are ready.")
    finally:
        await account_client.close()
