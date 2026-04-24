from __future__ import annotations

import json
from typing import Annotated, Any, Optional

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)


app = async_typer.AsyncTyper(help="Manage feed subscriptions for your project")


def _parse_annotations(annotations: Optional[str]) -> Optional[dict[str, str]]:
    if annotations is None:
        return None
    if annotations.strip() == "":
        return {}
    try:
        payload = json.loads(annotations)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("Invalid JSON for --annotations") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("--annotations must be a JSON object")
    return {str(key): str(value) for key, value in payload.items()}


def _subscription_row(subscription: Any) -> dict[str, str]:
    return {
        "id": subscription.id,
        "room": subscription.room,
        "path": subscription.path,
    }


@app.async_command("create")
async def subscription_create(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Option("--feed-id", help="Feed id")],
    room: Annotated[str, typer.Option("--room", help="Room name")],
    path: Annotated[str, typer.Option("--path", help="Storage path prefix")],
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations", help='annotations in json format {"name":"value"}'
        ),
    ] = None,
):
    """Create a feed subscription."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        subscription = await client.create_feed_subscription(
            project_id=project_id,
            feed_id=feed_id,
            room=resolve_room(room),
            path=path,
            annotations=_parse_annotations(annotations) or {},
        )
        print(subscription.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("update")
async def subscription_update(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Option("--feed-id", help="Feed id")],
    subscription_id: Annotated[str, typer.Argument(help="Subscription id to update")],
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations", help='annotations in json format {"name":"value"}'
        ),
    ] = None,
):
    """Update a feed subscription."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        existing = await client.get_feed_subscription(
            project_id=project_id,
            feed_id=feed_id,
            subscription_id=subscription_id,
        )
        await client.update_feed_subscription(
            project_id=project_id,
            feed_id=feed_id,
            subscription_id=subscription_id,
            annotations=_parse_annotations(annotations)
            if annotations is not None
            else existing.annotations,
        )
        print(f"[green]Updated feed subscription:[/] {subscription_id}")
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed subscription not found:[/] {subscription_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()


@app.async_command("show")
async def subscription_show(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Option("--feed-id", help="Feed id")],
    subscription_id: Annotated[str, typer.Argument(help="Subscription id to show")],
):
    """Show feed subscription details."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        subscription = await client.get_feed_subscription(
            project_id=project_id,
            feed_id=feed_id,
            subscription_id=subscription_id,
        )
        print(subscription.model_dump(mode="json"))
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed subscription not found:[/] {subscription_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()


@app.async_command("list")
async def subscription_list(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Option("--feed-id", help="Feed id")],
    o: OutputFormatOption = "table",
):
    """List subscriptions for a feed."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        subscriptions = await client.list_feed_subscriptions(
            project_id=project_id,
            feed_id=feed_id,
        )
        if o == "json":
            print(
                {
                    "subscriptions": [
                        subscription.model_dump(mode="json")
                        for subscription in subscriptions
                    ]
                }
            )
        else:
            print_json_table(
                [_subscription_row(subscription) for subscription in subscriptions],
                "id",
                "room",
                "path",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def subscription_delete(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Option("--feed-id", help="Feed id")],
    subscription_id: Annotated[str, typer.Argument(help="Subscription id to delete")],
):
    """Delete a feed subscription."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        await client.delete_feed_subscription(
            project_id=project_id,
            feed_id=feed_id,
            subscription_id=subscription_id,
        )
        print(f"[green]Deleted feed subscription:[/] {subscription_id}")
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed subscription not found:[/] {subscription_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()
