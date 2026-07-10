import inspect
from collections.abc import AsyncIterator
from typing import Optional, TypedDict

import typer
from rich.console import Console
from rich.table import Table

from meshagent.api import ApiScope, ParticipantToken, RoomClient
from meshagent.api.specs.service import (
    ANNOTATION_SERVICE_README,
    ConfigMountSpec,
    ContainerMountSpec,
    EmptyDirMountSpec,
    ImageStorageMountSpec,
    RoomStorageMountSpec,
    ServiceSpec,
)
from meshagent.agents.context import AgentSessionContext
from meshagent.api.client import (
    AccessResource,
    AccessSubject,
    Meshagent,
    RoomConnectionInfo,
)
from meshagent.cli import async_typer, auth_async
from meshagent.cli.local_settings import (
    get_active_api_key as get_active_api_key_from_settings,
    get_active_project as get_active_project_from_settings,
    get_llm_proxy_bearer_token as get_llm_proxy_bearer_token_from_settings,
    load_settings,
    resolve_api_url,
    set_active_api_key as set_active_api_key_in_settings,
    set_active_project as set_active_project_in_settings,
    set_llm_proxy_bearer_token as set_llm_proxy_bearer_token_in_settings,
)
from meshagent.openai.tools.responses_adapter import ShellTool
from meshagent.tools import (
    ContainerShellTool,
    ProcessShellTool,
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

DEFAULT_SHELL_IMAGE = "meshagent/python:default"
DEFAULT_DATASET_NAMESPACE = (".datasets",)


class NormalizedRequiredToolOptions(TypedDict):
    toolkit: list[str]
    schema: list[str]
    require_image_generation: str | None
    require_computer_use: bool
    require_shell: bool
    require_advanced_shell: bool
    require_apply_patch: bool
    require_web_search: bool
    require_web_fetch: bool
    mcp: bool
    require_storage: bool


DEPRECATED_REQUIRE_OPTION_ALIASES = {
    "--toolkit": "--require-toolkit",
    "--require-schema": "--schema",
    "--require-image-generation": "--image-generation",
    "--require-computer-use": "--computer-use",
    "--require-shell": "--shell",
    "--require-advanced-shell": "--advanced-shell",
    "--require-apply-patch": "--apply-patch",
    "--require-web-search": "--web-search",
    "--require-web-fetch": "--web-fetch",
    "--require-mcp": "--mcp",
    "--require-storage": "--storage",
    "--require-read-only-storage": "--read-only-storage",
    "--require-time": "--time",
    "--require-uuid": "--uuid",
    "--require-table-read": "--table-read",
    "--require-table-write": "--table-write",
    "--require-discovery": "--discovery",
}

DUPLICATE_REQUIRE_OPTION_NAMES = {
    "require_schema",
    "require_image_generation",
    "require_shell",
    "require_apply_patch",
    "require_web_search",
    "require_web_fetch",
    "require_mcp",
    "require_storage",
}


def strip_command_options(
    app: typer.Typer, *, option_names: set[str], command_names: set[str] | None = None
) -> None:
    for command_info in app.registered_commands:
        command_name = command_info.name
        if command_names is not None and command_name not in command_names:
            continue

        callback = command_info.callback
        if callback is None:
            continue

        signature = inspect.signature(callback)
        callback.__signature__ = inspect.Signature(
            parameters=[
                parameter
                for parameter in signature.parameters.values()
                if parameter.name not in option_names
            ],
            return_annotation=signature.return_annotation,
        )


def normalize_required_tool_options(
    *,
    toolkit: list[str] | None = None,
    require_toolkit: list[str] | None = None,
    schema: list[str] | None = None,
    require_schema: list[str] | None = None,
    image_generation: str | None = None,
    require_image_generation: str | None = None,
    computer_use: bool | None = None,
    require_computer_use: bool | None = None,
    shell: bool | None = None,
    require_shell: bool | None = None,
    advanced_shell: bool | None = None,
    require_advanced_shell: bool | None = None,
    apply_patch: bool | None = None,
    require_apply_patch: bool | None = None,
    web_search: bool | None = None,
    require_web_search: bool | None = None,
    web_fetch: bool | None = None,
    require_web_fetch: bool | None = None,
    mcp: bool | None = None,
    require_mcp: bool | None = None,
    storage: bool | None = None,
    require_storage: bool | None = None,
) -> NormalizedRequiredToolOptions:
    return {
        "toolkit": [*(require_toolkit or []), *(toolkit or [])],
        "schema": [*(require_schema or []), *(schema or [])],
        "require_image_generation": require_image_generation or image_generation,
        "require_computer_use": bool(require_computer_use or computer_use),
        "require_shell": bool(require_shell or shell),
        "require_advanced_shell": bool(require_advanced_shell or advanced_shell),
        "require_apply_patch": bool(require_apply_patch or apply_patch),
        "require_web_search": bool(require_web_search or web_search),
        "require_web_fetch": bool(require_web_fetch or web_fetch),
        "mcp": bool(mcp or require_mcp),
        "require_storage": bool(require_storage or storage),
    }


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
    room: RoomClient | None = None,
    model: str,
    llm_participant: Optional[str] = None,
    name: str = "shell",
    working_dir: Optional[str] = None,
    image: Optional[str] = DEFAULT_SHELL_IMAGE,
    mounts: Optional[ContainerMountSpec] = DEFAULT_CONTAINER_MOUNT_SPEC,
    env: Optional[dict[str, str]] = None,
) -> ShellTool | ContainerShellTool | ProcessShellTool:
    if supports_openai_shell_tool(model=model, llm_participant=llm_participant):
        return ShellTool(
            room=room,
            name=name,
            working_dir=working_dir,
            image=image,
            mounts=mounts,
            env=env,
        )

    if image is None:
        return ProcessShellTool(
            name=name,
            working_dir=working_dir,
            env=env,
        )

    return ContainerShellTool(
        room=room,
        name=name,
        working_dir=working_dir,
        image=image,
        mounts=mounts,
        env=env,
    )


