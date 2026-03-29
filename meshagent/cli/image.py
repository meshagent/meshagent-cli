from __future__ import annotations

import asyncio
import posixpath
import queue
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
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
from meshagent.cli.oci_archive import (
    DEFAULT_ARCHITECTURE,
    ImagePackError,
    build_oci_archive,
    build_oci_archive_to_writer,
)


app = async_typer.AsyncTyper(help="Build and pack OCI images")
_ARCHIVE_STREAM_QUEUE_SIZE = 8
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


class _StreamingArchiveOutput:
    def __init__(self, *, output_path: Path) -> None:
        self._output_path = output_path
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

        self._file.write(data)
        self._enqueue_chunk(data)
        return len(data)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
) -> object:
    try:
        packed_archive = await build_oci_archive_to_writer(
            source_dir=source_dir,
            output_path=output_path,
            archive_output=archive_output,
            base_image=base_image,
            architecture=architecture,
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


@app.async_command("build", help="Build a container image inside a room.")
async def build_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    tag: Annotated[
        str, typer.Option(..., help="Image tag to build, e.g. repo/name:tag")
    ],
    context_path: Annotated[
        str,
        typer.Option(
            ...,
            "--context-path",
            help="Build context path inside one of the mounted paths (absolute path)",
        ),
    ],
    dockerfile_path: Annotated[
        Optional[str],
        typer.Option(
            "--dockerfile-path",
            help="Optional Dockerfile path inside one of the mounted paths (absolute path)",
        ),
    ] = None,
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help=(
                "Room storage mount '<source>:<mount>[:ro|rw]'. "
                "Example '/src:/workspace'"
            ),
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help=(
                "Project storage mount '<source>:<mount>[:ro|rw]'. "
                "Example '/shared:/project:ro'"
            ),
        ),
    ] = [],
    mount_image: Annotated[
        list[str],
        typer.Option(
            "--mount-image",
            help=(
                "Image mount '<image>=<mount>[:ro|rw]'. "
                "Example 'alpine:latest=/toolchain:ro'"
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
    mount_spec = _parse_image_operation_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
        mount_image=mount_image,
    )

    if not context_path.startswith("/"):
        raise typer.BadParameter("--context-path must be an absolute path")
    if dockerfile_path is not None and not dockerfile_path.startswith("/"):
        raise typer.BadParameter("--dockerfile-path must be an absolute path")

    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")

    account_client, client = await _with_client(
        project_id=project_id,
        room=resolved_room,
    )
    try:
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
    archive_output = _StreamingArchiveOutput(output_path=output_path)
    build_task = asyncio.create_task(
        _build_oci_archive_to_streaming_output(
            source_dir=source_dir,
            output_path=output_path,
            archive_output=archive_output,
            base_image=base,
            architecture=architecture,
        )
    )
    upload_task = asyncio.create_task(
        client.storage.upload_stream(
            path=remote_path,
            chunks=archive_output.iter_chunks(),
            overwrite=True,
            size=None,
            name=output_path.name,
        )
    )
    try:
        try:
            packed_archive, _ = await asyncio.gather(build_task, upload_task)
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
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()

    print(
        f"[green]Wrote OCI archive[/green] {packed_archive.output_path} "
        f"({packed_archive.ref_name})"
    )
    print(f"[green]Uploaded OCI archive[/green] {remote_path}")
