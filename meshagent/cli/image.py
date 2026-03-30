from __future__ import annotations

import asyncio
import platform
import posixpath
import queue
import re
import threading
import tempfile
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.containers import (
    _parse_creds,
    _parse_image_operation_mounts,
    _stream_container_job_logs_and_wait_for_exit,
    _with_client,
)
from meshagent.cli.helper import resolve_room
from meshagent.cli.helper import split_container_mount, split_image_mount
from meshagent.api import RoomClient
from meshagent.cli.oci_archive import (
    DEFAULT_ARCHITECTURE,
    ImagePackError,
    PackedOciArchive,
    build_oci_archive,
    build_oci_archive_to_writer,
)


app = async_typer.AsyncTyper(help="Build and pack OCI images")
_ARCHIVE_STREAM_QUEUE_SIZE = 8
_DEFAULT_CONTEXT_MOUNT_PATH = "/context"
ImageProjectIdOption = Annotated[
    Optional[str],
    typer.Option(
        "--project-id",
        help="A MeshAgent project id. If empty, the activated project will be used.",
    ),
]
ImageRoomOption = Annotated[
    Optional[str],
    typer.Option(
        "--room",
        help="Room name",
    ),
]


class _ArchiveStreamEnd:
    pass


_ARCHIVE_STREAM_END = _ArchiveStreamEnd()


@dataclass(frozen=True)
class _BuildPackSpec:
    source_dir: Path
    mount_path: str


@dataclass(frozen=True)
class _UploadedPackedArchive:
    packed_archive: PackedOciArchive
    remote_path: str


class _StreamingArchiveOutput:
    def __init__(self, *, output_path: Path | None = None) -> None:
        self._output_path = output_path
        self._file = None
        if output_path is not None:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = output_path.open("wb")
        self._queue: queue.Queue[bytes | _ArchiveStreamEnd] = queue.Queue(
            maxsize=_ARCHIVE_STREAM_QUEUE_SIZE
        )
        self._stream_aborted = threading.Event()
        self._error: BaseException | None = None
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("archive output is closed")
        if data == b"":
            return 0

        if self._file is not None:
            self._file.write(data)
        self._enqueue_chunk(data)
        return len(data)

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            self._file.close()

    def finish(self) -> None:
        self.close()
        self._enqueue_end()

    def fail(self, exc: BaseException) -> None:
        self._error = exc
        self.close()
        self._enqueue_end()

    def abort(self) -> None:
        self._stream_aborted.set()

    def cleanup_failed_output(self) -> None:
        if self._output_path is None:
            return
        with suppress(FileNotFoundError):
            self._output_path.unlink()

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        while True:
            item = await asyncio.to_thread(self._queue.get)
            if item is _ARCHIVE_STREAM_END:
                if self._error is not None:
                    raise self._error
                return
            yield item

    def _enqueue_chunk(self, data: bytes) -> None:
        while True:
            if self._stream_aborted.is_set():
                return
            try:
                self._queue.put(data, timeout=0.1)
                return
            except queue.Full:
                continue

    def _enqueue_end(self) -> None:
        while True:
            if self._stream_aborted.is_set():
                with suppress(queue.Full):
                    self._queue.put_nowait(_ARCHIVE_STREAM_END)
                return
            try:
                self._queue.put(_ARCHIVE_STREAM_END, timeout=0.1)
                return
            except queue.Full:
                continue


async def _build_oci_archive_to_streaming_output(
    *,
    source_dir: Path,
    output_path: Path,
    archive_output: _StreamingArchiveOutput,
    base_image: str | None,
    architecture: str,
    ref_name: str | None = None,
    on_packed_archive_ready=None,
) -> PackedOciArchive:
    try:
        build_kwargs = {
            "source_dir": source_dir,
            "output_path": output_path,
            "archive_output": archive_output,
            "base_image": base_image,
            "architecture": architecture,
            "ref_name": ref_name,
        }
        if on_packed_archive_ready is not None:
            build_kwargs["on_packed_archive_ready"] = on_packed_archive_ready
        packed_archive = await build_oci_archive_to_writer(
            **build_kwargs,
        )
    except BaseException as exc:
        archive_output.fail(exc)
        raise

    archive_output.finish()
    return packed_archive


