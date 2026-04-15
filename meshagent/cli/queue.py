import base64
import json as _json
import mimetypes
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.api import (
    RoomClient,
    RoomException,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import websocket_room_url
from meshagent.api.room_server_client import Queue as RoomQueue
from meshagent.agents.mail_common import create_email_message
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)

app = async_typer.AsyncTyper(help="Use queues in a room")


def _queue_rows(queues: list[RoomQueue]) -> list[dict[str, str | int]]:
    return [{"name": item.name, "size": item.size} for item in queues]


@app.async_command("list", help="List queues in a room.")
async def queue_list(
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

        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            rows = _queue_rows(await client.queues.list())

            if output == "json":
                print(_json.dumps({"queues": rows}, indent=2))
                return

            if len(rows) == 0:
                print("There are no queues currently in the room")
                return

            print_json_table(rows, "name", "size")

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("send", help="Send a JSON message to a room queue.")
async def send(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
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
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            if file is not None:
                with open(file, "rb") as f:
                    message = f.read()
            else:
                message = _json.loads(json)

            await client.queues.send(name=queue, message=message)

    except RoomException as e:
        print(e)
    finally:
        await account_client.close()


@app.async_command(
    "send-mail",
    help="Create an email message and send it to a room queue.",
)
async def send_mail(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    queue: Annotated[str, typer.Option(..., help="Queue name")],
    subject: Annotated[str, typer.Option(..., help="Email subject")],
    body: Annotated[str, typer.Option(help="Email body")] = "",
    from_address: Annotated[
        str,
        typer.Option("--from", help="Sender email address"),
    ],
    attachment: Annotated[
        list[str],
        typer.Option(
            "--attachment",
            help="Attachment file path. May be provided multiple times.",
        ),
    ] = [],
):
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
            message = create_email_message(
                to_address=queue,
                from_address=from_address,
                subject=subject,
                body=body,
            )

            for attachment_path in attachment:
                path = Path(attachment_path)
                mime_type, _ = mimetypes.guess_type(path.name)
                if mime_type is None:
                    mime_type = "application/octet-stream"
                maintype, subtype = mime_type.split("/", 1)
                message.add_attachment(
                    path.read_bytes(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=path.name,
                )

            await client.queues.send(
                name=queue,
                message={
                    "base64": base64.b64encode(message.as_bytes()).decode("ascii"),
                },
            )
            print(f"[bold green]Queued email message in {queue}.[/bold green]")

    except RoomException as e:
        print(e)
    finally:
        await account_client.close()


@app.async_command("receive", help="Receive a message from a room queue.")
async def receive(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    queue: Annotated[str, typer.Option(..., help="Queue name")],
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            response = await client.queues.receive(name=queue, wait=False)
            if response is None:
                print("[bold yellow]Queue did not contain any messages.[/bold yellow]")
                raise typer.Exit(1)
            else:
                print(response)

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("size", help="Show the current size of a room queue.")
async def size(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    queue: Annotated[str, typer.Option(..., help="Queue name")],
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            matching_queue = next(
                (item for item in await client.queues.list() if item.name == queue),
                None,
            )
            if matching_queue is None:
                print(f"[bold red]Queue not found:[/bold red] {queue}")
                raise typer.Exit(1)

            print(matching_queue.size)

    except RoomException as e:
        print(e)
        raise typer.Exit(1)
    finally:
        await account_client.close()
