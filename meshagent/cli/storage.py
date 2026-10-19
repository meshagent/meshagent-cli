"""File commands shared by room, agent and top-level files entry points."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import fnmatch
import glob
import mimetypes
import os
from pathlib import Path
import posixpath
import shutil
import sys
import tempfile
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from rich import print
import typer

from meshagent.api import FilesTarget, Meshagent, TargetFilesClient
from meshagent.api.client import NotFoundError
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id, resolve_room

app = async_typer.AsyncTyper(help="Manage persistent room and managed-agent files")
RoomOption = Annotated[
    str | None, typer.Option("--room", help="Room name (defaults to MESHAGENT_ROOM)")
]
AgentOption = Annotated[str | None, typer.Option("--agent", help="Managed agent name")]
RecursiveOption = Annotated[
    bool, typer.Option("-r", "--recursive", help="Include directories recursively")
]


@dataclass(frozen=True)
class StoragePath:
    scheme: Literal["local", "room", "agent", "meshagent"]
    path: str
    target: FilesTarget | None = None


def parse_path(value: str) -> StoragePath:
    """Keep legacy room://path; qualified meshagent://project/rooms/name/path
    addresses another target without changing the command's default target.
    """
    for scheme in ("room", "agent"):
        if value.startswith(f"{scheme}://"):
            return StoragePath(scheme, value[len(scheme) + 3 :].strip("/"))
    if value.startswith("meshagent://"):
        uri = urlsplit(value)
        parts = uri.path.lstrip("/").split("/", 2)
        if uri.query or uri.fragment or not uri.netloc or len(parts) < 2:
            raise typer.BadParameter("Use meshagent://PROJECT/{rooms|agents}/NAME/PATH")
        namespace, name = parts[:2]
        if namespace not in {"rooms", "agents"}:
            raise typer.BadParameter("Files namespace must be rooms or agents")
        target = FilesTarget(
            project_id=unquote(uri.netloc),
            kind="room" if namespace == "rooms" else "agent",
            name=unquote(name),
        )
        return StoragePath(
            "meshagent", unquote(parts[2]).rstrip("/") if len(parts) > 2 else "", target
        )
    return StoragePath("local", value)


def split_glob_subpath(path: str) -> tuple[str, str | None]:
    if glob.has_magic(path):
        return posixpath.dirname(path), posixpath.basename(path)
    return path, None


class StorageContext:
    def __init__(self, project_id: str | None, room: str | None, agent: str | None):
        if room and agent:
            raise typer.BadParameter(
                "--room and --agent are mutually exclusive; use qualified meshagent:// paths for cross-target copies"
            )
        self.project_id = project_id
        self.room = room
        self.agent = agent
        self.client: Meshagent | None = None

    async def remote(self, path: StoragePath) -> TargetFilesClient:
        if self.client is None:
            self.client = await get_client()
        if path.target is not None:
            return self.client.files.for_target(path.target)
        project = await resolve_project_id(project_id=self.project_id)
        if path.scheme == "agent" or (path.scheme == "local" and self.agent):
            if not self.agent:
                raise typer.BadParameter("Provide --agent for agent:// paths")
            return self.client.files.for_agent(
                project_id=project, agent_name=self.agent
            )
        room = resolve_room(self.room)
        return self.client.files.for_room(project_id=project, room_name=room)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


@asynccontextmanager
async def storage_context(project_id: str | None, room: str | None, agent: str | None):
    context = StorageContext(project_id, room, agent)
    try:
        yield context
    finally:
        await context.close()


async def _remote_matches(
    files: TargetFilesClient, path: str, *, folders: bool = False
) -> list[str]:
    base, pattern = split_glob_subpath(path)
    if pattern is None:
        if not await files.exists(base):
            raise FileNotFoundError(f"Remote path '{path}' does not exist")
        return [base]
    return [
        posixpath.join(base, entry.name)
        async for entry in files.list(base)
        if (folders or not entry.is_folder) and fnmatch.fnmatchcase(entry.name, pattern)
    ]


async def _local_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as file:
        while chunk := await asyncio.to_thread(file.read, 256 * 1024):
            yield chunk


async def _download(files: TargetFilesClient, source: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as file:
            temporary = file.name
            async for chunk in files.download_stream(source):
                await asyncio.to_thread(file.write, chunk)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@app.async_command("exists", help="Check whether a path exists in room or agent files.")
async def storage_exists_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    path: str,
    agent: AgentOption = None,
):
    parsed = parse_path(path)
    async with storage_context(project_id, room, agent) as context:
        files = await context.remote(parsed)
        exists = await files.exists(parsed.path.strip("/"))
        print(f"'{path}' {'exists' if exists else 'does NOT exist'} in remote storage.")


async def _copy_local_tree(
    files: TargetFilesClient, source: Path, destination: str, recursive: bool
) -> None:
    if source.is_symlink() and source.is_dir():
        raise ValueError("Copying directory symlinks is not supported")
    if source.is_dir():
        if not recursive:
            raise typer.BadParameter("Use -r to copy a directory")
        if destination:
            await files.mkdir(destination)
        for child in sorted(source.iterdir()):
            await _copy_local_tree(
                files, child, posixpath.join(destination, child.name), True
            )
    else:
        await files.upload(
            destination,
            _local_chunks(source),
            content_type=mimetypes.guess_type(source.name)[0]
            or "application/octet-stream",
            overwrite=True,
        )


async def _copy_remote_tree(
    files: TargetFilesClient, source: str, destination: Path, recursive: bool
) -> None:
    metadata = await files.stat(source)
    if metadata.is_folder:
        if not recursive:
            raise typer.BadParameter("Use -r to copy a directory")
        destination.mkdir(parents=True, exist_ok=True)
        async for entry in files.list(source):
            # Never interpret a server-returned filename as a local path.
            if (
                entry.name in {"", ".", ".."}
                or "/" in entry.name
                or "\\" in entry.name
                or Path(entry.name).name != entry.name
            ):
                raise ValueError("Files server returned an invalid filename")
            await _copy_remote_tree(
                files,
                posixpath.join(source, entry.name),
                destination / entry.name,
                True,
            )
    else:
        await _download(files, source, destination)


@app.async_command(
    "cp", help="Copy local, room or agent files, including across projects/targets."
)
async def storage_cp_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    source_path: str,
    dest_path: str,
    agent: AgentOption = None,
    recursive: RecursiveOption = False,
):
    source, destination = parse_path(source_path), parse_path(dest_path)
    async with storage_context(project_id, room, agent) as context:
        source_files = (
            await context.remote(source) if source.scheme != "local" else None
        )
        destination_files = (
            await context.remote(destination) if destination.scheme != "local" else None
        )
        if source_files is not None:
            sources = await _remote_matches(
                source_files, source.path, folders=recursive
            )
        else:
            sources = (
                glob.glob(source.path) if glob.has_magic(source.path) else [source.path]
            )
            sources = [path for path in sources if Path(path).exists()]
        if not sources:
            raise FileNotFoundError(f"No files match '{source_path}'")
        if destination_files is None:
            folder = Path(destination.path).is_dir() or dest_path.endswith(os.sep)
        else:
            folder = not destination.path or dest_path.endswith("/")
            if not folder:
                try:
                    folder = (await destination_files.stat(destination.path)).is_folder
                except NotFoundError:
                    pass
        if len(sources) > 1 and not folder:
            raise typer.BadParameter(
                "Destination must be a directory for multiple source files"
            )
        for item in sources:
            name = posixpath.basename(item) if source_files else Path(item).name
            dest = (
                destination.path
                if not item
                else (
                    posixpath.join(destination.path, name)
                    if folder
                    else destination.path
                )
            )
            if source_files is not None and destination_files is not None:
                if not item:
                    if not recursive:
                        raise typer.BadParameter("Use -r to copy a files root")
                    if dest:
                        await destination_files.mkdir(dest)
                    async for entry in source_files.list():
                        await destination_files.copy(
                            source_files.location(entry.name),
                            posixpath.join(dest, entry.name),
                            overwrite=True,
                        )
                        print(
                            f"Copied '{entry.name}' to '{posixpath.join(dest, entry.name)}'"
                        )
                    continue
                if (await source_files.stat(item)).is_folder and not recursive:
                    raise typer.BadParameter("Use -r to copy a directory")
                await destination_files.copy(
                    source_files.location(item), dest, overwrite=True
                )
            elif source_files is not None:
                await _copy_remote_tree(source_files, item, Path(dest), recursive)
            elif destination_files is not None:
                await _copy_local_tree(destination_files, Path(item), dest, recursive)
            else:
                output = Path(dest)
                output.parent.mkdir(parents=True, exist_ok=True)
                if Path(item).is_dir():
                    if not recursive:
                        raise typer.BadParameter("Use -r to copy a directory")
                    shutil.copytree(item, output, dirs_exist_ok=True)
                else:
                    shutil.copyfile(item, output)
            print(f"Copied '{item}' to '{dest}'")


@app.async_command("get", help="Print a local, room or agent text file.")
async def storage_get_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    path: str,
    agent: AgentOption = None,
    encoding: Annotated[
        str, typer.Option("--encoding", help="Text encoding")
    ] = "utf-8",
):
    parsed = parse_path(path)
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    async with storage_context(project_id, room, agent) as context:
        if parsed.scheme == "local":
            chunks = _local_chunks(Path(parsed.path))
        else:
            files = await context.remote(parsed)
            chunks = files.download_stream(parsed.path)
        async for chunk in chunks:
            sys.stdout.write(decoder.decode(chunk))
        sys.stdout.write(decoder.decode(b"", final=True) + "\n")


@app.async_command(
    "rm", help="Remove local, room or agent files; directories require -r."
)
async def storage_rm_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    path: str,
    agent: AgentOption = None,
    recursive: RecursiveOption = False,
):
    parsed = parse_path(path)
    async with storage_context(project_id, room, agent) as context:
        if parsed.scheme != "local":
            files = await context.remote(parsed)
            for target in await _remote_matches(files, parsed.path):
                if (await files.stat(target)).is_folder and not recursive:
                    raise typer.BadParameter("Use -r to remove a directory")
                await files.delete(target, recursive=recursive)
                print(f"Removed '{target}'")
        else:
            targets = (
                glob.glob(parsed.path) if glob.has_magic(parsed.path) else [parsed.path]
            )
            for target in targets:
                item = Path(target)
                if item.is_dir() and not item.is_symlink():
                    if not recursive:
                        raise typer.BadParameter("Use -r to remove a directory")
                    shutil.rmtree(item)
                else:
                    item.unlink()
                print(f"Removed '{target}'")


async def _list_remote(
    files: TargetFilesClient, path: str, recursive: bool, prefix: str = ""
) -> None:
    if path and not (await files.stat(path)).is_folder:
        print(prefix + posixpath.basename(path))
        return
    async for entry in files.list(path):
        print(prefix + entry.name + ("/" if entry.is_folder else ""))
        if recursive and entry.is_folder:
            await _list_remote(
                files, posixpath.join(path, entry.name), True, prefix + "  "
            )


def _list_local(path: Path, recursive: bool, prefix: str = "") -> None:
    if not path.is_dir():
        print(prefix + path.name)
        return
    for entry in sorted(path.iterdir()):
        print(prefix + entry.name + ("/" if entry.is_dir() else ""))
        if recursive and entry.is_dir() and not entry.is_symlink():
            _list_local(entry, True, prefix + "  ")


@app.async_command(
    "ls", help="List local, room or agent files, with optional globs and -r."
)
async def storage_ls_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    path: Annotated[
        str, typer.Argument(help="Local, room://, agent:// or meshagent:// path")
    ],
    agent: AgentOption = None,
    recursive: RecursiveOption = False,
):
    parsed = parse_path(path)
    async with storage_context(project_id, room, agent) as context:
        if parsed.scheme == "local":
            targets = (
                glob.glob(parsed.path) if glob.has_magic(parsed.path) else [parsed.path]
            )
            for target in targets:
                if not Path(target).exists():
                    raise FileNotFoundError(target)
                _list_local(Path(target), recursive)
        else:
            files = await context.remote(parsed)
            if glob.has_magic(parsed.path):
                for target in await _remote_matches(files, parsed.path, folders=True):
                    await _list_remote(files, target, recursive)
            else:
                await _list_remote(files, parsed.path, recursive)


@app.async_command(
    "presign", help="Create a scoped temporary file URL (requires presign permission)."
)
async def storage_presign_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption = None,
    path: str,
    agent: AgentOption = None,
    method: Annotated[
        Literal["GET", "HEAD", "PUT", "DELETE"], typer.Option("--method")
    ] = "GET",
    expires_in: Annotated[
        int, typer.Option("--expires-in", min=1, max=604800, help="Lifetime in seconds")
    ] = 3600,
):
    parsed = parse_path(path)
    async with storage_context(project_id, room, agent) as context:
        files = await context.remote(parsed)
        result = await files.presign(parsed.path, method=method, expires_in=expires_in)
        # JSON preserves required headers as well as the URL and expiry.
        sys.stdout.write(result.model_dump_json() + "\n")
