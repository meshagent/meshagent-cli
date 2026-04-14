from __future__ import annotations

from typing import Annotated, Any, Optional

import json
import os
from pathlib import Path

import typer
from rich import print

from meshagent.api.client import ConflictError, NotFoundError, ScheduledTask
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)


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


def _parse_active_state(*, active: bool, inactive: bool) -> Optional[bool]:
    if active and inactive:
        raise typer.BadParameter("Provide only one of --active or --inactive")
    if active:
        return True
    if inactive:
        return False
    return None


def _require_room(room: Optional[str]) -> str:
    resolved = resolve_room(room)
    if resolved is None:
        print("[red]Room name not specified, pass --room or set MESHAGENT_ROOM[/red]")
        raise typer.Exit(code=1)
    return resolved


def _scheduled_task_records(tasks: list[ScheduledTask]) -> list[dict[str, Any]]:
    return [task.model_dump(mode="json") for task in tasks]


def _scheduled_task_table_rows(tasks: list[ScheduledTask]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _scheduled_task_records(tasks):
        rows.append(
            {key: ("" if value is None else value) for key, value in record.items()}
        )
    return rows


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
        project_id = await resolve_project_id(project_id=project_id)
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
        except ConflictError:
            print("[red]Scheduled task already exists[/red]")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Created scheduled task:[/] {created_task_id}")
    finally:
        await client.close()


@app.async_command("list")
async def scheduled_task_list(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", "-r", help="Filter by room name"),
    ] = os.getenv("MESHAGENT_ROOM"),
    task_id: Annotated[
        Optional[str],
        typer.Option("--id", "--task-id", help="Filter by scheduled task id"),
    ] = None,
    active: Annotated[
        bool,
        typer.Option("--active", help="Filter to active tasks only"),
    ] = False,
    inactive: Annotated[
        bool,
        typer.Option("--inactive", help="Filter to inactive tasks only"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of tasks to return"),
    ] = 200,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Row offset for pagination"),
    ] = 0,
    o: OutputFormatOption = "table",
):
    """List scheduled tasks."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        active_filter = _parse_active_state(active=active, inactive=inactive)
        tasks = await client.list_scheduled_tasks(
            project_id=project_id,
            room_name=room,
            task_id=task_id,
            active=active_filter,
            limit=limit,
            offset=offset,
        )

        if o == "json":
            print(
                json.dumps(
                    {"scheduled_tasks": _scheduled_task_records(tasks)}, indent=2
                )
            )
            return

        if len(tasks) == 0:
            print("There are not currently any scheduled tasks in the project")
            return

        print_json_table(
            _scheduled_task_table_rows(tasks),
            "id",
            "room_name",
            "queue_name",
            "schedule",
            "active",
            "once",
            "last_status",
        )
    finally:
        await client.close()


@app.async_command("update")
async def scheduled_task_update(
    *,
    project_id: ProjectIdOption,
    task_id: Annotated[
        str,
        typer.Argument(help="Scheduled task id to update"),
    ],
    room: Annotated[
        Optional[str],
        typer.Option("--room", "-r", help="Updated room name"),
    ] = os.getenv("MESHAGENT_ROOM"),
    queue: Annotated[
        Optional[str],
        typer.Option("--queue", "-q", help="Updated queue name"),
    ] = None,
    schedule: Annotated[
        Optional[str],
        typer.Option("--schedule", "-s", help="Updated cron schedule"),
    ] = None,
    payload: Annotated[
        Optional[str],
        typer.Option(
            "--payload",
            help='Updated JSON payload to enqueue (for example \'{"action":"sync"}\')',
        ),
    ] = None,
    payload_file: Annotated[
        Optional[str],
        typer.Option(
            "--payload-file",
            help="Path to a file containing updated JSON payload",
        ),
    ] = None,
    active: Annotated[
        bool,
        typer.Option("--active", help="Mark the task active"),
    ] = False,
    inactive: Annotated[
        bool,
        typer.Option("--inactive", help="Mark the task inactive"),
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
    """Update a scheduled task."""
    active_state = _parse_active_state(active=active, inactive=inactive)
    parsed_annotations = (
        _parse_annotations(annotations) if annotations is not None else None
    )
    task_payload = None
    if payload is not None or payload_file is not None:
        task_payload = _load_payload(payload=payload, payload_file=payload_file)

    if all(
        value is None
        for value in (
            room,
            queue,
            schedule,
            task_payload,
            active_state,
            parsed_annotations,
        )
    ):
        raise typer.BadParameter("No changes specified")

    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        try:
            await client.update_scheduled_task(
                project_id=project_id,
                task_id=task_id,
                room_name=room,
                queue_name=queue,
                payload=task_payload,
                schedule=schedule,
                active=active_state,
                annotations=parsed_annotations,
            )
        except NotFoundError:
            print(f"[red]Scheduled task not found:[/] {task_id}")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Updated scheduled task:[/] {task_id}")
    finally:
        await client.close()


@app.async_command("delete")
async def scheduled_task_delete(
    *,
    project_id: ProjectIdOption,
    task_id: Annotated[
        str,
        typer.Argument(help="Scheduled task id to delete"),
    ],
):
    """Delete a scheduled task."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        try:
            await client.delete_scheduled_task(project_id=project_id, task_id=task_id)
        except NotFoundError:
            print(f"[red]Scheduled task not found:[/] {task_id}")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Deleted scheduled task:[/] {task_id}")
    finally:
        await client.close()