def _resolve_room_archive_path(*, output_path: Path, room_path: str | None) -> str:
    if room_path is None or room_path.strip() == "":
        return f"/{output_path.name}"

    if not room_path.startswith("/"):
        raise typer.BadParameter("--room-path must be an absolute room storage path")

    if room_path.endswith("/"):
        return posixpath.join(room_path, output_path.name)

    return room_path


def _parse_build_pack(value: str) -> _BuildPackSpec:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("--pack cannot be empty")

    if ":" not in cleaned:
        return _BuildPackSpec(
            source_dir=Path(cleaned),
            mount_path=_DEFAULT_CONTEXT_MOUNT_PATH,
        )

    source_dir_text, mount_path = (part.strip() for part in cleaned.rsplit(":", 1))
    if source_dir_text == "" or mount_path == "":
        raise typer.BadParameter("--pack must be in the form '<path>[:<mount>]'")

    return _BuildPackSpec(
        source_dir=Path(source_dir_text),
        mount_path=mount_path,
    )


def _format_container_mount(
    *,
    source: str,
    mount: str,
    read_only: bool,
    default_read_only: bool,
) -> str:
    if read_only == default_read_only:
        return f"{source}:{mount}"
    suffix = "ro" if read_only else "rw"
    return f"{source}:{mount}:{suffix}"


def _format_image_mount(*, image: str, mount: str, read_only: bool) -> str:
    if read_only:
        return f"{image}={mount}"
    return f"{image}={mount}:rw"


def _normalize_build_container_mounts(
    *,
    values: list[str],
    option_name: str,
    default_read_only: bool,
) -> tuple[list[str], list[str]]:
    normalized_values: list[str] = []
    context_candidates: list[str] = []

    for value in values:
        cleaned = value.strip()
        if cleaned == "":
            raise typer.BadParameter(f"{option_name} cannot be empty")

        if ":" not in cleaned:
            mount = _DEFAULT_CONTEXT_MOUNT_PATH
            normalized_values.append(
                _format_container_mount(
                    source=cleaned,
                    mount=mount,
                    read_only=default_read_only,
                    default_read_only=default_read_only,
                )
            )
            context_candidates.append(mount)
            continue

        source, mount, read_only = split_container_mount(
            value,
            option_name,
            default_read_only,
        )
        normalized_values.append(
            _format_container_mount(
                source=source,
                mount=mount,
                read_only=read_only,
                default_read_only=default_read_only,
            )
        )
        context_candidates.append(mount)

    return normalized_values, context_candidates


def _normalize_build_image_mounts(
    *,
    values: list[str],
) -> tuple[list[str], list[str]]:
    normalized_values: list[str] = []
    context_candidates: list[str] = []

    for value in values:
        cleaned = value.strip()
        if cleaned == "":
            raise typer.BadParameter("--mount-image cannot be empty")

        if "=" not in cleaned:
            mount = _DEFAULT_CONTEXT_MOUNT_PATH
            normalized_values.append(
                _format_image_mount(image=cleaned, mount=mount, read_only=True)
            )
            context_candidates.append(mount)
            continue

        image_ref, mount, subpath, read_only = split_image_mount(
            value,
            "--mount-image",
        )
        if subpath is not None:
            raise typer.BadParameter("--mount-image subpaths are not supported here")
        normalized_values.append(
            _format_image_mount(image=image_ref, mount=mount, read_only=read_only)
        )
        context_candidates.append(mount)

    return normalized_values, context_candidates


def _resolve_build_context_path(
    *,
    context_path: str | None,
    context_candidates: list[str],
) -> str:
    if context_path is not None:
        if not context_path.startswith("/"):
            raise typer.BadParameter("--context-path must be an absolute path")
        return context_path

    deduped_candidates: list[str] = []
    for candidate in context_candidates:
        if candidate not in deduped_candidates:
            deduped_candidates.append(candidate)

    if len(deduped_candidates) == 0:
        raise typer.BadParameter(
            "--context-path is required unless exactly one of --pack, "
            "--mount-room-path, --mount-project-path, or --mount-image is provided"
        )

    if len(deduped_candidates) > 1:
        raise typer.BadParameter(
            "--context-path is required when multiple mount targets are provided"
        )

    resolved_context_path = deduped_candidates[0]
    if not resolved_context_path.startswith("/"):
        raise typer.BadParameter("--context-path must be an absolute path")
    return resolved_context_path


