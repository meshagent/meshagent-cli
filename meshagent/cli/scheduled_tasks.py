from __future__ import annotations

from typing import Annotated, Any, Optional

import json
import os
from pathlib import Path

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room


app = async_typer.AsyncTyper(help="Manage scheduled tasks for your project")


def _parse_annotations(annotations: Optional[str]) -> Optional[dict[str, str]]:
    if annotations is None:
        return None

    if annotations.strip() == "":
        return {}

    try:
        parsed = json.loads(annotations)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("Invalid JSON for --annotations") from exc

    if not isinstance(parsed, dict):
        raise typer.BadParameter("--annotations must be a JSON object")

    out: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise typer.BadParameter(
                "--annotations must be a JSON object with string keys and values"
            )
        out[key] = value
    return out


def _load_payload(*, payload: Optional[str], payload_file: Optional[str]) -> Any:
    if payload is not None and payload_file is not None:
        raise typer.BadParameter("Provide only one of --payload or --payload-file")

    if payload is None and payload_file is None:
        raise typer.BadParameter("Provide --payload or --payload-file")

    payload_text: str
    if payload_file is not None:
        try:
            payload_text = (
                Path(payload_file).expanduser().resolve().read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise typer.BadParameter(
                f"Unable to read payload from --payload-file: {exc}"
            ) from exc
    else:
        payload_text = payload or ""

    try:
        return json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--payload must be valid JSON") from exc


def _require_room(room: Optional[str]) -> str:
    resolved = resolve_room(room)
    if resolved is None:
        print("[red]Room name not specified, pass --room or set MESHAGENT_ROOM[/red]")
        raise typer.Exit(code=1)
    return resolved


@app.async_command("add")
async def scheduled_task_add(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", "-r", help="Room name"),
    ] = os.getenv("MESHAGENT_ROOM"),
    queue: Annotated[
        str,
        typer.Option("--queue", "-q", help="Queue name to dispatch the task to"),
    ],
    schedule: Annotated[
        str,
        typer.Option("--schedule", "-s", help="Cron schedule for task execution"),
    ],
    payload: Annotated[
        Optional[str],
        typer.Option(
            "--payload",
            help='JSON payload to enqueue (for example \'{"action":"sync"}\')',
        ),
    ] = None,
    payload_file: Annotated[
        Optional[str],
        typer.Option(
            "--payload-file",
            help="Path to a file containing JSON payload",
        ),
    ] = None,
    task_id: Annotated[
        Optional[str],
        typer.Option("--id", help="Optional task id"),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run once and then deactivate"),
    ] = False,
    active: Annotated[
        bool,
        typer.Option("--active/--inactive", help="Initial active state"),
    ] = True,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-n",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Add a scheduled task."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room_name = _require_room(room)
        task_payload = _load_payload(payload=payload, payload_file=payload_file)
        parsed_annotations = _parse_annotations(annotations) or {}

        try:
            created_task_id = await client.create_scheduled_task(
                project_id=project_id,
                room_name=room_name,
                queue_name=queue,
                payload=task_payload,
                schedule=schedule,
                active=active,
                task_id=task_id,
                once=once,
                annotations=parsed_annotations,
            )
        except ClientResponseError as exc:
            if exc.status == 409:
                print("[red]Scheduled task already exists[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created scheduled task:[/] {created_task_id}")
    finally:
        await client.close()
