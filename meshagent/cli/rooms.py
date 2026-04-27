# rooms_cli.py (add to the same module or import into your CLI package)

import json
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)
from meshagent.api import RoomException

app = async_typer.AsyncTyper(help="Create, list, and manage rooms in a project")

# ---------------------------
# Helpers
# ---------------------------


async def _resolve_room_id_or_fail(
    account_client, *, project_id: str, room_id: Optional[str], room_name: Optional[str]
) -> str:
    """
    If room_id is provided, return it.
    Else, resolve via room_name -> account_client.get_room(...).id
    """
    if room_id:
        return room_id
    if not room_name:
        raise RoomException("You must provide either --id or --name.")
    room = await account_client.get_room(project_id=project_id, name=room_name)
    return room.id


def _maybe_parse_json(label: str, s: Optional[str]):
    if s is None:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise RoomException(f"Invalid {label} JSON: {e}") from e


def _maybe_parse_string_dict_json(
    label: str, s: Optional[str]
) -> Optional[dict[str, str]]:
    if s is None:
        return None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as e:
        raise RoomException(f"Invalid {label} JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise RoomException(f"Invalid {label} JSON: expected an object")

    output: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str):
            raise RoomException(f"Invalid {label} JSON: all keys must be strings")
        if not isinstance(v, str):
            raise RoomException(f"Invalid {label} JSON: all values must be strings")
        output[k] = v
    return output


# ---------------------------
# Commands
# ---------------------------


@app.async_command(
    "create",
    help=(
        "Create a room in the project. Use this before meshagent deploy --room "
        "when the room does not already exist, then deploy with "
        "'meshagent deploy PATH --room <room> --tag <tag> --public --domain <domain>'."
    ),
)
async def room_create_command(
    *,
    project_id: ProjectIdOption,
    name: Annotated[str, typer.Option(..., help="Room name")],
    if_not_exists: Annotated[
        bool, typer.Option(help="Do not error if the room already exists")
    ] = False,
    metadata: Annotated[
        Optional[str], typer.Option(help="Optional JSON object for room metadata")
    ] = None,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            help='Optional JSON object for room annotations, e.g. {"meshagent.storage.class":"ephemeral"}',
        ),
    ] = None,
):
    """Create a room in the project."""
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        meta_obj = _maybe_parse_json("metadata", metadata)
        annotations_obj = _maybe_parse_string_dict_json("annotations", annotations)

        print(f"[bold green]Creating room {name}[/bold green]")
        room = await account_client.create_room(
            project_id=project_id,
            name=name,
            if_not_exists=if_not_exists,
            metadata=meta_obj,
            annotations=annotations_obj,
        )

        print(
            json.dumps(
                {
                    "id": room.id,
                    "name": room.name,
                    "metadata": room.metadata,
                    "annotations": room.annotations,
                },
                indent=2,
            )
        )

    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("delete")
async def room_delete_command(
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Room ID (preferred)")] = None,
    name: Optional[str] = None,
):
    """
    Delete a room by ID (or by name if --name is supplied).
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(name) if name else None
        rid = await _resolve_room_id_or_fail(
            account_client, project_id=project_id, room_id=id, room_name=room_name
        )

        print(f"[bold yellow]Deleting room id={rid}...[/bold yellow]")
        await account_client.delete_room(project_id=project_id, room_id=rid)
        print("[bold cyan]Room deleted.[/bold cyan]")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("update")
async def room_update_command(
    *,
    project_id: ProjectIdOption,
    id: Annotated[Optional[str], typer.Option(help="Room ID (preferred)")] = None,
    name: Optional[str] = None,
    new_name: Annotated[str, typer.Option(..., help="New room name")],
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            help='Optional JSON object for room annotations, e.g. {"meshagent.storage.class":"ephemeral"}',
        ),
    ] = None,
):
    """
    Update a room's name (ID is preferred; name will be resolved to ID if needed).
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(name) if name else None
        rid = await _resolve_room_id_or_fail(
            account_client, project_id=project_id, room_id=id, room_name=room_name
        )
        annotations_obj = _maybe_parse_string_dict_json("annotations", annotations)

        print(
            f"[bold green]Updating room id={rid} -> name='{new_name}'...[/bold green]"
        )
        await account_client.update_room(
            project_id=project_id,
            room_id=rid,
            name=new_name,
            annotations=annotations_obj,
        )
        print("[bold cyan]Room updated.[/bold cyan]")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("list")
async def room_list_command(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
    limit: Annotated[
        int, typer.Option(help="Max rooms to return", min=1, max=500)
    ] = 50,
    offset: Annotated[int, typer.Option(help="Offset for pagination", min=0)] = 0,
    order_by: Annotated[
        str, typer.Option(help='Order by column (e.g. "room_name", "created_at")')
    ] = "room_name",
):
    """
    List rooms in the project.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        rooms = await account_client.list_rooms(
            project_id=project_id,
            limit=limit,
            offset=offset,
            order_by=order_by,
        )
        output = [
            {
                "id": r.id,
                "name": r.name,
                "metadata": r.metadata,
                "annotations": r.annotations,
            }
            for r in rooms
        ]
        if o == "json":
            print(json.dumps(output, indent=2))
        elif output:
            print_json_table(output, "id", "name")
        else:
            print("No rooms found.")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("get")
async def room_get_command(
    *,
    project_id: ProjectIdOption,
    name: Optional[str] = None,
):
    """
    Get a single room by name (handy for resolving the ID).
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(name)

        print(f"[bold green]Fetching room '{room_name}'...[/bold green]")
        r = await account_client.get_room(project_id=project_id, name=room_name)
        print(
            json.dumps(
                {
                    "id": r.id,
                    "name": r.name,
                    "metadata": r.metadata,
                    "annotations": r.annotations,
                },
                indent=2,
            )
        )
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()
