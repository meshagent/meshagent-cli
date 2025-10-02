import typer
from rich import print
import json
from typing import Annotated, List, Optional
from meshagent.cli.common_options import ProjectIdOption

from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper()

# ---------------------------------------------------------------------------
# Webhook commands
# ---------------------------------------------------------------------------


@app.async_command("create")
async def webhook_create(
    *,
    project_id: ProjectIdOption = None,
    name: Annotated[str, typer.Option(help="Friendly name for the webhook")],
    url: Annotated[str, typer.Option(help="Target URL that will receive POSTs")],
    event: Annotated[
        List[str],
        typer.Option(
            "-e", "--event", help="Event to subscribe to (repeat for multiple)."
        ),
    ],
    description: Annotated[
        str, typer.Option(default="", help="Optional description")
    ] = "",
    action: Annotated[
        Optional[str],
        typer.Option("--action", help="Optional action name delivered with each call"),
    ] = None,
    payload: Annotated[
        Optional[str],
        typer.Option(
            "--payload",
            help="Optional JSON string sent as the body (merged with event payload).",
        ),
    ] = None,
):
    """Create a new project webhook."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        payload_obj = json.loads(payload) if payload else None
        webhook = await client.create_webhook(
            project_id=project_id,
            name=name,
            url=url,
            events=event,
            description=description,
            action=action,
            payload=payload_obj,
        )
        print_json_table([webhook])
    finally:
        await client.close()


@app.async_command("list")
async def webhook_list(
    *,
    project_id: ProjectIdOption = None,
):
    """List all webhooks for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        hooks = await client.list_webhooks(project_id)
        print_json_table(
            hooks.get("webhooks"),
            "id",
            "name",
            "description",
            "url",
            "events",
            "action",
        )
    finally:
        await client.close()


@app.async_command("delete")
async def webhook_delete(
    *,
    project_id: ProjectIdOption = None,
    webhook_id: Annotated[str, typer.Argument(help="ID of the webhook to delete")],
):
    """Delete a project webhook."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        await client.delete_webhook(project_id, webhook_id)
        print(f"[green]Webhook {webhook_id} deleted.[/]")
    finally:
        await client.close()
