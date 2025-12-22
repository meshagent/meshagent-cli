# meshagent/cli/mailboxes.py

from __future__ import annotations

from typing import Annotated, Optional

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, OutputFormatOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(help="Manage mailboxes for your project")


@app.async_command("create")
async def mailbox_create(
    *,
    project_id: ProjectIdOption = None,
    address: Annotated[
        str,
        typer.Option(
            "--address",
            "-a",
            help="Mailbox email address (unique per project)",
        ),
    ],
    room: Annotated[
        str,
        typer.Option(
            "--room",
            "-r",
            help="Room name to route inbound mail into",
        ),
    ],
    queue: Annotated[
        str,
        typer.Option(
            "--queue",
            "-q",
            help="Queue name to deliver inbound messages to",
        ),
    ],
):
    """Create a mailbox attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        try:
            await client.create_mailbox(
                project_id=project_id,
                address=address,
                room=room,
                queue=queue,
            )
        except ClientResponseError as exc:
            # Common patterns: 409 conflict on duplicate address, 400 validation, etc.
            if exc.status == 409:
                print(f"[red]Mailbox address already in use:[/] {address}")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created mailbox:[/] {address}")
    finally:
        await client.close()


@app.async_command("update")
async def mailbox_update(
    *,
    project_id: ProjectIdOption = None,
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
    ] = None,
    queue: Annotated[
        Optional[str],
        typer.Option(
            "--queue",
            "-q",
            help="Queue name to deliver inbound messages to",
        ),
    ] = None,
):
    """Update a mailbox routing configuration."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        # Keep parity with other CLIs: allow partial update by reading existing first
        if room is None or queue is None:
            try:
                mb = await client.get_mailbox(project_id=project_id, address=address)
            except ClientResponseError as exc:
                if exc.status == 404:
                    print(f"[red]Mailbox not found:[/] {address}")
                    raise typer.Exit(code=1)
                raise
            room = room or mb.room
            queue = queue or mb.queue

        try:
            await client.update_mailbox(
                project_id=project_id,
                address=address,
                room=room,
                queue=queue,
            )
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Mailbox not found:[/] {address}")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated mailbox:[/] {address}")
    finally:
        await client.close()


@app.async_command("show")
async def mailbox_show(
    *,
    project_id: ProjectIdOption = None,
    address: Annotated[str, typer.Argument(help="Mailbox address to show")],
):
    """Show mailbox details."""
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
    project_id: ProjectIdOption = None,
    o: OutputFormatOption = "table",
):
    """List mailboxes for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        mailboxes = await client.list_mailboxes(project_id=project_id)

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
    project_id: ProjectIdOption = None,
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
