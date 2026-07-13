# meshagent/cli/mailboxes.py

from __future__ import annotations

from typing import Annotated, Optional

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.api.client import Mailbox, ValidationErrorResponse
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, OutputFormatOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)

import json
import os

app = async_typer.AsyncTyper(help="Manage mailboxes for your project")


async def _list_project_mailboxes(
    client,
    *,
    project_id: str,
    count: int,
    offset: int,
    filter: str | None,
) -> list[Mailbox]:
    if count <= 0:
        return []

    mailboxes: list[Mailbox] = []
    continuation_token: str | None = None
    target_count = offset + count
    while len(mailboxes) < target_count:
        page = await client.list_mailboxes_page(
            project_id=project_id,
            page_size=min(target_count - len(mailboxes), 100),
            continuation_token=continuation_token,
            filter=filter,
        )
        mailboxes.extend(page.mailboxes)
        continuation_token = page.continuation_token
        if continuation_token is None or not page.mailboxes:
            break

    return mailboxes[offset:target_count]


def _validation_error_message(
    exc: ClientResponseError | ValidationErrorResponse,
) -> str:
    message = exc.message if isinstance(exc, ClientResponseError) else str(exc)
    for marker in ("body=", "body: "):
        if marker in message:
            message = message.split(marker, 1)[1]
            break
    if message.startswith("400: "):
        message = message.removeprefix("400: ")
    return message.strip()


def _parse_annotations(annotations: Optional[str]) -> Optional[dict[str, str]]:
    if annotations is None:
        return None
    if annotations.strip() == "":
        return {}
    try:
        return json.loads(annotations)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("Invalid JSON for --annotations") from exc


@app.async_command("create")
async def mailbox_create(
    *,
    project_id: ProjectIdOption,
    address: Annotated[
        str,
        typer.Option(
            "--address",
            "-a",
            help="Mailbox email address (unique per project)",
        ),
    ],
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
    queue: Annotated[
        str,
        typer.Option(
            "--queue",
            "-q",
            help="Queue name to deliver inbound messages to",
        ),
    ],
    public: Annotated[
        bool,
        typer.Option(
            "--public",
            help="Queue name to deliver inbound messages to",
        ),
    ] = False,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-n",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Create a mailbox attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)
        try:
            parsed_annotations = _parse_annotations(annotations) or {}
            await client.create_mailbox(
                project_id=project_id,
                address=address,
                room=room,
                queue=queue,
                public=public,
                annotations=parsed_annotations,
            )
        except (ClientResponseError, ValidationErrorResponse) as exc:
            # Common patterns: 409 conflict on duplicate address, 400 validation, etc.
            status = exc.status if isinstance(exc, ClientResponseError) else 400
            if status == 409:
                print(f"[red]Mailbox address already in use:[/] {address}")
                raise typer.Exit(code=1)
            if status == 400:
                print(f"[red]{_validation_error_message(exc)}[/]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created mailbox:[/] {address}")
    finally:
        await client.close()


@app.async_command("update")
async def mailbox_update(
    *,
    project_id: ProjectIdOption,
    address: Annotated[
        str,
        typer.Argument(help="Mailbox email address to update"),
    ],
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room",
            "-r",
            help="Room name to route inbound mail into",
        ),
    ] = os.getenv("MESHAGENT_ROOM"),
    queue: Annotated[
        Optional[str],
        typer.Option(
            "--queue",
            "-q",
            help="Queue name to deliver inbound messages to",
        ),
    ] = None,
    public: Annotated[
        bool,
        typer.Option(
            "--public",
            help="Queue name to deliver inbound messages to",
        ),
    ] = False,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-n",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Update a mailbox routing configuration."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)
        # Keep parity with other CLIs: allow partial update by reading existing first
        parsed_annotations = _parse_annotations(annotations)

        if room is None or queue is None or parsed_annotations is None:
            try:
                mb = await client.get_mailbox(project_id=project_id, address=address)
            except (ClientResponseError, ValidationErrorResponse) as exc:
                status = exc.status if isinstance(exc, ClientResponseError) else 400
                if status == 404:
                    print(f"[red]Mailbox not found:[/] {address}")
                    raise typer.Exit(code=1)
                if status == 400:
                    print(f"[red]{_validation_error_message(exc)}[/]")
                    raise typer.Exit(code=1)
                raise
            room = room or mb.room
            queue = queue or mb.queue
            parsed_annotations = (
                parsed_annotations if parsed_annotations is not None else mb.annotations
            )

        try:
            await client.update_mailbox(
                project_id=project_id,
                address=address,
                room=room,
                queue=queue,
                public=public,
                annotations=parsed_annotations,
            )
        except (ClientResponseError, ValidationErrorResponse) as exc:
            status = exc.status if isinstance(exc, ClientResponseError) else 400
            if status == 404:
                print(f"[red]Mailbox not found:[/] {address}")
                raise typer.Exit(code=1)
            if status == 400:
                print(f"[red]{_validation_error_message(exc)}[/]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated mailbox:[/] {address}")
    finally:
        await client.close()


@app.async_command("get")
async def mailbox_get(
    *,
    project_id: ProjectIdOption,
    address: Annotated[str, typer.Argument(help="Mailbox address to get")],
):
    """Get mailbox details."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        try:
            mb = await client.get_mailbox(project_id=project_id, address=address)
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Mailbox not found:[/] {address}")
                raise typer.Exit(code=1)
            raise
        print(mb.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("list")
async def mailbox_list(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
    filter: Annotated[
        Optional[str], typer.Option("--filter", help="Lowercase contains filter")
    ] = None,
    count: Annotated[
        int,
        typer.Option("--count", help="Maximum number of mailboxes to return", min=1),
    ] = 100,
    offset: Annotated[
        int, typer.Option("--offset", help="Row offset for pagination", min=0)
    ] = 0,
    o: OutputFormatOption = "table",
):
    """List mailboxes for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)

        if room is not None:
            mailboxes = await client.list_room_mailboxes(
                project_id=project_id,
                room_name=room,
                count=count,
                offset=offset,
                filter=filter,
            )
        else:
            mailboxes = await _list_project_mailboxes(
                client,
                project_id=project_id,
                count=count,
                offset=offset,
                filter=filter,
            )

        if o == "json":
            # Keep your existing conventions: wrap in an object.
            print({"mailboxes": [mb.model_dump(mode="json") for mb in mailboxes]})
        else:
            print_json_table(
                [
                    {
                        "address": mb.address,
                        "room": mb.room,
                        "queue": mb.queue,
                        "public": mb.public,
                    }
                    for mb in mailboxes
                ],
                "address",
                "room",
                "queue",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def mailbox_delete(
    *,
    project_id: ProjectIdOption,
    address: Annotated[str, typer.Argument(help="Mailbox address to delete")],
):
    """Delete a mailbox."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        try:
            await client.delete_mailbox(project_id=project_id, address=address)
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Mailbox not found:[/] {address}")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Mailbox deleted:[/] {address}")
    finally:
        await client.close()
