import typer
from rich.console import Console
from rich.table import Table
from pydantic import BaseModel
from pathlib import Path
from typing import Optional
from meshagent.cli import auth_async
from meshagent.cli import async_typer
from meshagent.api.helpers import meshagent_base_url
from meshagent.api.specs.service import ServiceSpec
from meshagent.agents.context import AgentChatContext
from meshagent.api.client import Meshagent, RoomConnectionInfo
from meshagent.tools.storage import (
    StorageToolLocalMount,
    StorageToolMount,
    StorageToolRoomMount,
)
import os
import aiofiles
from pydantic_yaml import parse_yaml_raw_as
import json

from rich import print

SETTINGS_FILE = Path.home() / ".meshagent" / "project.json"


def _ensure_cache_dir():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


class Settings(BaseModel):
    active_project: Optional[str] = None
    active_api_keys: Optional[dict] = {}


def _save_settings(s: Settings):
    _ensure_cache_dir()
    SETTINGS_FILE.write_text(s.model_dump_json())


def _load_settings():
    try:
        _ensure_cache_dir()
        if SETTINGS_FILE.exists():
            return Settings.model_validate_json(SETTINGS_FILE.read_text())
    except OSError as ex:
        if ex.errno == 30:
            return Settings()
        else:
            raise


async def get_active_project():
    settings = _load_settings()
    if settings is None:
        return None
    return settings.active_project


async def set_active_project(project_id: str | None):
    settings = _load_settings() or Settings()
    settings.active_project = project_id
    _save_settings(settings)


async def set_active_api_key(project_id: str, key: str):
    settings = _load_settings() or Settings()
    settings.active_api_keys[project_id] = key
    _save_settings(settings)


async def get_active_api_key(project_id: str):
    settings = _load_settings()
    if settings is None:
        return None
    key: str = settings.active_api_keys.get(project_id)
    # Ignore old keys, API key format changed
    if key is not None and key.startswith("ma-"):
        return key
    else:
        return None


app = async_typer.AsyncTyper()


class CustomMeshagentClient(Meshagent):
    async def connect_room(self, *, project_id: str, room: str) -> RoomConnectionInfo:
        from urllib.parse import quote

        jwt = os.getenv("MESHAGENT_TOKEN")

        if jwt is not None and room == os.getenv("MESHAGENT_ROOM"):
            return RoomConnectionInfo(
                jwt=jwt,
                room_name=room,
                project_id=os.getenv("MESHAGENT_PROJECT_ID"),
                room_url=meshagent_base_url() + f"/rooms/{quote(room)}",
            )

        return await super().connect_room(project_id=project_id, room=room)


async def get_client():
    key = os.getenv("MESHAGENT_API_KEY") or os.getenv("MESHAGENT_TOKEN")
    if key is not None or os.getenv("MESHAGENT_SESSION_ID") is not None:
        return CustomMeshagentClient(
            base_url=meshagent_base_url(),
            token=key,
        )
    else:
        access_token = await auth_async.get_access_token()
        return CustomMeshagentClient(
            base_url=meshagent_base_url(),
            token=access_token,
        )


def print_json_table(records: list, *cols):
    if not records:
        raise SystemExit("No rows to print")

    # 2️⃣  --- build the table -------------------------------------------
    table = Table(show_header=True, header_style="bold magenta")

    if len(cols) > 0:
        # use the keys of the first object as column order
        for col in cols:
            table.add_column(col.title())  # "id" → "Id"

        for row in records:
            table.add_row(*(str(row.get(col, "")) for col in cols))

    else:
        # use the keys of the first object as column order
        for col in records[0]:
            table.add_column(col.title())  # "id" → "Id"

        for row in records:
            table.add_row(*(str(row.get(col, "")) for col in records[0]))

    # 3️⃣  --- render ------------------------------------------------------
    Console().print(table)


def resolve_room(room_name: Optional[str] = None):
    if room_name is None:
        room_name = os.getenv("MESHAGENT_ROOM")

    return room_name


