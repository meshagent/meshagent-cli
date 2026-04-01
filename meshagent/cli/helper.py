from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from meshagent.api import RoomClient
from meshagent.api.helpers import meshagent_base_url
from meshagent.api.specs.service import (
    ANNOTATION_SERVICE_README,
    ContainerMountSpec,
    EmptyDirMountSpec,
    ImageStorageMountSpec,
    ProjectStorageMountSpec,
    RoomStorageMountSpec,
    ServiceSpec,
)
from meshagent.agents.context import AgentSessionContext
from meshagent.api.client import Meshagent, RoomConnectionInfo
from meshagent.cli import async_typer, auth_async
from meshagent.openai.tools.responses_adapter import ShellConfig, ShellTool
from meshagent.tools import (
    ContainerShellTool,
    ProcessShellTool,
    Toolkit,
    ToolkitBuilder,
)
from meshagent.tools.container_shell import DEFAULT_CONTAINER_MOUNT_SPEC
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
DEFAULT_SHELL_IMAGE = "python:3.13"


def _ensure_cache_dir():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


def resolve_shell_image(shell_image: Optional[str]) -> Optional[str]:
    if shell_image is None:
        return DEFAULT_SHELL_IMAGE

    normalized = shell_image.strip()
    if normalized == "":
        return DEFAULT_SHELL_IMAGE

    if normalized.lower() == "none":
        return None

    return normalized


def supports_openai_shell_tool(
    *, model: str, llm_participant: Optional[str] = None
) -> bool:
    return llm_participant is None and model.startswith("gpt-")


def build_shell_tool(
    *,
    model: str,
    llm_participant: Optional[str] = None,
    config: Optional[ShellConfig] = None,
    working_dir: Optional[str] = None,
    image: Optional[str] = DEFAULT_SHELL_IMAGE,
    mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
    env: Optional[dict[str, str]] = None,
) -> ShellTool | ContainerShellTool | ProcessShellTool:
    if config is None:
        config = ShellConfig(name="shell")

    if supports_openai_shell_tool(model=model, llm_participant=llm_participant):
        return ShellTool(
            config=config,
            working_dir=working_dir,
            image=image,
            mounts=mounts,
            env=env,
        )

    if image is None:
        return ProcessShellTool(
            name=config.name,
            working_dir=working_dir,
            env=env,
        )

    return ContainerShellTool(
        name=config.name,
        working_dir=working_dir,
        image=image,
        mounts=mounts,
        env=env,
    )


