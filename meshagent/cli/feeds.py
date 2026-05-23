from __future__ import annotations

import json
from pathlib import Path
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


app = async_typer.AsyncTyper(help="Manage feeds for your project")

_FEED_BATCHING_NOTE = (
    "[yellow]Feed subscriptions are batched for performance. "
    "Messages may take up to a minute to appear in room storage.[/]"
)


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


def _load_json_payload(
    *, value: Optional[str], file: Optional[Path], option_name: str
) -> Any | None:
    if value is not None and file is not None:
        raise typer.BadParameter(
            f"Use either {option_name} or {option_name}-file, not both"
        )
    if value is None and file is None:
        return None

    raw = value
    if file is not None:
        try:
            raw = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"Unable to read {file}") from exc

    assert raw is not None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON for {option_name}") from exc


def _load_jsonl_messages(path: Path) -> list[Any]:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read {path}") from exc

    messages: list[Any] = []
    for line_number, line in enumerate(contents.splitlines(), start=1):
        if line.strip() == "":
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(
                f"Invalid JSON on line {line_number} of {path}"
            ) from exc

    if len(messages) == 0:
        raise typer.BadParameter("JSONL file must contain at least one JSON value")

    return messages


def _feed_table_row(feed: Any) -> dict[str, Any]:
    return {
        "id": feed.id,
        "name": feed.name,
        "visibility": feed.visibility,
        "paused": feed.paused,
    }


@app.async_command("create")
async def feed_create(
    *,
    project_id: ProjectIdOption,
    name: Annotated[str, typer.Option("--name", "-n", help="Feed name")],
    description: Annotated[
        str, typer.Option("--description", "-d", help="Feed description")
    ] = "",
    visibility: Annotated[
        str,
        typer.Option("--visibility", help="Feed visibility [public|project|private]"),
    ] = "private",
    paused: Annotated[
        bool,
        typer.Option("--paused", help="Create the feed in a paused state"),
    ] = False,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations", help='annotations in json format {"name":"value"}'
        ),
    ] = None,
    message_schema: Annotated[
        Optional[str],
        typer.Option("--message-schema", help="JSON schema as inline JSON"),
    ] = None,
    message_schema_file: Annotated[
        Optional[Path],
        typer.Option("--message-schema-file", help="Path to a JSON schema file"),
    ] = None,
):
    """Create a feed."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        feed = await client.create_feed(
            project_id=project_id,
            name=name,
            description=description,
            visibility=visibility,
            paused=paused,
            annotations=_parse_annotations(annotations) or {},
            message_schema=_load_json_payload(
                value=message_schema,
                file=message_schema_file,
                option_name="--message-schema",
            ),
        )
        print(feed.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("update")
async def feed_update(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Argument(help="Feed id to update")],
    name: Annotated[
        Optional[str], typer.Option("--name", "-n", help="Feed name")
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option("--description", "-d", help="Feed description"),
    ] = None,
    paused: Annotated[
        bool,
        typer.Option("--paused", help="Pause the feed"),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume a paused feed"),
    ] = False,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations", help='annotations in json format {"name":"value"}'
        ),
    ] = None,
    message_schema: Annotated[
        Optional[str],
        typer.Option("--message-schema", help="JSON schema as inline JSON"),
    ] = None,
    message_schema_file: Annotated[
        Optional[Path],
        typer.Option("--message-schema-file", help="Path to a JSON schema file"),
    ] = None,
    clear_message_schema: Annotated[
        bool,
        typer.Option(
            "--clear-message-schema", help="Remove the existing message schema"
        ),
    ] = False,
):
    """Update a feed."""
    if paused and resume:
        raise typer.BadParameter("Use either --paused or --resume, not both")

    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        existing = await client.get_feed(project_id=project_id, feed_id=feed_id)
        if clear_message_schema and (
            message_schema is not None or message_schema_file is not None
        ):
            raise typer.BadParameter(
                "Use either --clear-message-schema or --message-schema/--message-schema-file"
            )

        resolved_message_schema = (
            None
            if clear_message_schema
            else _load_json_payload(
                value=message_schema,
                file=message_schema_file,
                option_name="--message-schema",
            )
        )
        await client.update_feed(
            project_id=project_id,
            feed_id=feed_id,
            name=name or existing.name,
            description=description
            if description is not None
            else existing.description,
            paused=True if paused else False if resume else existing.paused,
            annotations=_parse_annotations(annotations)
            if annotations is not None
            else existing.annotations,
            message_schema=resolved_message_schema
            if clear_message_schema or resolved_message_schema is not None
            else existing.message_schema,
        )
        print(f"[green]Updated feed:[/] {feed_id}")
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed not found:[/] {feed_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()


@app.async_command("get")
async def feed_get(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Argument(help="Feed id to get")],
):
    """Get feed details."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        feed = await client.get_feed(project_id=project_id, feed_id=feed_id)
        print(feed.model_dump(mode="json"))
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed not found:[/] {feed_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()


@app.async_command("list")
async def feed_list(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="Room name to filter feeds by"),
    ] = None,
    filter: Annotated[
        Optional[str], typer.Option("--filter", help="Lowercase contains filter")
    ] = None,
    count: Annotated[
        int, typer.Option("--count", help="Maximum number of feeds to return")
    ] = 100,
    offset: Annotated[
        int, typer.Option("--offset", help="Row offset for pagination")
    ] = 0,
    o: OutputFormatOption = "table",
):
    """List feeds for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)
        feeds = (
            await client.list_room_feeds(
                project_id=project_id,
                room_name=room,
                count=count,
                offset=offset,
                filter=filter,
            )
            if room is not None
            else await client.list_feeds(
                project_id=project_id,
                count=count,
                offset=offset,
                filter=filter,
            )
        )
        if o == "json":
            print({"feeds": [feed.model_dump(mode="json") for feed in feeds]})
        else:
            print_json_table(
                [_feed_table_row(feed) for feed in feeds],
                "id",
                "name",
                "visibility",
                "paused",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def feed_delete(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Argument(help="Feed id to delete")],
):
    """Delete a feed."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        await client.delete_feed(project_id=project_id, feed_id=feed_id)
        print(f"[green]Deleted feed:[/] {feed_id}")
    except ClientResponseError as exc:
        if exc.status == 404:
            print(f"[red]Feed not found:[/] {feed_id}")
            raise typer.Exit(code=1)
        raise
    finally:
        await client.close()


