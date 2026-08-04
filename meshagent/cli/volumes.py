import json
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.api import RoomException
from meshagent.api.client import StorageVolume
from meshagent.api.specs.service import (
    ANNOTATION_STORAGE_CAPACITY,
    ANNOTATION_STORAGE_CLASS,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)


app = async_typer.AsyncTyper(help="Manage durable storage volumes for a room")
_VOLUME_STORAGE_CLASSES = frozenset({"standard", "juice", "zerofs"})
_CAPACITY_STORAGE_CLASSES = frozenset({"juice", "zerofs"})


def _json_object(value: Optional[str], *, label: str) -> dict | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"invalid {label} JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{label} must be a JSON object")
    return parsed


def _volume_options(
    *,
    annotations: dict | None,
    volume_type: str | None,
    max_size_mb: int | None,
) -> tuple[dict[str, str], str, int | None]:
    values: dict[str, str] = {}
    for key, value in (annotations or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise typer.BadParameter("annotation keys and values must be strings")
        values[key] = value
    reserved = {ANNOTATION_STORAGE_CLASS, ANNOTATION_STORAGE_CAPACITY}.intersection(
        values
    )
    if reserved:
        raise typer.BadParameter(
            "use --type and --max-size instead of reserved storage annotations"
        )
    storage_class = (volume_type or "standard").strip().lower()
    if storage_class not in _VOLUME_STORAGE_CLASSES:
        valid = ", ".join(sorted(_VOLUME_STORAGE_CLASSES))
        raise typer.BadParameter(f"type must be one of: {valid}")
    if max_size_mb is not None:
        if storage_class not in _CAPACITY_STORAGE_CLASSES:
            raise typer.BadParameter(
                "--max-size is supported only for JuiceFS and ZeroFS volumes"
            )
        if max_size_mb <= 0:
            raise typer.BadParameter("--max-size must be a positive integer in MB")
    return values, storage_class, max_size_mb


def _volume_record(volume: StorageVolume) -> dict:
    record = volume.model_dump(mode="json")
    record["type"] = volume.storage_class
    record["max_size_mb"] = volume.max_size_mb
    return record


@app.async_command("list")
async def list_volumes_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output: OutputFormatOption = "table",
) -> None:
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise RoomException("--room is required")
        volumes = await client.list_room_volumes(
            project_id=project_id,
            room=room_name,
        )
        records = [_volume_record(volume) for volume in volumes]
        if output == "json":
            print(json.dumps(records, indent=2))
        elif records:
            print_json_table(
                records,
                "id",
                "name",
                "type",
                "max_size_mb",
                "provisioned",
                "reconcile_error",
            )
        else:
            print("No storage volumes found.")
    finally:
        await client.close()


@app.async_command("create")
async def create_volume_command(
    name: Annotated[str, typer.Argument(help="Volume name")],
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    description: Annotated[str, typer.Option(help="Volume description")] = "",
    metadata: Annotated[
        Optional[str],
        typer.Option(help="Volume metadata JSON object"),
    ] = None,
    annotations: Annotated[
        Optional[str],
        typer.Option(help="Volume annotations JSON object"),
    ] = None,
    volume_type: Annotated[
        Optional[str],
        typer.Option(
            "--type",
            help="Storage type: standard, juice, or zerofs",
        ),
    ] = None,
    max_size_mb: Annotated[
        Optional[int],
        typer.Option(
            "--max-size",
            help="Maximum volume size in MB (JuiceFS and ZeroFS only)",
        ),
    ] = None,
) -> None:
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise RoomException("--room is required")
        parsed_annotations, storage_class, max_size_mb = _volume_options(
            annotations=_json_object(annotations, label="annotations"),
            volume_type=volume_type,
            max_size_mb=max_size_mb,
        )
        volume = await client.create_room_volume(
            project_id=project_id,
            room=room_name,
            name=name,
            description=description,
            metadata=_json_object(metadata, label="metadata"),
            storage_class=storage_class,
            max_size_mb=max_size_mb,
            annotations=parsed_annotations,
        )
        print(json.dumps(volume.model_dump(mode="json"), indent=2))
    finally:
        await client.close()


@app.async_command("delete")
async def delete_volume_command(
    volume: Annotated[str, typer.Argument(help="Volume ID or name")],
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
) -> None:
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise RoomException("--room is required")
        volumes = await client.list_room_volumes(
            project_id=project_id,
            room=room_name,
        )
        selected = next(
            (
                candidate
                for candidate in volumes
                if candidate.id == volume or candidate.name == volume
            ),
            None,
        )
        if selected is None:
            raise RoomException(f"storage volume {volume!r} was not found")
        deleted = await client.delete_room_volume(
            project_id=project_id,
            room=room_name,
            volume_id=selected.id,
        )
        print(f"Storage volume {deleted.name!r} was marked for deletion.")
    finally:
        await client.close()


@app.async_command("expand")
async def expand_volume_command(
    volume: Annotated[str, typer.Argument(help="Volume ID or name")],
    *,
    max_size_mb: Annotated[
        int,
        typer.Option(
            "--max-size",
            help="New maximum volume size in MB",
        ),
    ],
    project_id: ProjectIdOption,
    room: RoomOption,
) -> None:
    if max_size_mb <= 0:
        raise typer.BadParameter("--max-size must be a positive integer in MB")
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)
        if room_name is None:
            raise RoomException("--room is required")
        volumes = await client.list_room_volumes(
            project_id=project_id,
            room=room_name,
        )
        selected = next(
            (
                candidate
                for candidate in volumes
                if candidate.id == volume or candidate.name == volume
            ),
            None,
        )
        if selected is None:
            raise RoomException(f"storage volume {volume!r} was not found")
        expanded = await client.expand_room_volume(
            project_id=project_id,
            room=room_name,
            volume_id=selected.id,
            max_size_mb=max_size_mb,
        )
        print(json.dumps(expanded.model_dump(mode="json"), indent=2))
    finally:
        await client.close()