def _load_settings():
    return load_settings()


def get_active_project_sync() -> str | None:
    return get_active_project_from_settings()


async def get_active_project():
    return get_active_project_sync()


async def set_active_project(project_id: str | None):
    set_active_project_in_settings(project_id)


async def set_active_api_key(project_id: str, key: str):
    set_active_api_key_in_settings(project_id, key)


async def get_active_api_key(project_id: str):
    key = get_active_api_key_from_settings(project_id)
    # Ignore old keys, API key format changed
    if key is not None and key.startswith("ma-"):
        return key
    else:
        return None


async def get_llm_proxy_bearer_token() -> str | None:
    return get_llm_proxy_bearer_token_from_settings()


async def set_llm_proxy_bearer_token(token: str | None) -> None:
    set_llm_proxy_bearer_token_in_settings(token)


app = async_typer.AsyncTyper()


class CustomMeshagentClient(Meshagent):
    async def can_create_rooms(self, project_id: str) -> bool:
        result = await self.test_access(
            project_id=project_id,
            subject=AccessSubject(type="user", id="me"),
            resource=AccessResource(type="project", id=project_id),
            relation="room_creator",
        )
        return result.allowed

    async def can_use_llm_proxy(self, project_id: str) -> bool:
        result = await self.test_access(
            project_id=project_id,
            subject=AccessSubject(type="user", id="me"),
            resource=AccessResource(type="project", id=project_id),
            relation="llm_proxy_user",
        )
        return result.allowed

    async def connect_room(self, *, project_id: str, room: str) -> RoomConnectionInfo:
        from urllib.parse import quote

        jwt = os.getenv("MESHAGENT_TOKEN")

        if jwt is not None and room == os.getenv("MESHAGENT_ROOM"):
            return RoomConnectionInfo(
                jwt=jwt,
                room_name=room,
                project_id=os.getenv("MESHAGENT_PROJECT_ID"),
                room_url=resolve_api_url() + f"/rooms/{quote(room)}",
            )

        return await super().connect_room(project_id=project_id, room=room)


async def get_client(*, api_url: str | None = None):
    resolved_api_url = resolve_api_url(api_url=api_url)
    key = os.getenv("MESHAGENT_API_KEY") or os.getenv("MESHAGENT_TOKEN")
    if key is not None or os.getenv("MESHAGENT_SESSION_ID") is not None:
        return CustomMeshagentClient(
            base_url=resolved_api_url,
            token=key,
        )
    else:
        access_token = await auth_async.get_access_token()
        return CustomMeshagentClient(
            base_url=resolved_api_url,
            token=access_token,
        )


