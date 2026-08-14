# rooms_cli.py (add to the same module or import into your CLI package)

import json
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.api import RoomException
from meshagent.api.client import Room
from meshagent.api.participant_token import ApiScope
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.local_settings import get_active_user_id

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
        raise RoomException("You must provide either --id or a room name.")
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


async def _list_my_rooms(
    account_client,
    *,
    project_id: str,
    limit: int,
    offset: int,
    filter: Optional[str],
) -> list[Room]:
    return await _list_rooms_view(
        account_client,
        project_id=project_id,
        view="my",
        limit=limit,
        offset=offset,
        filter=filter,
    )


async def _list_rooms_view(
    account_client,
    *,
    project_id: str,
    view: str,
    limit: int,
    offset: int,
    filter: Optional[str],
) -> list[Room]:
    rooms: list[Room] = []
    continuation_token: str | None = None
    target_count = offset + limit
    # The "my" view is paged in access-object order, unlike the name-ordered
    # inventory view, so it must be exhausted before sorting and slicing.
    fetch_all = view == "my"
    while fetch_all or len(rooms) < target_count:
        page = await account_client.list_rooms_page(
            project_id=project_id,
            page_size=(
                100 if fetch_all else min(max(target_count - len(rooms), 1), 100)
            ),
            continuation_token=continuation_token,
            filter=filter,
            view=view,
        )
        rooms.extend(page.rooms)
        continuation_token = page.continuation_token
        if continuation_token is None:
            break
    rooms.sort(key=lambda room: room.name.lower())
    return rooms[offset:target_count]


# ---------------------------
# Commands
# ---------------------------


@app.async_command(
    "create",
    help="Create a room in the project.",
)
async def room_create_command(
    name: Annotated[Optional[str], typer.Argument(help="Room name")] = None,
    *,
    project_id: ProjectIdOption,
    name_option: Annotated[
        Optional[str],
        typer.Option("--name", help="Room name", hidden=True),
    ] = None,
    owner: Annotated[
        bool,
        typer.Option(
            "--owner/--no-owner",
            help="Add the active user as the room owner.",
        ),
    ] = True,
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
        room_name = name if name is not None else name_option
        if room_name is None or room_name.strip() == "":
            raise RoomException("Room name is required.")

        meta_obj = _maybe_parse_json("metadata", metadata)
        annotations_obj = _maybe_parse_string_dict_json("annotations", annotations)
        permissions: dict[str, ApiScope] | None = None
        if owner:
            active_user_id = get_active_user_id()
            if active_user_id is None:
                raise RoomException(
                    "Unable to determine the active user for the owner grant. "
                    "Run `meshagent auth login` or pass --no-owner."
                )
            permissions = {active_user_id: ApiScope.full()}

        print(f"[bold green]Creating room {room_name}[/bold green]")
        room = await account_client.create_room(
            project_id=project_id,
            name=room_name,
            if_not_exists=if_not_exists,
            metadata=meta_obj,
            annotations=annotations_obj,
            permissions=permissions,
        )

        print(
            json.dumps(
                {
                    "id": room.id,
                    "name": room.name,
                    "metadata": room.metadata,
                    "annotations": room.annotations,
                    "enabled": room.enabled,
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
    new_name: Annotated[Optional[str], typer.Option(help="New room name")] = None,
    enabled: Annotated[
        Optional[bool],
        typer.Option("--enabled/--disabled", help="Enable or disable the room"),
    ] = None,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            help='Optional JSON object for room annotations, e.g. {"meshagent.storage.class":"ephemeral"}',
        ),
    ] = None,
):
    """
    Update a room's name or enabled state (ID is preferred; name will be resolved to ID if needed).
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(name) if name else None
        rid = await _resolve_room_id_or_fail(
            account_client, project_id=project_id, room_id=id, room_name=room_name
        )
        annotations_obj = _maybe_parse_string_dict_json("annotations", annotations)
        if new_name is None and annotations_obj is None and enabled is None:
            raise RoomException(
                "Provide at least one of --new-name, --annotations, --enabled, or --disabled."
            )
        if new_name is None:
            current_room = await account_client.get_room(
                project_id=project_id, name=rid
            )
            new_name = current_room.name

        print(f"[bold green]Updating room id={rid}...[/bold green]")
        await account_client.update_room(
            project_id=project_id,
            room_id=rid,
            name=new_name,
            annotations=annotations_obj,
            enabled=enabled,
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
    count: Annotated[
        int, typer.Option("--count", help="Max rooms to return", min=1, max=500)
    ] = 100,
    limit: Annotated[
        Optional[int],
        typer.Option(
            "--limit", help="Max rooms to return", min=1, max=500, hidden=True
        ),
    ] = None,
    offset: Annotated[int, typer.Option(help="Offset for pagination", min=0)] = 0,
    order_by: Annotated[
        str, typer.Option(help='Order rooms by name; only "room_name" is supported')
    ] = "room_name",
    filter: Annotated[
        Optional[str],
        typer.Option("--filter", help="Lowercase contains filter for room names"),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Show all rooms in the project instead of only rooms you can access",
        ),
    ] = False,
):
    """
    List rooms in the project.
    """
    if order_by != "room_name":
        raise typer.BadParameter(
            'Only "room_name" is supported.', param_hint="--order-by"
        )

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_limit = limit if limit is not None else count
        if show_all:
            rooms = await _list_rooms_view(
                account_client,
                project_id=project_id,
                view="all",
                limit=room_limit,
                offset=offset,
                filter=filter,
            )
        else:
            rooms = await _list_my_rooms(
                account_client,
                project_id=project_id,
                limit=room_limit,
                offset=offset,
                filter=filter,
            )
        output = [
            {
                "id": r.id,
                "name": r.name,
                "metadata": r.metadata,
                "annotations": r.annotations,
                "enabled": r.enabled,
            }
            for r in rooms
        ]
        if o == "json":
            print(json.dumps(output, indent=2))
        elif output:
            print_json_table(output, "id", "name", "enabled")
        else:
            print("No rooms found.")
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()


@app.async_command("get")
async def room_get_command(
    room: Annotated[Optional[str], typer.Argument(help="Room name or ID")] = None,
    *,
    project_id: ProjectIdOption,
    name: Annotated[
        Optional[str],
        typer.Option("--name", help="Room name", hidden=True),
    ] = None,
):
    """
    Get a single room by name or ID.
    """
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_selector = room if room is not None else name
        room_name = resolve_room(room_selector)

        print(f"[bold green]Fetching room '{room_name}'...[/bold green]")
        r = await account_client.get_room(project_id=project_id, name=room_name)
        print(
            json.dumps(
                {
                    "id": r.id,
                    "name": r.name,
                    "metadata": r.metadata,
                    "annotations": r.annotations,
                    "enabled": r.enabled,
                },
                indent=2,
            )
        )
    except RoomException as ex:
        print(f"[red]{ex}[/red]")
        raise typer.Exit(1)
    finally:
        await account_client.close()