@app.async_command("send")
async def feed_send(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Argument(help="Feed id to publish to")],
    message: Annotated[
        Optional[str],
        typer.Option("--message", help="Inline JSON message"),
    ] = None,
    message_file: Annotated[
        Optional[Path],
        typer.Option("--message-file", help="Path to a JSON file"),
    ] = None,
):
    """Publish a single JSON message to a feed."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        payload = _load_json_payload(
            value=message,
            file=message_file,
            option_name="--message",
        )
        if payload is None:
            raise typer.BadParameter("Provide --message or --message-file")
        await client.publish_feed_message(
            project_id=project_id,
            feed_id=feed_id,
            message=payload,
        )
        print(f"[green]Published message to feed:[/] {feed_id}")
        print(_FEED_BATCHING_NOTE)
    finally:
        await client.close()


@app.async_command("send-batch")
async def feed_send_batch(
    *,
    project_id: ProjectIdOption,
    feed_id: Annotated[str, typer.Argument(help="Feed id to publish to")],
    jsonl_file: Annotated[
        Path,
        typer.Option("--jsonl-file", help="Path to a JSONL file"),
    ],
):
    """Publish a JSONL file to a feed."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        messages = _load_jsonl_messages(jsonl_file)
        await client.publish_feed_batch(
            project_id=project_id,
            feed_id=feed_id,
            messages=messages,
        )
        print(f"[green]Published {len(messages)} messages to feed:[/] {feed_id}")
        print(_FEED_BATCHING_NOTE)
    finally:
        await client.close()