def print_json_table(records: list, *cols, empty: str = "No rows to print"):
    if not records:
        raise SystemExit(empty)

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
            "[red]--key is required if MESHAGENT_API_KEY is not set. "
            "You can use meshagent service-account api-key create to create a new API key.[/red]"
        )
        raise typer.Exit(1)

    return key


async def mint_participant_token_for_cli(
    *,
    project_id: str | None,
    name: str,
    room_name: str | None = None,
    role: str | None = None,
    api_scope: ApiScope | None = None,
    grants: list[dict] | None = None,
    key: str | None = None,
) -> str:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    signing_key = key
    if signing_key is None:
        signing_key = await get_active_api_key(resolved_project_id)
    if signing_key is None:
        signing_key = os.getenv("MESHAGENT_API_KEY")

    signing_secret = os.getenv("MESHAGENT_SECRET")
    if signing_key is not None or signing_secret:
        if grants is not None:
            token = ParticipantToken.from_json({"name": name, "grants": grants})
            token.project_id = resolved_project_id
        else:
            token = ParticipantToken(name=name, project_id=resolved_project_id)
            if role is not None and role != "user":
                token.add_role_grant(role)
            if api_scope is not None:
                token.add_api_grant(api_scope)
            if room_name is not None:
                token.add_room_grant(room_name)
        if signing_key is not None:
            return token.to_jwt(api_key=signing_key)
        return token.to_jwt(token=signing_secret)

    client = await get_client()
    try:
        mint_kwargs = {
            "name": name,
            "room_name": room_name,
            "role": role,
            "api": api_scope.model_dump(mode="json") if api_scope is not None else None,
        }
        if grants is not None:
            mint_kwargs = {"name": name, "grants": grants}
        return await client.mint_participant_token(
            resolved_project_id,
            **mint_kwargs,
        )
    finally:
        await client.close()


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


def resolve_dataset_namespace(
    *,
    namespace: Optional[str],
    default_namespace: tuple[str, ...] | None = None,
) -> Optional[list[str]]:
    if namespace is None:
        if default_namespace is None:
            return None
        return list(default_namespace)

    return namespace.split("::")


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


def split_config_mount(value: str, option_name: str) -> str:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter(f"{option_name} cannot be empty")
    if ":" in cleaned:
        raise typer.BadParameter(f"{option_name} must be in the form '<mount>'")
    return cleaned


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
    room: RoomClient,
    local_paths: list[str],
    room_paths: list[str],
    default_room_mount: bool = False,
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
            StorageToolRoomMount(
                path=mount,
                subpath=subpath,
                read_only=read_only,
                room=room,
            )
        )

    if len(mounts) == 0 and default_room_mount:
        mounts.append(StorageToolRoomMount(path="/", room=room))
    return mounts or None


def parse_shell_tool_mounts(
    *,
    room_paths: list[str],
    project_paths: Optional[list[str]] = None,
    image_paths: Optional[list[str]] = None,
    empty_dir_paths: Optional[list[str]] = None,
    config_paths: Optional[list[str]] = None,
) -> Optional[ContainerMountSpec]:
    if project_paths:
        raise typer.BadParameter("project storage mounts are no longer supported")
    room_mounts: list[RoomStorageMountSpec] = []
    image_mounts: list[ImageStorageMountSpec] = []
    empty_dir_mounts: list[EmptyDirMountSpec] = []
    config_mounts: list[ConfigMountSpec] = []

    if image_paths is None:
        image_paths = []
    if empty_dir_paths is None:
        empty_dir_paths = []
    if config_paths is None:
        config_paths = []

    for value in room_paths:
        source, mount, read_only = split_container_mount(
            value, "--shell-tool-room-path", False
        )
        subpath = source if source not in {"", ".", "/"} else None
        room_mounts.append(
            RoomStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
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

    for value in config_paths:
        mount = split_config_mount(value, "--shell-tool-config-mount")
        config_mounts.append(ConfigMountSpec(path=mount))

    if (
        not room_mounts
        and not image_mounts
        and not empty_dir_mounts
        and not config_mounts
    ):
        return None

    return ContainerMountSpec(
        room=room_mounts or None,
        images=image_mounts or None,
        empty_dirs=empty_dir_mounts or None,
        configs=config_mounts or None,
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