async def resolve_project_id(project_id: Optional[str] = None):
    if project_id is None:
        project_id = os.getenv("MESHAGENT_PROJECT_ID") or await get_active_project()

    if project_id is None:
        print(
            "[red]Project ID not specified, activate a project or pass a project on the command line[/red]"
        )
        raise typer.Exit(code=1)

    return project_id


async def init_context_from_spec(context: AgentChatContext) -> None:
    path = os.getenv("MESHAGENT_SPEC_PATH")

    if path is None:
        return None

    async with aiofiles.open(path, "r") as file:
        spec_str = await file.read()
        try:
            json.loads(spec_str)
            spec = ServiceSpec.model_validate_json(spec_str)
        except ValueError:
            # fallback on yaml parser if spec can't
            spec = parse_yaml_raw_as(ServiceSpec, spec_str)

        readme = spec.metadata.annotations.get("meshagent.service.readme")

        if spec.metadata.description:
            context.append_assistant_message(
                f"This agent's description:\n{spec.metadata.description}"
            )

        if readme is not None:
            context.append_assistant_message(f"This agent's README:\n{readme}")


async def resolve_key(project_id: str | None, key: str | None):
    project_id = await resolve_project_id(project_id=project_id)
    if key is None:
        key = await get_active_api_key(project_id=project_id)

    if key is None:
        key = os.getenv("MESHAGENT_API_KEY")

    if key is None and os.getenv("MESHAGENT_TOKEN") is None:
        print(
            "[red]--key is required if MESHAGENT_API_KEY is not set. You can use meshagent api-key create to create a new api key."
        )
        raise typer.Exit(1)

    return key


def split_storage_mount(value: str, option_name: str) -> tuple[str, str, bool]:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter(f"{option_name} cannot be empty")

    read_only = False
    lowered = cleaned.lower()
    if lowered.endswith(":ro") or lowered.endswith(":rw"):
        cleaned, suffix = cleaned.rsplit(":", 1)
        read_only = suffix.lower() == "ro"

    parts = cleaned.rsplit(":", 1)
    if len(parts) != 2:
        raise typer.BadParameter(
            f"{option_name} must be in the form '<source>:<mount>[:ro|rw]'"
        )

    source, mount = (part.strip() for part in parts)
    if source == "" or mount == "":
        raise typer.BadParameter(
            f"{option_name} must include both source and mount paths"
        )

    return source, mount, read_only


def parse_storage_tool_mounts(
    *,
    local_paths: list[str],
    room_paths: list[str],
) -> Optional[list[StorageToolMount]]:
    mounts: list[StorageToolMount] = []

    for value in local_paths:
        source, mount, read_only = split_storage_mount(
            value, "--storage-tool-local-path"
        )
        mounts.append(
            StorageToolLocalMount(path=mount, local_path=source, read_only=read_only)
        )

    for value in room_paths:
        source, mount, read_only = split_storage_mount(
            value, "--storage-tool-room-path"
        )
        subpath = source if source not in {"", ".", "/"} else None
        mounts.append(
            StorageToolRoomMount(path=mount, subpath=subpath, read_only=read_only)
        )

    return mounts or None


def cleanup_args(args: list[str]):
    out = []
    i = 0
    while i < len(args):
        if args[i] == "--service-name":
            i += 1
        elif args[i] == "--service-title":
            i += 1
        elif args[i] == "--service-description":
            i += 1
        elif args[i] == "--project-id":
            i += 1
        elif args[i] == "--room":
            i += 1
        elif args[i].startswith("--service-name="):
            pass
        elif args[i].startswith("--service-title="):
            pass
        elif args[i].startswith("--service-description="):
            pass
        elif args[i].startswith("--project-id="):
            pass
        elif args[i].startswith("--room="):
            pass
        elif args[i] == "deploy":
            pass
        elif args[i] == "spec":
            pass
        else:
            out.append(args[i])
        i += 1
    return out