class _RuntimeAwareShellToolkitBuilder(ToolkitBuilder):
    def __init__(
        self,
        *,
        llm_participant: Optional[str] = None,
        working_dir: Optional[str] = None,
        image: Optional[str] = DEFAULT_SHELL_IMAGE,
        mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(name="shell", type=ShellConfig)
        self.llm_participant = llm_participant
        self.working_dir = working_dir
        self.image = image
        self.mounts = mounts
        self.env = env

    async def make(
        self, *, room: RoomClient, model: str, config: ShellConfig
    ) -> Toolkit:
        del room
        return Toolkit(
            name="shell",
            tools=[
                build_shell_tool(
                    model=model,
                    llm_participant=self.llm_participant,
                    config=config,
                    working_dir=self.working_dir,
                    image=self.image,
                    mounts=self.mounts,
                    env=self.env,
                )
            ],
        )


def build_shell_toolkit_builder(
    *,
    llm_participant: Optional[str] = None,
    working_dir: Optional[str] = None,
    image: Optional[str] = DEFAULT_SHELL_IMAGE,
    mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
    env: Optional[dict[str, str]] = None,
) -> ToolkitBuilder:
    return _RuntimeAwareShellToolkitBuilder(
        llm_participant=llm_participant,
        working_dir=working_dir,
        image=image,
        mounts=mounts,
        env=env,
    )


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


async def init_context_from_spec(context: AgentSessionContext) -> None:
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

        annotations = spec.metadata.annotations or {}
        readme = annotations.get(ANNOTATION_SERVICE_README)

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


async def upload_room_bytes_stream(
    *,
    room: RoomClient,
    path: str,
    data: bytes,
    overwrite: bool = False,
    name: str | None = None,
    mime_type: str | None = None,
) -> None:
    async def chunk_stream() -> AsyncIterator[bytes]:
        yield data

    await room.storage.upload_stream(
        path=path,
        chunks=chunk_stream(),
        overwrite=overwrite,
        size=len(data),
        name=name,
        mime_type=mime_type,
    )


def parse_memory_selector(value: str) -> tuple[str, Optional[list[str]]]:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("--use-memory cannot be empty")

    segments = [segment.strip() for segment in cleaned.split("/")]
    if any(segment == "" for segment in segments):
        raise typer.BadParameter(
            "--use-memory must be '<name>' or '<namespace>/<name>' with no empty segments"
        )

    memory_name = segments[-1]
    namespace = segments[:-1]
    return memory_name, namespace or None


def merge_option_lists(*option_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for values in option_lists:
        merged.extend(values)
    return merged


def _split_mount_value(
    value: str, option_name: str, default_read_only: bool
) -> tuple[str, str, bool]:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter(f"{option_name} cannot be empty")

    read_only = default_read_only
    parts = cleaned.rsplit(":", 2)
    if len(parts) == 3 and parts[2].lower() in {"ro", "rw"}:
        cleaned = f"{parts[0]}:{parts[1]}"
        read_only = parts[2].lower() == "ro"

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


def split_storage_mount(value: str, option_name: str) -> tuple[str, str, bool]:
    return _split_mount_value(value, option_name, False)


def split_container_mount(
    value: str, option_name: str, default_read_only: bool
) -> tuple[str, str, bool]:
    return _split_mount_value(value, option_name, default_read_only)


def split_empty_dir_mount(value: str, option_name: str) -> tuple[str, bool]:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter(f"{option_name} cannot be empty")

    read_only = False
    mount = cleaned
    parts = cleaned.rsplit(":", 1)
    if len(parts) == 2:
        suffix = parts[1].lower()
        if suffix in {"ro", "rw"}:
            mount = parts[0].strip()
            read_only = suffix == "ro"
        else:
            raise typer.BadParameter(
                f"{option_name} must be in the form '<mount>[:ro|rw]'"
            )

    if mount == "":
        raise typer.BadParameter(f"{option_name} must include a mount path")
    if ":" in mount:
        raise typer.BadParameter(f"{option_name} must be in the form '<mount>[:ro|rw]'")

    return mount, read_only


def split_image_mount(
    value: str, option_name: str
) -> tuple[str, str, Optional[str], bool]:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter(f"{option_name} cannot be empty")

    if "=" not in cleaned:
        raise typer.BadParameter(
            f"{option_name} must be in the form '<image>=<mount>[:ro|rw]'"
        )

    image, remainder = (part.strip() for part in cleaned.split("=", 1))
    if image == "" or remainder == "":
        raise typer.BadParameter(
            f"{option_name} must include both image and mount paths"
        )

    read_only = True
    subpath: Optional[str] = None
    parts = [part.strip() for part in remainder.split(":")]
    if len(parts) == 1:
        mount = parts[0]
    elif len(parts) == 2:
        if parts[1].lower() in {"ro", "rw"}:
            mount = parts[0]
            read_only = parts[1].lower() == "ro"
        else:
            raise typer.BadParameter(
                f"{option_name} must be in the form '<image>=<mount>[:ro|rw]'"
            )
    else:
        raise typer.BadParameter(
            f"{option_name} must be in the form '<image>=<mount>[:ro|rw]'"
        )

    if mount == "":
        raise typer.BadParameter(
            f"{option_name} must include both image and mount paths"
        )

    return image, mount, subpath, read_only


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


def parse_shell_tool_mounts(
    *,
    room_paths: list[str],
    project_paths: list[str],
    image_paths: Optional[list[str]] = None,
    empty_dir_paths: Optional[list[str]] = None,
) -> Optional[ContainerMountSpec]:
    room_mounts: list[RoomStorageMountSpec] = []
    project_mounts: list[ProjectStorageMountSpec] = []
    image_mounts: list[ImageStorageMountSpec] = []
    empty_dir_mounts: list[EmptyDirMountSpec] = []

    if image_paths is None:
        image_paths = []
    if empty_dir_paths is None:
        empty_dir_paths = []

    for value in room_paths:
        source, mount, read_only = split_container_mount(
            value, "--shell-tool-room-path", False
        )
        subpath = source if source not in {"", ".", "/"} else None
        room_mounts.append(
            RoomStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in project_paths:
        source, mount, read_only = split_container_mount(
            value, "--shell-tool-project-path", True
        )
        subpath = source if source not in {"", ".", "/"} else None
        project_mounts.append(
            ProjectStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in image_paths:
        image, mount, subpath, read_only = split_image_mount(
            value, "--shell-image-mount"
        )

        image_mounts.append(
            ImageStorageMountSpec(
                image=image,
                path=mount,
                subpath=subpath,
                read_only=read_only,
            )
        )

    for value in empty_dir_paths:
        mount, read_only = split_empty_dir_mount(value, "--shell-tool-empty-dir")
        empty_dir_mounts.append(
            EmptyDirMountSpec(
                path=mount,
                read_only=read_only,
            )
        )

    if (
        not room_mounts
        and not project_mounts
        and not image_mounts
        and not empty_dir_mounts
    ):
        return None

    return ContainerMountSpec(
        room=room_mounts or None,
        project=project_mounts or None,
        images=image_mounts or None,
        empty_dirs=empty_dir_mounts or None,
    )


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


def cleanup_args_strip_options(args: list[str], options: list[str]) -> list[str]:
    option_set = set(options)
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in option_set:
            i += 2
            continue
        if any(arg.startswith(f"{option}=") for option in option_set):
            i += 1
            continue
        out.append(arg)
        i += 1
    return out