def _resolve_build_pack_room_path(*, tag: str, room_path: str | None) -> str | None:
    if room_path is None or room_path.strip() == "":
        return None

    if not room_path.startswith("/"):
        raise typer.BadParameter(
            "--pack-room-path must be an absolute room storage path"
        )

    if room_path.endswith("/"):
        return posixpath.join(room_path, f"{tag}.tar")

    return room_path


def _default_build_pack_room_path(*, packed_archive: PackedOciArchive) -> str:
    algorithm, separator, digest_hex = packed_archive.manifest_digest.partition(":")
    if algorithm != "sha256" or separator == "" or digest_hex == "":
        raise typer.BadParameter(
            "packed build context produced an unsupported manifest digest"
        )
    return f"/.images/{digest_hex}.tar"


def _build_pack_ref_name(*, tag: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", tag.lower()).strip("._-")
    if cleaned == "":
        cleaned = "context"
    return f"meshagent-build-context:{cleaned}"


def _infer_packed_dockerfile_path(
    *,
    pack_spec: _BuildPackSpec,
    context_path: str,
    dockerfile_path: str | None,
) -> str | None:
    if dockerfile_path is not None:
        if not dockerfile_path.startswith("/"):
            raise typer.BadParameter("--dockerfile-path must be an absolute path")
        return dockerfile_path

    if context_path != pack_spec.mount_path:
        return None

    resolved_source_dir = pack_spec.source_dir.expanduser().resolve()
    for candidate_name in ("Containerfile", "Dockerfile"):
        candidate_path = resolved_source_dir / candidate_name
        if candidate_path.is_file():
            return posixpath.join(pack_spec.mount_path, candidate_name)

    raise typer.BadParameter(
        "no Dockerfile or Containerfile found in the packed context; "
        "add one or pass --dockerfile-path"
    )


def _default_pack_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return DEFAULT_ARCHITECTURE


async def _upload_oci_archive_to_room(
    *,
    client: RoomClient,
    source_dir: Path,
    remote_path: str | None,
    output_path: Path | None,
    base_image: str | None,
    architecture: str,
    ref_name: str | None = None,
) -> _UploadedPackedArchive:
    archive_output = _StreamingArchiveOutput(output_path=output_path)
    with tempfile.TemporaryDirectory(prefix="meshagent-oci-upload-") as temp_dir:
        default_output_name = output_path.name if output_path is not None else "oci.tar"
        build_output_path = output_path or (Path(temp_dir) / default_output_name)
        packed_archive_ready: asyncio.Future[PackedOciArchive] = (
            asyncio.get_running_loop().create_future()
        )

        async def _on_packed_archive_ready(packed_archive: PackedOciArchive) -> None:
            if not packed_archive_ready.done():
                packed_archive_ready.set_result(packed_archive)

        build_task = asyncio.create_task(
            _build_oci_archive_to_streaming_output(
                source_dir=source_dir,
                output_path=build_output_path,
                archive_output=archive_output,
                base_image=base_image,
                architecture=architecture,
                ref_name=ref_name,
                on_packed_archive_ready=_on_packed_archive_ready,
            )
        )

        async def _upload_archive() -> str:
            packed_archive = await packed_archive_ready
            resolved_remote_path = (
                remote_path
                if remote_path is not None
                else _default_build_pack_room_path(packed_archive=packed_archive)
            )
            upload_name = (
                output_path.name
                if output_path is not None
                else posixpath.basename(resolved_remote_path)
            )
            await client.storage.upload_stream(
                path=resolved_remote_path,
                chunks=archive_output.iter_chunks(),
                overwrite=True,
                size=None,
                name=upload_name,
            )
            return resolved_remote_path

        upload_task = asyncio.create_task(_upload_archive())
        try:
            packed_archive, resolved_remote_path = await asyncio.gather(
                build_task, upload_task
            )
        except ImagePackError as exc:
            archive_output.abort()
            if not build_task.done():
                with suppress(Exception):
                    await build_task
            if not upload_task.done():
                upload_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await upload_task
            archive_output.cleanup_failed_output()
            raise typer.BadParameter(str(exc)) from exc
        except Exception:
            archive_output.abort()
            if not build_task.done():
                with suppress(Exception):
                    await build_task
            if not upload_task.done():
                upload_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await upload_task
            archive_output.cleanup_failed_output()
            raise
    return _UploadedPackedArchive(
        packed_archive=packed_archive,
        remote_path=resolved_remote_path,
    )


@app.async_command("build", help="Build a container image inside a room.")
async def build_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    tag: Annotated[
        str, typer.Option(..., help="Image tag to build, e.g. repo/name:tag")
    ],
    context_path: Annotated[
        Optional[str],
        typer.Option(
            "--context-path",
            help=(
                "Build context path inside one of the mounted paths (absolute path). "
                "Defaults to the mounted path when there is exactly one context source."
            ),
        ),
    ] = None,
    dockerfile_path: Annotated[
        Optional[str],
        typer.Option(
            "--dockerfile-path",
            help="Optional Dockerfile path inside one of the mounted paths (absolute path)",
        ),
    ] = None,
    pack: Annotated[
        Optional[str],
        typer.Option(
            "--pack",
            help=(
                "Pack a local directory, upload it to room storage, and mount it as an "
                "image volume. Format '<path>[:<mount>]'. Defaults mount to /context."
            ),
        ),
    ] = None,
    pack_architecture: Annotated[
        Optional[str],
        typer.Option(
            "--pack-architecture",
            help=(
                "Architecture metadata for the packed build context image. Defaults "
                "to the local machine architecture."
            ),
        ),
    ] = None,
    pack_room_path: Annotated[
        Optional[str],
        typer.Option(
            "--pack-room-path",
            help=(
                "Room storage path for the uploaded packed archive. Defaults to "
                "'/.images/{manifest-digest}.tar'. If a directory is provided, "
                "'{tag}.tar' is appended."
            ),
        ),
    ] = None,
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help=(
                "Room storage mount '<source>[:<mount>[:ro|rw]]'. "
                "If mount is omitted, /context is used."
            ),
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help=(
                "Project storage mount '<source>[:<mount>[:ro|rw]]'. "
                "If mount is omitted, /context is used."
            ),
        ),
    ] = [],
    mount_image: Annotated[
        list[str],
        typer.Option(
            "--mount-image",
            help=(
                "Image mount '<image>[=<mount>[:ro|rw]]'. "
                "If mount is omitted, /context is used."
            ),
        ),
    ] = [],
    private: Annotated[
        bool,
        typer.Option(
            "--private/--public",
            help="Whether the build container is private to the participant",
        ),
    ] = False,
    cred: Annotated[
        list[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
) -> None:
    normalized_room_mounts, room_context_candidates = _normalize_build_container_mounts(
        values=mount_room_path,
        option_name="--mount-room-path",
        default_read_only=False,
    )
    normalized_project_mounts, project_context_candidates = (
        _normalize_build_container_mounts(
            values=mount_project_path,
            option_name="--mount-project-path",
            default_read_only=True,
        )
    )
    normalized_image_mounts, image_context_candidates = _normalize_build_image_mounts(
        values=mount_image,
    )
    pack_spec = _parse_build_pack(pack) if pack is not None else None
    context_path = _resolve_build_context_path(
        context_path=context_path,
        context_candidates=[
            *room_context_candidates,
            *project_context_candidates,
            *image_context_candidates,
            *([pack_spec.mount_path] if pack_spec is not None else []),
        ],
    )
    if pack_spec is not None:
        dockerfile_path = _infer_packed_dockerfile_path(
            pack_spec=pack_spec,
            context_path=context_path,
            dockerfile_path=dockerfile_path,
        )
    elif dockerfile_path is not None and not dockerfile_path.startswith("/"):
        raise typer.BadParameter("--dockerfile-path must be an absolute path")

    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")

    account_client, client = await _with_client(
        project_id=project_id,
        room=resolved_room,
    )
    try:
        if pack_spec is not None:
            packed_ref_name = _build_pack_ref_name(tag=tag)
            requested_packed_room_path = _resolve_build_pack_room_path(
                tag=tag,
                room_path=pack_room_path,
            )
            resolved_pack_architecture = (
                pack_architecture.strip()
                if pack_architecture is not None and pack_architecture.strip() != ""
                else _default_pack_architecture()
            )
            uploaded_packed_archive = await _upload_oci_archive_to_room(
                client=client,
                source_dir=pack_spec.source_dir,
                remote_path=requested_packed_room_path,
                output_path=None,
                base_image=None,
                architecture=resolved_pack_architecture,
                ref_name=packed_ref_name,
            )
            packed_room_path = uploaded_packed_archive.remote_path
            normalized_image_mounts.append(
                _format_image_mount(
                    image=f"meshagent.room:{packed_room_path}",
                    mount=pack_spec.mount_path,
                    read_only=True,
                )
            )
            print(
                f"[green]Uploaded packed build context[/green] {packed_room_path} "
                f"({packed_ref_name})"
            )

        mount_spec = _parse_image_operation_mounts(
            mount_room_path=normalized_room_mounts,
            mount_project_path=normalized_project_mounts,
            mount_image=normalized_image_mounts,
        )
        container_id = await client.containers.build(
            tag=tag,
            mounts=[mount_spec],
            context_path=context_path,
            dockerfile_path=dockerfile_path,
            private=private,
            credentials=_parse_creds(cred),
        )
        exit_code = await _stream_container_job_logs_and_wait_for_exit(
            client=client, container_id=container_id
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("pack", help="Pack a local directory into an OCI image archive.")
async def pack_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    path: Annotated[str, typer.Argument(help="Local directory to pack")],
    output: Annotated[
        str,
        typer.Option(
            ...,
            "--output",
            "-o",
            help="Local path to write the OCI archive tar",
        ),
    ],
    base: Annotated[
        Optional[str],
        typer.Option(
            "--base",
            help="Optional base image reference. Defaults to scratch semantics.",
        ),
    ] = None,
    architecture: Annotated[
        str,
        typer.Option(
            "--architecture",
            help="Architecture to use when resolving --base",
        ),
    ] = DEFAULT_ARCHITECTURE,
    room_path: Annotated[
        Optional[str],
        typer.Option(
            "--room-path",
            help=(
                "Room storage path to upload the archive to when --room is set. "
                "Defaults to the output file name."
            ),
        ),
    ] = None,
) -> None:
    source_dir = Path(path)
    output_path = Path(output).expanduser().resolve()
    resolved_room = resolve_room(room)
    if resolved_room is None:
        try:
            packed_archive = await build_oci_archive(
                source_dir=source_dir,
                output_path=output_path,
                base_image=base,
                architecture=architecture,
            )
        except ImagePackError as exc:
            raise typer.BadParameter(str(exc)) from exc

        print(
            f"[green]Wrote OCI archive[/green] {packed_archive.output_path} "
            f"({packed_archive.ref_name})"
        )
        return

    remote_path = _resolve_room_archive_path(
        output_path=output_path,
        room_path=room_path,
    )
    account_client, client = await _with_client(
        project_id=project_id,
        room=resolved_room,
    )
    try:
        uploaded_archive = await _upload_oci_archive_to_room(
            client=client,
            source_dir=source_dir,
            remote_path=remote_path,
            output_path=output_path,
            base_image=base,
            architecture=architecture,
        )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()

    print(
        f"[green]Wrote OCI archive[/green] {uploaded_archive.packed_archive.output_path} "
        f"({uploaded_archive.packed_archive.ref_name})"
    )
    print(f"[green]Uploaded OCI archive[/green] {uploaded_archive.remote_path}")
