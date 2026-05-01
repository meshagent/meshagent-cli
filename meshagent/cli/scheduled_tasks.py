from __future__ import annotations

from typing import Annotated, Any, Optional

import json
import os
from pathlib import Path

import typer
from pydantic import ValidationError
from rich import print

from meshagent.api.client import (
    ConflictError,
    NotFoundError,
    ScheduledTask,
    ScheduledTaskRun,
)
from meshagent.api.specs.service import ScheduledTaskSpec
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)


app = async_typer.AsyncTyper(help="Manage scheduled tasks for your project")


def _load_scheduled_task_spec(path: str) -> ScheduledTaskSpec:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
        return ScheduledTaskSpec.from_yaml(text)
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read scheduled task spec: {exc}") from exc
    except ValidationError as exc:
        raise typer.BadParameter(f"Invalid ScheduledTaskSpec: {exc}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Invalid scheduled task spec: {exc}") from exc


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


async def _resolve_room_id(client: Any, *, project_id: str, room: str) -> str:
    from meshagent.api.client import RoomException

    try:
        resolved = await client.get_room(project_id=project_id, name=room)
    except (NotFoundError, RoomException):
        print(f"[red]Room not found:[/] {room}")
        raise typer.Exit(code=1)
    return resolved.id


def _scheduled_task_records(tasks: list[ScheduledTask]) -> list[dict[str, Any]]:
    return [task.model_dump(mode="json", exclude_none=True) for task in tasks]


def _scheduled_task_table_rows(tasks: list[ScheduledTask]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _scheduled_task_records(tasks):
        container = record.get("container")
        row = {key: ("" if value is None else value) for key, value in record.items()}
        row["target"] = "container" if container else "queue"
        row["container_image"] = (
            container.get("image", "") if isinstance(container, dict) else ""
        )
        rows.append(row)
    return rows


def _scheduled_task_run_records(runs: list[ScheduledTaskRun]) -> list[dict[str, Any]]:
    return [run.model_dump(mode="json", exclude_none=True) for run in runs]


def _scheduled_task_run_table_rows(
    runs: list[ScheduledTaskRun],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in _scheduled_task_run_records(runs):
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
    file: Annotated[
        str,
        typer.Option(
            "--file",
            "-f",
            help="Path to a ScheduledTaskSpec YAML file",
        ),
    ],
):
    """Add a scheduled task."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = _require_room(room)
        spec = _load_scheduled_task_spec(file)

        try:
            created_task_id = await client.create_scheduled_task(
                project_id=project_id,
                room_name=room_name,
                spec=spec,
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
    filter: Annotated[
        Optional[str], typer.Option("--filter", help="Lowercase contains filter")
    ] = None,
    count: Annotated[
        int, typer.Option("--count", help="Maximum number of tasks to return")
    ] = 100,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of tasks to return", hidden=True),
    ] = 100,
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
        room_id = (
            await _resolve_room_id(client, project_id=project_id, room=room)
            if room is not None
            else None
        )
        list_kwargs = {
            "project_id": project_id,
            "room_id": room_id,
            "task_id": task_id,
            "active": active_filter,
            "limit": count if count != 100 else limit,
            "offset": offset,
        }
        if filter is not None:
            list_kwargs["filter"] = filter
        tasks = await client.list_scheduled_tasks(**list_kwargs)

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
            "target",
            "queue_name",
            "container_image",
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
    file: Annotated[
        str,
        typer.Option(
            "--file",
            "-f",
            help="Path to a replacement ScheduledTaskSpec YAML file",
        ),
    ],
):
    """Update a scheduled task."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        spec = _load_scheduled_task_spec(file)
        try:
            await client.update_scheduled_task(
                project_id=project_id,
                task_id=task_id,
                spec=spec,
            )
        except NotFoundError:
            print(f"[red]Scheduled task not found:[/] {task_id}")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Updated scheduled task:[/] {task_id}")
    finally:
        await client.close()


@app.async_command("runs")
async def scheduled_task_runs(
    *,
    project_id: ProjectIdOption,
    task_id: Annotated[
        str,
        typer.Argument(help="Scheduled task id"),
    ],
    count: Annotated[
        int, typer.Option("--count", help="Maximum number of runs to return")
    ] = 100,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of runs to return", hidden=True),
    ] = 100,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Row offset for pagination"),
    ] = 0,
    o: OutputFormatOption = "table",
):
    """List runs for a scheduled task."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        try:
            runs = await client.list_scheduled_task_runs(
                project_id=project_id,
                task_id=task_id,
                limit=count if count != 100 else limit,
                offset=offset,
            )
        except NotFoundError:
            print(f"[red]Scheduled task not found:[/] {task_id}")
            raise typer.Exit(code=1)

        if o == "json":
            print(json.dumps({"runs": _scheduled_task_run_records(runs)}, indent=2))
            return

        if len(runs) == 0:
            print("There are not currently any runs for this scheduled task")
            return

        print_json_table(
            _scheduled_task_run_table_rows(runs),
            "id",
            "target",
            "status",
            "attempt_count",
            "scheduled_time",
            "timeout_at",
            "started_at",
            "lease_expires_at",
            "completed_at",
            "container_id",
            "error",
        )
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
