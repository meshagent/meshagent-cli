from __future__ import annotations

import asyncio
import json
import posixpath
import queue
import re
import shlex
import threading
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Optional

import typer
from pydantic import ValidationError
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.containers import (
    _parse_creds,
    _parse_image_operation_mounts,
    _stream_build_job_logs_and_wait_for_exit,
    _with_client,
)
from meshagent.cli.helper import resolve_room
from meshagent.cli.helper import (
    resolve_project_id,
    split_container_mount,
    split_empty_dir_mount,
    split_image_mount,
)
from meshagent.api import ApiScope, RoomClient
from meshagent.api.client import ConflictError
from meshagent.api.image_runtime import (
    IMAGE_RUNTIME_BASES,
    IMAGE_RUNTIME_LABEL,
    IMAGE_RUNTIME_MOUNT_PATH,
    IMAGE_RUNTIME_MOUNT_SUBPATH,
    ImageRuntimeDefinition,
)
from meshagent.api.room_ports import RESERVED_ROOM_SERVICE_PORTS
from meshagent.api.specs.service import (
    ANNOTATION_REQUEST_VALIDATION_METHOD,
    ANNOTATION_SERVICE_ID,
    ContainerMountSpec,
    ContainerSpec,
    EmptyDirMountSpec,
    EnvironmentVariable,
    ImageStorageMountSpec,
    PortSpec,
    ProjectStorageMountSpec,
    RoomStorageMountSpec,
    SecretValue,
    ServiceMetadata,
    ServiceSpec,
    TokenValue,
)
from meshagent.cli.oci_archive import (
    DEFAULT_ARCHITECTURE,
    DockerIgnore,
    ImagePackError,
    PackedOciArchive,
    build_oci_archive,
    build_oci_archive_to_writer,
)


app = async_typer.AsyncTyper(help="Build and pack OCI images")
_ARCHIVE_STREAM_QUEUE_SIZE = 8
_CLIENT_CLOSE_TIMEOUT_SECONDS = 2.0
_DEFAULT_CONTEXT_MOUNT_PATH = "/context"
_ROOM_PACK_TAG_REGISTRY = "room.meshagent.com"
_TEMP_BUILD_PACK_ROOM_PATH_PREFIX = "/temp/build/packs"
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_REGISTRY_COMPONENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_COOKIE_VALIDATION_METHOD = "cookie"
_RESERVED_ROOM_SERVICE_PORTS_TEXT = ", ".join(
    str(port) for port in sorted(RESERVED_ROOM_SERVICE_PORTS)
)
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
class _ParsedEnvironmentSecretVariable:
    name: str
    source: str


@dataclass(frozen=True)
class _ResolvedDeployEnvironment:
    environment: list[EnvironmentVariable] | None
    identity: str


@dataclass(frozen=True)
class _ParsedImageTag:
    value: str
    registry: str | None
    repository: str
    tag: str | None

    @property
    def repository_ref(self) -> str:
        if self.registry is None:
            return self.repository
        return f"{self.registry}/{self.repository}"

    @property
    def latest_ref(self) -> str:
        return f"{self.repository_ref}:latest"


@dataclass(frozen=True)
class _UploadedPackedArchive:
    packed_archive: PackedOciArchive
    remote_path: str


@dataclass(frozen=True)
class _ResolvedBuildStageInputs:
    normalized_room_mounts: list[str]
    normalized_project_mounts: list[str]
    normalized_image_mounts: list[str]
    context_path: str
    dockerfile_path: str | None
    pack_spec: _BuildPackSpec | None
    local_packed_dockerfile: Path | None
    preserved_packed_build_paths: frozenset[str]


@dataclass(frozen=True)
class _PackedDockerfileMetadata:
    exposed_ports: tuple[int, ...] = ()
    volumes: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    entrypoint: tuple[str, ...] | None = None
    command: tuple[str, ...] | None = None
    environment: tuple[tuple[str, str], ...] = ()
    working_dir: str | None = None


@dataclass(frozen=True)
class _RuntimeContainerOverride:
    image: str
    command: str
    working_dir: str
    image_mount: ImageStorageMountSpec
    default_environment: tuple[EnvironmentVariable, ...] = ()


@dataclass(frozen=True)
class _ServiceDeployPlan:
    spec: ServiceSpec
    service_id_annotation: str


@dataclass(frozen=True)
class _RoomRouteTarget:
    port: str


@dataclass(frozen=True)
class _RoomServiceUpsertResult:
    service_id: str
    created: bool


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
    preserved_paths: frozenset[str] | None = None,
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
        if preserved_paths is not None:
            build_kwargs["preserved_paths"] = preserved_paths
        if on_packed_archive_ready is not None:
            build_kwargs["on_packed_archive_ready"] = on_packed_archive_ready
        packed_archive = await build_oci_archive_to_writer(
            **build_kwargs,
        )
    except BaseException as exc:
        await asyncio.to_thread(archive_output.fail, exc)
        raise

    await asyncio.to_thread(archive_output.finish)
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


def _parse_build_tag(tag: str) -> _ParsedImageTag:
    cleaned = tag.strip()
    if cleaned == "":
        raise typer.BadParameter("--tag cannot be empty")
    if "@" in cleaned:
        raise typer.BadParameter(
            "--tag must be an OCI image reference without a digest"
        )

    tag_suffix: str | None = None
    last_colon = cleaned.rfind(":")
    last_slash = cleaned.rfind("/")
    if last_colon > last_slash:
        tag_suffix = cleaned[last_colon + 1 :]
        cleaned = cleaned[:last_colon]
        if tag_suffix == "" or _IMAGE_TAG_RE.fullmatch(tag_suffix) is None:
            raise typer.BadParameter(f"invalid OCI image tag: {tag}")

    if cleaned == "":
        raise typer.BadParameter(f"invalid OCI image reference: {tag}")

    parts = cleaned.split("/")
    if any(part == "" for part in parts):
        raise typer.BadParameter(f"invalid OCI image reference: {tag}")

    registry: str | None = None
    repository_parts = parts
    first_part = parts[0]
    if "." in first_part or ":" in first_part or first_part == "localhost":
        registry = first_part
        repository_parts = parts[1:]
        if len(repository_parts) == 0:
            raise typer.BadParameter(
                f"missing repository name in OCI image reference: {tag}"
            )

        host, separator, port = registry.partition(":")
        registry_components = host.split(".")
        if any(
            component == "" or _REGISTRY_COMPONENT_RE.fullmatch(component) is None
            for component in registry_components
        ):
            raise typer.BadParameter(f"invalid OCI image registry: {tag}")
        if separator != "" and (port == "" or not port.isdigit()):
            raise typer.BadParameter(f"invalid OCI image registry port: {tag}")

    if any(
        _REPOSITORY_COMPONENT_RE.fullmatch(component) is None
        for component in repository_parts
    ):
        raise typer.BadParameter(f"invalid OCI image repository: {tag}")

    return _ParsedImageTag(
        value=tag.strip(),
        registry=registry,
        repository="/".join(repository_parts),
        tag=tag_suffix,
    )


def _require_room_pack_tag(*, parsed_tag: _ParsedImageTag) -> None:
    if parsed_tag.registry != _ROOM_PACK_TAG_REGISTRY:
        raise typer.BadParameter(
            "--pack requires --tag to start with room.meshagent.com/"
        )


def _resolve_build_pack_room_path(
    *, parsed_tag: _ParsedImageTag, room_path: str | None
) -> str:
    if room_path is None or room_path.strip() == "":
        return posixpath.join("/", parsed_tag.repository)

    cleaned_room_path = room_path.strip()
    if not cleaned_room_path.startswith("/"):
        raise typer.BadParameter(
            "--pack-room-path must be an absolute room storage path"
        )

    if cleaned_room_path.endswith("/"):
        return posixpath.join(cleaned_room_path, parsed_tag.repository)

    return cleaned_room_path


def _build_pack_ref_name_for_room_path(*, room_path: str) -> str:
    repository = room_path.lstrip("/")
    if repository == "":
        raise typer.BadParameter(
            "packed build contexts require a non-root room storage path"
        )

    repository_parts = repository.split("/")
    if any(
        _REPOSITORY_COMPONENT_RE.fullmatch(component) is None
        for component in repository_parts
    ):
        raise typer.BadParameter(
            "--pack-room-path must map to a valid room.meshagent.com repository path"
        )

    return f"{_ROOM_PACK_TAG_REGISTRY}/{repository}:latest"


def _resolve_uploaded_build_pack_room_path(
    *, parsed_tag: _ParsedImageTag, room_path: str | None
) -> tuple[str, bool]:
    if room_path is None or room_path.strip() == "":
        temporary_room_path = posixpath.join(
            _TEMP_BUILD_PACK_ROOM_PATH_PREFIX,
            uuid.uuid4().hex,
        )
        return temporary_room_path, True

    return _resolve_build_pack_room_path(
        parsed_tag=parsed_tag, room_path=room_path
    ), False


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


def _resolve_local_packed_dockerfile(
    *,
    pack_spec: _BuildPackSpec | None,
    dockerfile_path: str | None,
) -> Path | None:
    if pack_spec is None or dockerfile_path is None:
        return None

    mounted_root = PurePosixPath(pack_spec.mount_path)
    mounted_dockerfile = PurePosixPath(dockerfile_path)
    try:
        relative_path = mounted_dockerfile.relative_to(mounted_root)
    except ValueError:
        return None

    if len(relative_path.parts) == 0:
        return None

    return pack_spec.source_dir.expanduser().resolve().joinpath(*relative_path.parts)


def _resolve_local_packed_path(
    *,
    pack_spec: _BuildPackSpec | None,
    mounted_path: str | None,
) -> Path | None:
    if pack_spec is None or mounted_path is None:
        return None

    mounted_root = PurePosixPath(pack_spec.mount_path)
    mounted_candidate = PurePosixPath(mounted_path)
    try:
        relative_path = mounted_candidate.relative_to(mounted_root)
    except ValueError:
        return None

    resolved_source_dir = pack_spec.source_dir.expanduser().resolve()
    if len(relative_path.parts) == 0:
        return resolved_source_dir
    return resolved_source_dir.joinpath(*relative_path.parts)


def _resolve_build_stage_inputs(
    *,
    context_path: str | None,
    dockerfile_path: str | None,
    pack: str | None,
    mount_room_path: list[str],
    mount_project_path: list[str],
    mount_image: list[str],
) -> _ResolvedBuildStageInputs:
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
    resolved_context_path = _resolve_build_context_path(
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
            context_path=resolved_context_path,
            dockerfile_path=dockerfile_path,
        )
    elif dockerfile_path is not None and not dockerfile_path.startswith("/"):
        raise typer.BadParameter("--dockerfile-path must be an absolute path")

    local_packed_dockerfile = _resolve_local_packed_dockerfile(
        pack_spec=pack_spec,
        dockerfile_path=dockerfile_path,
    )
    preserved_packed_build_paths = _preserved_packed_build_paths(
        pack_spec=pack_spec,
        context_path=resolved_context_path,
        dockerfile_path=dockerfile_path,
    )
    if local_packed_dockerfile is not None and not local_packed_dockerfile.is_file():
        raise typer.BadParameter(
            f"packed Dockerfile does not exist locally: {local_packed_dockerfile}"
        )

    return _ResolvedBuildStageInputs(
        normalized_room_mounts=normalized_room_mounts,
        normalized_project_mounts=normalized_project_mounts,
        normalized_image_mounts=normalized_image_mounts,
        context_path=resolved_context_path,
        dockerfile_path=dockerfile_path,
        pack_spec=pack_spec,
        local_packed_dockerfile=local_packed_dockerfile,
        preserved_packed_build_paths=preserved_packed_build_paths,
    )


def _read_packed_dockerfile_text(*, local_packed_dockerfile: Path | None) -> str | None:
    if local_packed_dockerfile is None:
        return None

    try:
        return local_packed_dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(
            "unable to read packed Dockerfile metadata: "
            f"{local_packed_dockerfile} ({exc})"
        ) from exc


def _iter_dockerfile_instruction_lines(
    *, dockerfile_text: str
) -> list[tuple[str, str]]:
    instructions: list[tuple[str, str]] = []
    logical_line_parts: list[str] = []
    for raw_line in dockerfile_text.splitlines():
        stripped_line = raw_line.strip()
        if not logical_line_parts and (
            stripped_line == "" or stripped_line.startswith("#")
        ):
            continue

        line_part = raw_line.rstrip()
        continues = line_part.endswith("\\")
        if continues:
            line_part = line_part[:-1].rstrip()
        logical_line_parts.append(line_part.strip())
        if continues:
            continue

        logical_line = " ".join(part for part in logical_line_parts if part != "")
        logical_line_parts.clear()
        if logical_line == "":
            continue

        instruction, _, args = logical_line.partition(" ")
        instructions.append((instruction.upper(), args.strip()))
    return instructions


def _parse_dockerfile_assignment_args(*, args: str) -> list[tuple[str, str]]:
    tokens = shlex.split(args, comments=False, posix=True)
    if len(tokens) == 2 and "=" not in tokens[0]:
        key = tokens[0].strip()
        if key == "":
            return []
        return [(key, tokens[1])]

    pairs: list[tuple[str, str]] = []
    for token in tokens:
        key, separator, value = token.partition("=")
        if separator == "" or key.strip() == "":
            continue
        pairs.append((key.strip(), value))
    return pairs


def _parse_dockerfile_command_tokens(*, args: str) -> tuple[str, ...] | None:
    stripped_args = args.strip()
    if stripped_args == "":
        return None
    if stripped_args.startswith("["):
        try:
            parsed_args = json.loads(stripped_args)
        except json.JSONDecodeError:
            parsed_args = None
        if isinstance(parsed_args, list) and all(
            isinstance(value, str) for value in parsed_args
        ):
            return tuple(parsed_args)
    tokens = tuple(shlex.split(stripped_args, comments=False, posix=True))
    return tokens or None


def _normalize_container_path(*, path: str) -> str:
    cleaned_path = path.strip()
    if cleaned_path == "":
        return ""
    return posixpath.normpath(cleaned_path)


def _parse_dockerfile_volume_paths(*, args: str) -> tuple[str, ...]:
    stripped_args = args.strip()
    if stripped_args == "":
        return ()

    parsed_json_paths: list[str] | None = None
    if stripped_args.startswith("["):
        try:
            parsed_args = json.loads(stripped_args)
        except json.JSONDecodeError:
            parsed_args = None
        if isinstance(parsed_args, list) and all(
            isinstance(value, str) for value in parsed_args
        ):
            parsed_json_paths = parsed_args

    volume_paths: list[str] = []
    for raw_path in (
        parsed_json_paths
        if parsed_json_paths is not None
        else shlex.split(stripped_args, comments=False, posix=True)
    ):
        normalized_path = _normalize_container_path(path=raw_path)
        if normalized_path == "" or normalized_path in volume_paths:
            continue
        volume_paths.append(normalized_path)

    return tuple(volume_paths)


def _parse_dockerfile_workdir(
    *,
    args: str,
    current_working_dir: str | None,
) -> str | None:
    tokens = shlex.split(args, comments=False, posix=True)
    if len(tokens) == 0:
        return current_working_dir

    next_working_dir = tokens[0]
    if next_working_dir.startswith("/"):
        return posixpath.normpath(next_working_dir)

    base_working_dir = current_working_dir or "/"
    return posixpath.normpath(posixpath.join(base_working_dir, next_working_dir))


def _parse_packed_dockerfile_metadata(
    *,
    local_packed_dockerfile: Path | None,
) -> _PackedDockerfileMetadata | None:
    dockerfile_text = _read_packed_dockerfile_text(
        local_packed_dockerfile=local_packed_dockerfile
    )
    if dockerfile_text is None:
        return None

    exposed_ports: list[int] = []
    volumes: list[str] = []
    labels: dict[str, str] = {}
    entrypoint: tuple[str, ...] | None = None
    command: tuple[str, ...] | None = None
    environment: dict[str, str] = {}
    working_dir: str | None = None
    saw_from = False

    for instruction, args in _iter_dockerfile_instruction_lines(
        dockerfile_text=dockerfile_text
    ):
        if instruction == "FROM":
            saw_from = True
            exposed_ports = []
            volumes = []
            labels = {}
            entrypoint = None
            command = None
            environment = {}
            working_dir = None
            continue

        if not saw_from:
            continue

        if instruction == "EXPOSE":
            for token in shlex.split(args, comments=False, posix=True):
                port_text, _, protocol = token.partition("/")
                if protocol != "" and protocol.lower() == "udp":
                    continue
                if not port_text.isdigit():
                    continue
                port = int(port_text)
                if port < 1 or port > 65535 or port in exposed_ports:
                    continue
                exposed_ports.append(port)
            continue

        if instruction == "VOLUME":
            for volume_path in _parse_dockerfile_volume_paths(args=args):
                if volume_path in volumes:
                    continue
                volumes.append(volume_path)
            continue

        if instruction == "LABEL":
            for key, value in _parse_dockerfile_assignment_args(args=args):
                labels[key] = value
            continue

        if instruction == "ENV":
            for key, value in _parse_dockerfile_assignment_args(args=args):
                environment[key] = value
            continue

        if instruction == "WORKDIR":
            working_dir = _parse_dockerfile_workdir(
                args=args,
                current_working_dir=working_dir,
            )
            continue

        if instruction == "ENTRYPOINT":
            entrypoint = _parse_dockerfile_command_tokens(args=args)
            continue

        if instruction == "CMD":
            command = _parse_dockerfile_command_tokens(args=args)
            continue

    return _PackedDockerfileMetadata(
        exposed_ports=tuple(exposed_ports),
        volumes=tuple(volumes),
        labels=labels,
        entrypoint=entrypoint,
        command=command,
        environment=tuple(environment.items()),
        working_dir=working_dir,
    )


def _infer_deploy_ports_from_packed_dockerfile(
    *,
    local_packed_dockerfile: Path | None,
) -> list[PortSpec] | None:
    dockerfile_metadata = _parse_packed_dockerfile_metadata(
        local_packed_dockerfile=local_packed_dockerfile
    )
    return _infer_deploy_ports_from_packed_dockerfile_metadata(
        dockerfile_metadata=dockerfile_metadata
    )


def _infer_deploy_ports_from_packed_dockerfile_metadata(
    *,
    dockerfile_metadata: _PackedDockerfileMetadata | None,
) -> list[PortSpec] | None:
    if dockerfile_metadata is None:
        return None

    if len(dockerfile_metadata.exposed_ports) == 0:
        return None

    try:
        return [
            PortSpec(
                num=port,
                type="http",
                published=True,
            )
            for port in dockerfile_metadata.exposed_ports
        ]
    except ValidationError as exc:
        raise typer.BadParameter(
            "packed Dockerfile exposed a reserved MeshAgent room infrastructure "
            f"port; reserved ports: {_RESERVED_ROOM_SERVICE_PORTS_TEXT}"
        ) from exc


def _command_has_runtime_launcher(
    *,
    command_tokens: tuple[str, ...],
    runtime: ImageRuntimeDefinition,
) -> bool:
    if len(command_tokens) == 0 or len(runtime.launcher) == 0:
        return False
    if len(command_tokens) < len(runtime.launcher):
        return False

    first_token = PurePosixPath(command_tokens[0]).name
    expected_first_token = PurePosixPath(runtime.launcher[0]).name
    if first_token != expected_first_token:
        return False

    return command_tokens[1 : len(runtime.launcher)] == runtime.launcher[1:]


def _format_service_command(*, command_tokens: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(token) for token in command_tokens)


def _resolve_runtime_container_override(
    *,
    parsed_tag: _ParsedImageTag,
    dockerfile_metadata: _PackedDockerfileMetadata | None,
) -> _RuntimeContainerOverride | None:
    if dockerfile_metadata is None:
        return None

    runtime_label = dockerfile_metadata.labels.get(IMAGE_RUNTIME_LABEL)
    if runtime_label is None:
        return None

    runtime = IMAGE_RUNTIME_BASES.get(runtime_label.strip().lower())
    if runtime is None:
        return None

    startup_command = dockerfile_metadata.entrypoint
    command_arguments: tuple[str, ...] = ()
    if startup_command is None:
        startup_command = dockerfile_metadata.command
    elif dockerfile_metadata.command is not None:
        command_arguments = dockerfile_metadata.command

    if startup_command is None:
        raise typer.BadParameter(
            "packed Dockerfile final stage uses "
            f"{IMAGE_RUNTIME_LABEL}={runtime.name} but does not define "
            "CMD or ENTRYPOINT"
        )

    if _command_has_runtime_launcher(
        command_tokens=startup_command,
        runtime=runtime,
    ):
        command_tokens = startup_command + command_arguments
    else:
        command_tokens = runtime.launcher + startup_command + command_arguments

    default_environment = tuple(
        EnvironmentVariable(name=name, value=value)
        for name, value in dockerfile_metadata.environment
    )
    working_dir = dockerfile_metadata.working_dir or IMAGE_RUNTIME_MOUNT_PATH

    return _RuntimeContainerOverride(
        image=runtime.base_image,
        command=_format_service_command(command_tokens=command_tokens),
        working_dir=working_dir,
        image_mount=ImageStorageMountSpec(
            image=parsed_tag.value,
            path=IMAGE_RUNTIME_MOUNT_PATH,
            subpath=IMAGE_RUNTIME_MOUNT_SUBPATH,
            read_only=True,
        ),
        default_environment=default_environment,
    )


def _validate_deploy_ports(*, ports: list[PortSpec], source: str) -> None:
    for port in ports:
        if not isinstance(port.num, int):
            continue
        if port.num in RESERVED_ROOM_SERVICE_PORTS:
            raise typer.BadParameter(
                f"{source} uses reserved MeshAgent room infrastructure port "
                f"{port.num}; reserved ports: {_RESERVED_ROOM_SERVICE_PORTS_TEXT}"
            )


def _build_reserved_port_error_source(*, existing_service: ServiceSpec | None) -> str:
    if existing_service is not None:
        return f"existing service {existing_service.metadata.name}"
    return "the inferred service configuration"


def _preserved_packed_build_paths(
    *,
    pack_spec: _BuildPackSpec | None,
    context_path: str,
    dockerfile_path: str | None,
) -> frozenset[str]:
    if pack_spec is None:
        return frozenset()

    resolved_source_dir = pack_spec.source_dir.expanduser().resolve()
    preserved_paths: set[str] = set()

    local_context_path = _resolve_local_packed_path(
        pack_spec=pack_spec,
        mounted_path=context_path,
    )
    if local_context_path is not None:
        dockerignore_path = local_context_path / ".dockerignore"
        if dockerignore_path.is_file():
            preserved_paths.add(
                dockerignore_path.relative_to(resolved_source_dir).as_posix()
            )

    local_dockerfile_path = _resolve_local_packed_dockerfile(
        pack_spec=pack_spec,
        dockerfile_path=dockerfile_path,
    )
    if local_dockerfile_path is not None:
        relative_dockerfile_path = local_dockerfile_path.relative_to(
            resolved_source_dir
        ).as_posix()
        dockerignore_path = resolved_source_dir / ".dockerignore"
        if dockerignore_path.is_file():
            docker_ignore = DockerIgnore(dockerignore_path)
            if docker_ignore.matches(relative_dockerfile_path):
                preserved_paths.add(relative_dockerfile_path)

    return frozenset(preserved_paths)


def _derive_service_name(*, parsed_tag: _ParsedImageTag) -> str:
    return parsed_tag.repository.replace("/", "-")


def _parse_environment_variables(*, values: list[str]) -> list[EnvironmentVariable]:
    environment: list[EnvironmentVariable] = []
    for value in values:
        if "=" not in value:
            raise typer.BadParameter("--env must be in the form 'KEY=VALUE'")
        name, env_value = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise typer.BadParameter("--env must include a non-empty variable name")
        environment.append(EnvironmentVariable(name=resolved_name, value=env_value))
    return environment


def _format_env_secret_reference(*, env_var: EnvironmentVariable) -> str:
    secret = env_var.secret
    if secret is None:
        return env_var.name
    return f"{env_var.name}={secret.id}"


def _parse_environment_secret_variables(
    *, values: list[str]
) -> list[_ParsedEnvironmentSecretVariable]:
    environment: list[_ParsedEnvironmentSecretVariable] = []
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(
                "--env-secret must be in the form 'NAME=SECRET_ID'"
            )
        name, secret_source = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise typer.BadParameter(
                "--env-secret must include a non-empty variable name"
            )
        resolved_source = secret_source.strip()
        if resolved_source == "":
            raise typer.BadParameter(
                "--env-secret must include a non-empty secret source"
            )
        environment.append(
            _ParsedEnvironmentSecretVariable(
                name=resolved_name,
                source=resolved_source,
            )
        )
    return environment


def _normalize_deploy_identity(*, identity: str | None) -> str | None:
    if identity is None:
        return None

    normalized_identity = identity.strip()
    if normalized_identity == "":
        raise typer.BadParameter("--identity cannot be empty")
    return normalized_identity


def _normalize_deploy_liveness(*, liveness: str | None) -> str | None:
    if liveness is None:
        return None

    normalized_liveness = liveness.strip()
    if normalized_liveness == "":
        raise typer.BadParameter("--liveness cannot be empty")
    if not normalized_liveness.startswith("/"):
        raise typer.BadParameter("--liveness must start with '/'")
    return normalized_liveness


def _parse_meshagent_token_scope(*, value: str) -> ApiScope:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("--meshagent-token cannot be empty")
    if cleaned == "userDefault":
        return ApiScope.user_default()
    if cleaned == "agentDefault":
        return ApiScope.agent_default()
    if cleaned == "full":
        return ApiScope.full()
    try:
        return ApiScope.model_validate_json(cleaned)
    except (ValidationError, ValueError) as exc:
        raise typer.BadParameter(
            "--meshagent-token must be one of userDefault, agentDefault, full, "
            "or a JSON ApiScope object"
        ) from exc


def _upsert_environment_variable(
    *,
    environment: list[EnvironmentVariable],
    env_var: EnvironmentVariable,
) -> None:
    for index, existing in enumerate(environment):
        if existing.name != env_var.name:
            continue
        environment[index] = env_var
        return
    environment.append(env_var)


def _resolve_meshagent_token_value(
    *,
    existing_environment: list[EnvironmentVariable] | None,
    default_identity: str,
    api_scope: ApiScope,
    identity_override: str | None,
) -> TokenValue:
    identity = identity_override if identity_override is not None else default_identity
    role = "agent"

    for env_var in existing_environment or []:
        if env_var.name != "MESHAGENT_TOKEN" or env_var.token is None:
            continue
        if identity_override is None and env_var.token.identity.strip() != "":
            identity = env_var.token.identity.strip()
        if env_var.token.role is not None and env_var.token.role.strip() != "":
            role = env_var.token.role.strip()
        break

    return TokenValue(identity=identity, api=api_scope, role=role)


def _resolve_deploy_identity(
    *,
    existing_environment: list[EnvironmentVariable] | None,
    default_identity: str,
    identity_override: str | None,
) -> str:
    if identity_override is not None:
        return identity_override

    for env_var in existing_environment or []:
        if env_var.name != "MESHAGENT_TOKEN" or env_var.token is None:
            continue
        resolved_identity = env_var.token.identity.strip()
        if resolved_identity != "":
            return resolved_identity
        break

    return default_identity


def _override_environment_token_identity(
    *,
    environment: list[EnvironmentVariable],
    identity: str,
) -> None:
    for index, env_var in enumerate(environment):
        if env_var.name != "MESHAGENT_TOKEN" or env_var.token is None:
            continue
        environment[index] = env_var.model_copy(
            update={
                "token": env_var.token.model_copy(
                    update={"identity": identity},
                )
            }
        )
        return


def _resolve_environment_secret_variables(
    *,
    values: list[_ParsedEnvironmentSecretVariable],
    identity: str,
) -> list[EnvironmentVariable]:
    return [
        EnvironmentVariable(
            name=value.name,
            secret=SecretValue(identity=identity, id=value.source),
        )
        for value in values
    ]


def _validate_deploy_environment_tokens(
    *,
    environment: list[EnvironmentVariable] | None,
) -> None:
    for env_var in environment or []:
        if env_var.token is None:
            continue
        token_identity = env_var.token.identity.strip()
        if token_identity == "" or "@" not in token_identity:
            continue
        raise typer.BadParameter(
            f"environment variable '{env_var.name}' uses token identity "
            f"'{token_identity}', but service environment tokens must use an "
            "agent identity"
        )


def _merge_deploy_environment(
    *,
    default_environment: list[EnvironmentVariable] | None,
    existing_environment: list[EnvironmentVariable] | None,
    parsed_environment: list[EnvironmentVariable],
    parsed_secret_environment: list[_ParsedEnvironmentSecretVariable],
    meshagent_token_scope: ApiScope | None,
    token_identity: str,
    identity_override: str | None,
) -> _ResolvedDeployEnvironment:
    environment = [
        env_var.model_copy(deep=True) for env_var in (default_environment or [])
    ]

    for env_var in existing_environment or []:
        _upsert_environment_variable(
            environment=environment,
            env_var=env_var.model_copy(deep=True),
        )

    resolved_identity = _resolve_deploy_identity(
        existing_environment=environment,
        default_identity=token_identity,
        identity_override=identity_override,
    )
    if identity_override is not None:
        _override_environment_token_identity(
            environment=environment,
            identity=identity_override,
        )

    for env_var in parsed_environment:
        _upsert_environment_variable(
            environment=environment,
            env_var=env_var,
        )

    for env_var in _resolve_environment_secret_variables(
        values=parsed_secret_environment,
        identity=resolved_identity,
    ):
        _upsert_environment_variable(
            environment=environment,
            env_var=env_var,
        )

    if meshagent_token_scope is not None:
        _upsert_environment_variable(
            environment=environment,
            env_var=EnvironmentVariable(
                name="MESHAGENT_TOKEN",
                token=_resolve_meshagent_token_value(
                    existing_environment=environment,
                    default_identity=token_identity,
                    api_scope=meshagent_token_scope,
                    identity_override=identity_override,
                ),
            ),
        )

    return _ResolvedDeployEnvironment(
        environment=environment or None,
        identity=resolved_identity,
    )


def _resolve_deploy_environment(
    *,
    existing_service: ServiceSpec | None,
    default_environment: list[EnvironmentVariable] | None,
    parsed_environment: list[EnvironmentVariable],
    parsed_secret_environment: list[_ParsedEnvironmentSecretVariable],
    meshagent_token_scope: ApiScope | None,
    token_identity: str,
    identity_override: str | None,
) -> _ResolvedDeployEnvironment:
    existing_container = (
        existing_service.container if existing_service is not None else None
    )
    return _merge_deploy_environment(
        default_environment=default_environment,
        existing_environment=(
            existing_container.environment if existing_container is not None else None
        ),
        parsed_environment=parsed_environment,
        parsed_secret_environment=parsed_secret_environment,
        meshagent_token_scope=meshagent_token_scope,
        token_identity=token_identity,
        identity_override=identity_override,
    )


def _collect_environment_token_identities(
    *,
    environment: list[EnvironmentVariable] | None,
) -> set[str]:
    identities: set[str] = set()
    for env_var in environment or []:
        if env_var.token is None:
            continue
        identity = env_var.token.identity.strip()
        if identity != "":
            identities.add(identity)
    return identities


async def _validate_deploy_environment_secrets(
    *,
    client: RoomClient,
    environment: list[EnvironmentVariable] | None,
    resolved_identity: str,
) -> None:
    _validate_deploy_environment_tokens(environment=environment)
    token_identities = _collect_environment_token_identities(environment=environment)
    for env_var in environment or []:
        secret = env_var.secret
        if secret is None:
            continue

        secret_reference = _format_env_secret_reference(env_var=env_var)
        if "@" in secret.identity:
            raise typer.BadParameter(
                f"--env-secret {secret_reference} is invalid because service "
                "environment secrets must use an agent identity"
            )

        if secret.identity not in token_identities:
            if secret.identity == resolved_identity:
                raise typer.BadParameter(
                    f"environment variable '{env_var.name}' references secret "
                    f"'{secret.identity}/{secret.id}' but no environment token is "
                    f"defined for identity '{secret.identity}'. Add "
                    "--meshagent-token to inject MESHAGENT_TOKEN for that identity."
                )
            raise typer.BadParameter(
                f"environment variable '{env_var.name}' references secret "
                f"'{secret.identity}/{secret.id}' but no environment token is "
                f"defined for identity '{secret.identity}'. Add --identity "
                f"'{secret.identity}' together with --meshagent-token, or use an "
                "existing token-backed identity."
            )

        if not await client.secrets.exists(
            secret_id=secret.id,
            for_identity=secret.identity,
        ):
            raise typer.BadParameter(
                f"environment variable '{env_var.name}' references missing secret "
                f"'{secret.identity}/{secret.id}'. Save the room secret first, then "
                "retry deploy."
            )


def _apply_runtime_image_mount(
    *,
    storage: ContainerMountSpec | None,
    runtime_image_mount: ImageStorageMountSpec,
) -> ContainerMountSpec:
    if storage is not None:
        for room_mount in storage.room or []:
            if room_mount.path == runtime_image_mount.path:
                raise typer.BadParameter(
                    "packed Dockerfile runtime injection requires "
                    f"{runtime_image_mount.path} to be free of room mounts"
                )
        for project_mount in storage.project or []:
            if project_mount.path == runtime_image_mount.path:
                raise typer.BadParameter(
                    "packed Dockerfile runtime injection requires "
                    f"{runtime_image_mount.path} to be free of project mounts"
                )
        for file_mount in storage.files or []:
            if file_mount.path == runtime_image_mount.path:
                raise typer.BadParameter(
                    "packed Dockerfile runtime injection requires "
                    f"{runtime_image_mount.path} to be free of file mounts"
                )
        for empty_dir_mount in storage.empty_dirs or []:
            if empty_dir_mount.path == runtime_image_mount.path:
                raise typer.BadParameter(
                    "packed Dockerfile runtime injection requires "
                    f"{runtime_image_mount.path} to be free of empty-dir mounts"
                )

    image_mounts: list[ImageStorageMountSpec] = []
    for image_mount in storage.images if storage is not None and storage.images else []:
        if image_mount.path == runtime_image_mount.path:
            if image_mount.subpath not in (None, runtime_image_mount.subpath):
                raise typer.BadParameter(
                    "packed Dockerfile runtime injection requires "
                    f"{runtime_image_mount.path} to be free of conflicting image mounts"
                )
            continue
        image_mounts.append(image_mount.model_copy(deep=True))

    image_mounts.append(runtime_image_mount)
    if storage is None:
        return ContainerMountSpec(images=image_mounts)

    return storage.model_copy(update={"images": image_mounts})


def _iter_deploy_storage_mount_paths(
    *, storage: ContainerMountSpec | None
) -> tuple[str, ...]:
    if storage is None:
        return ()

    mount_paths: list[str] = []
    for room_mount in storage.room or []:
        normalized_path = _normalize_container_path(path=room_mount.path)
        if normalized_path != "" and normalized_path not in mount_paths:
            mount_paths.append(normalized_path)
    for project_mount in storage.project or []:
        normalized_path = _normalize_container_path(path=project_mount.path)
        if normalized_path != "" and normalized_path not in mount_paths:
            mount_paths.append(normalized_path)
    for image_mount in storage.images or []:
        normalized_path = _normalize_container_path(path=image_mount.path)
        if normalized_path != "" and normalized_path not in mount_paths:
            mount_paths.append(normalized_path)
    for empty_dir_mount in storage.empty_dirs or []:
        normalized_path = _normalize_container_path(path=empty_dir_mount.path)
        if normalized_path != "" and normalized_path not in mount_paths:
            mount_paths.append(normalized_path)
    for config_mount in storage.configs or []:
        normalized_path = _normalize_container_path(path=config_mount.path)
        if normalized_path != "" and normalized_path not in mount_paths:
            mount_paths.append(normalized_path)

    return tuple(mount_paths)


def _build_missing_volume_mount_error(*, missing_volume_paths: tuple[str, ...]) -> str:
    if len(missing_volume_paths) == 1:
        missing_paths_text = missing_volume_paths[0]
    else:
        missing_paths_text = ", ".join(missing_volume_paths)

    example_path = missing_volume_paths[0]
    return (
        "packed Dockerfile final stage declares VOLUME path"
        f"{'' if len(missing_volume_paths) == 1 else 's'} {missing_paths_text}, "
        "but the deployed service does not mount "
        f"{'it' if len(missing_volume_paths) == 1 else 'them'}. "
        "Add a matching mount for each Dockerfile volume path, for example:\n"
        f"  --empty-dir-mount {example_path}\n"
        f"  --room-mount .:{example_path}\n"
        f"  --project-mount .:{example_path}:rw\n"
        f"  --image-mount some/image:tag={example_path}:rw\n"
        "Repeat one of those flags for every missing Dockerfile volume path."
    )


def _validate_packed_dockerfile_volume_mounts(
    *,
    dockerfile_metadata: _PackedDockerfileMetadata | None,
    storage: ContainerMountSpec | None,
) -> None:
    if dockerfile_metadata is None or len(dockerfile_metadata.volumes) == 0:
        return

    mounted_paths = set(_iter_deploy_storage_mount_paths(storage=storage))
    missing_volume_paths = tuple(
        volume_path
        for volume_path in dockerfile_metadata.volumes
        if volume_path not in mounted_paths
    )
    if len(missing_volume_paths) == 0:
        return

    raise typer.BadParameter(
        _build_missing_volume_mount_error(missing_volume_paths=missing_volume_paths)
    )


def _parse_deploy_storage(
    *,
    room_mounts: list[str],
    project_mounts: list[str],
    image_mounts: list[str],
    empty_dir_mounts: list[str],
) -> ContainerMountSpec | None:
    room_specs: list[RoomStorageMountSpec] = []
    project_specs: list[ProjectStorageMountSpec] = []
    image_specs: list[ImageStorageMountSpec] = []
    empty_dir_specs: list[EmptyDirMountSpec] = []

    for value in room_mounts:
        source, mount, read_only = split_container_mount(value, "--room-mount", False)
        subpath = source if source not in {"", ".", "/"} else None
        room_specs.append(
            RoomStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in project_mounts:
        source, mount, read_only = split_container_mount(value, "--project-mount", True)
        subpath = source if source not in {"", ".", "/"} else None
        project_specs.append(
            ProjectStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in image_mounts:
        image_ref, mount, subpath, read_only = split_image_mount(value, "--image-mount")
        image_specs.append(
            ImageStorageMountSpec(
                image=image_ref,
                path=mount,
                subpath=subpath,
                read_only=read_only,
            )
        )

    for value in empty_dir_mounts:
        mount, read_only = split_empty_dir_mount(value, "--empty-dir-mount")
        empty_dir_specs.append(EmptyDirMountSpec(path=mount, read_only=read_only))

    if not room_specs and not project_specs and not image_specs and not empty_dir_specs:
        return None

    return ContainerMountSpec(
        room=room_specs or None,
        project=project_specs or None,
        images=image_specs or None,
        empty_dirs=empty_dir_specs or None,
    )


def _merge_deploy_storage(
    *,
    existing_storage: ContainerMountSpec | None,
    parsed_storage: ContainerMountSpec | None,
    replace_room_mounts: bool,
    replace_project_mounts: bool,
    replace_image_mounts: bool,
    replace_empty_dir_mounts: bool,
) -> ContainerMountSpec | None:
    if parsed_storage is None:
        return existing_storage.model_copy(deep=True) if existing_storage else None

    preserved_storage = (
        existing_storage.model_copy(deep=True) if existing_storage else None
    )
    merged_storage = ContainerMountSpec(
        room=(
            parsed_storage.room
            if replace_room_mounts
            else preserved_storage.room
            if preserved_storage
            else None
        ),
        project=(
            parsed_storage.project
            if replace_project_mounts
            else preserved_storage.project
            if preserved_storage
            else None
        ),
        images=(
            parsed_storage.images
            if replace_image_mounts
            else preserved_storage.images
            if preserved_storage
            else None
        ),
        files=preserved_storage.files if preserved_storage else None,
        empty_dirs=(
            parsed_storage.empty_dirs
            if replace_empty_dir_mounts
            else preserved_storage.empty_dirs
            if preserved_storage
            else None
        ),
    )

    if (
        merged_storage.room is None
        and merged_storage.project is None
        and merged_storage.images is None
        and merged_storage.files is None
        and merged_storage.empty_dirs is None
    ):
        return None

    return merged_storage


def _resolve_deploy_storage(
    *,
    existing_service: ServiceSpec | None,
    parsed_storage: ContainerMountSpec | None,
    replace_room_mounts: bool,
    replace_project_mounts: bool,
    replace_image_mounts: bool,
    replace_empty_dir_mounts: bool,
    runtime_container: _RuntimeContainerOverride | None,
) -> ContainerMountSpec | None:
    existing_container = (
        existing_service.container if existing_service is not None else None
    )
    storage = _merge_deploy_storage(
        existing_storage=existing_container.storage if existing_container else None,
        parsed_storage=parsed_storage,
        replace_room_mounts=replace_room_mounts,
        replace_project_mounts=replace_project_mounts,
        replace_image_mounts=replace_image_mounts,
        replace_empty_dir_mounts=replace_empty_dir_mounts,
    )
    if runtime_container is not None:
        storage = _apply_runtime_image_mount(
            storage=storage,
            runtime_image_mount=runtime_container.image_mount,
        )
    return storage


def _build_deploy_service_spec(
    *,
    existing_service: ServiceSpec | None,
    parsed_tag: _ParsedImageTag,
    public: bool,
    liveness: str | None,
    environment: list[EnvironmentVariable] | None = None,
    storage: ContainerMountSpec | None = None,
    default_ports: list[PortSpec] | None = None,
    runtime_container: _RuntimeContainerOverride | None = None,
) -> _ServiceDeployPlan:
    service_name = _derive_service_name(parsed_tag=parsed_tag)
    annotations = _update_request_validation_annotations(
        annotations=(
            dict(existing_service.metadata.annotations or {})
            if existing_service is not None
            else {}
        ),
        public=public,
    )
    annotations[ANNOTATION_SERVICE_ID] = service_name

    if existing_service is not None and existing_service.external is not None:
        raise typer.BadParameter(
            f"existing service {service_name} is external and cannot be replaced with a container deployment"
        )

    metadata = (
        existing_service.metadata.model_copy(
            update={"name": service_name, "annotations": annotations}
        )
        if existing_service is not None
        else ServiceMetadata(
            name=service_name,
            annotations=annotations,
        )
    )
    container = (
        existing_service.container.model_copy(
            update={
                "image": (
                    runtime_container.image
                    if runtime_container is not None
                    else parsed_tag.value
                )
            }
        )
        if existing_service is not None and existing_service.container is not None
        else ContainerSpec(
            image=(
                runtime_container.image
                if runtime_container is not None
                else parsed_tag.value
            )
        )
    )
    if environment is not None:
        container = container.model_copy(update={"environment": environment})
    if storage is not None:
        container = container.model_copy(update={"storage": storage})
    if runtime_container is not None:
        container = container.model_copy(
            update={
                "command": runtime_container.command,
                "working_dir": runtime_container.working_dir,
            }
        )

    if existing_service is not None:
        ports = list(existing_service.ports or [])
        if len(ports) == 0 and default_ports is not None:
            ports = [port.model_copy(deep=True) for port in default_ports]
    else:
        ports = (
            [port.model_copy(deep=True) for port in default_ports]
            if default_ports is not None
            else []
        )
    if len(ports) > 0:
        _validate_deploy_ports(
            ports=ports,
            source=_build_reserved_port_error_source(existing_service=existing_service),
        )
        ports = [
            _update_deploy_port(
                port=port,
                public=public,
                liveness=liveness,
            )
            for port in ports
        ]

    spec = (
        existing_service.model_copy(
            update={
                "metadata": metadata,
                "container": container,
                "ports": ports,
                "external": None,
            }
        )
        if existing_service is not None
        else ServiceSpec(
            version="v1",
            kind="Service",
            metadata=metadata,
            container=container,
            ports=ports,
        )
    )
    return _ServiceDeployPlan(spec=spec, service_id_annotation=service_name)


def _update_request_validation_annotations(
    *,
    annotations: dict[str, str],
    public: bool,
) -> dict[str, str]:
    updated_annotations = dict(annotations)
    if not public:
        updated_annotations[ANNOTATION_REQUEST_VALIDATION_METHOD] = (
            _COOKIE_VALIDATION_METHOD
        )
    elif (
        updated_annotations.get(ANNOTATION_REQUEST_VALIDATION_METHOD)
        == _COOKIE_VALIDATION_METHOD
    ):
        del updated_annotations[ANNOTATION_REQUEST_VALIDATION_METHOD]

    return updated_annotations


def _update_deploy_port(
    *,
    port: PortSpec,
    public: bool,
    liveness: str | None,
) -> PortSpec:
    updated_port = port.model_copy(deep=True)
    if updated_port.type != "tcp":
        if liveness is not None:
            updated_port = updated_port.model_copy(update={"liveness": liveness})
        elif updated_port.liveness is None:
            updated_port = updated_port.model_copy(update={"liveness": "/"})

    if not updated_port.published:
        return updated_port

    annotations = _update_request_validation_annotations(
        annotations=dict(updated_port.annotations or {}),
        public=public,
    )
    return updated_port.model_copy(
        update={
            "public": True if public else None,
            "annotations": annotations or None,
        }
    )


def _resolve_domain_route_target(*, service_spec: ServiceSpec) -> _RoomRouteTarget:
    published_ports = [
        port
        for port in service_spec.ports or []
        if port.published and isinstance(port.num, int)
    ]
    if len(published_ports) == 0:
        raise typer.BadParameter(
            "--domain requires exactly one published service port; the service has none"
        )
    if len(published_ports) > 1:
        raise typer.BadParameter(
            "--domain requires exactly one published service port; the service has multiple published ports"
        )
    return _RoomRouteTarget(port=str(published_ports[0].num))


async def _find_room_service_by_name(
    *,
    account_client,
    project_id: str,
    room_name: str,
    service_name: str,
) -> ServiceSpec | None:
    services = await account_client.list_room_services(
        project_id=project_id,
        room_name=room_name,
    )
    for service in services:
        if service.metadata.name == service_name:
            return service
    return None


async def _upsert_room_service(
    *,
    account_client,
    project_id: str,
    room_name: str,
    service_spec: ServiceSpec,
) -> _RoomServiceUpsertResult:
    existing_service = await _find_room_service_by_name(
        account_client=account_client,
        project_id=project_id,
        room_name=room_name,
        service_name=service_spec.metadata.name,
    )
    if existing_service is None:
        service_id = await account_client.create_room_service(
            project_id=project_id,
            room_name=room_name,
            service=service_spec,
        )
        return _RoomServiceUpsertResult(service_id=service_id, created=True)

    if existing_service.id is None or existing_service.id == "":
        raise typer.BadParameter(
            f"existing service {service_spec.metadata.name} is missing an id"
        )
    service_spec.id = existing_service.id
    await account_client.update_room_service(
        project_id=project_id,
        room_name=room_name,
        service_id=existing_service.id,
        service=service_spec,
    )
    return _RoomServiceUpsertResult(service_id=existing_service.id, created=False)


async def _upsert_domain_route(
    *,
    account_client,
    project_id: str,
    room_name: str,
    domain: str,
    port: str,
    service_id: str,
) -> None:
    route_annotations = {ANNOTATION_SERVICE_ID: service_id}
    try:
        await account_client.create_route(
            project_id=project_id,
            domain=domain,
            room_name=room_name,
            port=port,
            annotations=route_annotations,
        )
    except ConflictError:
        existing = await account_client.get_route(project_id=project_id, domain=domain)
        if existing.room_name != room_name:
            raise typer.BadParameter(
                f"--domain {domain} already routes to room {existing.room_name}. "
                f"Refusing to change it to room {room_name}."
            ) from None
        updated_annotations = dict(existing.annotations)
        updated_annotations[ANNOTATION_SERVICE_ID] = service_id
        if existing.port == port and existing.annotations == updated_annotations:
            print(f"[green]Route already configured:[/] {domain} -> {room_name}:{port}")
            return
        await account_client.update_route(
            project_id=project_id,
            domain=domain,
            room_name=room_name,
            port=port,
            annotations=updated_annotations,
        )
        print(f"[green]Updated route:[/] {domain} -> {room_name}:{port}")
    else:
        print(f"[green]Created route:[/] {domain} -> {room_name}:{port}")


async def _apply_deploy_plan(
    *,
    account_client,
    client: RoomClient,
    project_id: str,
    room_name: str,
    deploy_plan: _ServiceDeployPlan,
    domain: str | None,
) -> None:
    route_target = (
        _resolve_domain_route_target(service_spec=deploy_plan.spec)
        if domain is not None
        else None
    )
    deploy_result = await _upsert_room_service(
        account_client=account_client,
        project_id=project_id,
        room_name=room_name,
        service_spec=deploy_plan.spec,
    )
    print(
        f"[green]Deployed service:[/] {deploy_plan.spec.metadata.name} "
        f"({deploy_result.service_id})"
    )
    if not deploy_result.created:
        await client.services.restart(service_id=deploy_result.service_id)
        print(
            f"[green]Restarted service:[/] {deploy_plan.spec.metadata.name} "
            f"({deploy_result.service_id})"
        )
    if domain is not None and route_target is not None:
        await _upsert_domain_route(
            account_client=account_client,
            project_id=project_id,
            room_name=room_name,
            domain=domain,
            port=route_target.port,
            service_id=deploy_plan.service_id_annotation,
        )


async def _upload_oci_archive_to_room(
    *,
    client: RoomClient,
    source_dir: Path,
    remote_path: str | None,
    output_path: Path | None,
    base_image: str | None,
    architecture: str,
    ref_name: str | None = None,
    preserved_paths: frozenset[str] | None = None,
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
                preserved_paths=preserved_paths,
                on_packed_archive_ready=_on_packed_archive_ready,
            )
        )

        async def _upload_archive() -> str:
            await packed_archive_ready
            if remote_path is None:
                raise typer.BadParameter(
                    "packed archive upload requires an explicit room storage path"
                )
            resolved_remote_path = remote_path
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


async def _close_pack_clients(
    *,
    account_client,
    client: RoomClient,
) -> None:
    try:
        await asyncio.wait_for(
            client.__aexit__(None, None, None),
            timeout=_CLIENT_CLOSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print("[yellow]Timed out closing room client after upload[/yellow]")

    try:
        await asyncio.wait_for(
            account_client.close(),
            timeout=_CLIENT_CLOSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        print("[yellow]Timed out closing account client after upload[/yellow]")


async def _run_image_build_stage(
    *,
    resolved_project_id: str | None,
    resolved_room: str,
    parsed_tag: _ParsedImageTag,
    context_path: str | None,
    dockerfile_path: str | None,
    pack: str | None,
    arch: str,
    pack_room_path: str | None,
    mount_room_path: list[str],
    mount_project_path: list[str],
    mount_image: list[str],
    private: bool,
    optimize: bool,
    cred: list[str],
) -> None:
    build_inputs = _resolve_build_stage_inputs(
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        pack=pack,
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
        mount_image=mount_image,
    )

    account_client, client = await _with_client(
        project_id=resolved_project_id,
        room=resolved_room,
    )
    packed_room_path: str | None = None
    should_delete_packed_room_path = False
    context_archive_path: str | None = None
    context_archive_ref: str | None = None
    context_archive_mount_path: str | None = None
    context_archive_arch: str | None = None
    loaded_packed_archive_ref: str | None = None
    try:
        if build_inputs.pack_spec is not None:
            _require_room_pack_tag(parsed_tag=parsed_tag)
            requested_packed_room_path, should_delete_packed_room_path = (
                _resolve_uploaded_build_pack_room_path(
                    parsed_tag=parsed_tag,
                    room_path=pack_room_path,
                )
            )
            packed_ref_name = _build_pack_ref_name_for_room_path(
                room_path=requested_packed_room_path
            )
            resolved_pack_architecture = arch.strip()
            if resolved_pack_architecture == "":
                raise typer.BadParameter("--arch cannot be empty")
            uploaded_packed_archive = await _upload_oci_archive_to_room(
                client=client,
                source_dir=build_inputs.pack_spec.source_dir,
                remote_path=requested_packed_room_path,
                output_path=None,
                base_image=None,
                architecture=resolved_pack_architecture,
                ref_name=packed_ref_name,
                preserved_paths=build_inputs.preserved_packed_build_paths,
            )
            packed_room_path = uploaded_packed_archive.remote_path
            loaded_packed_archive = await client.containers.load(
                archive_path=packed_room_path
            )
            context_archive_path = packed_room_path
            context_archive_ref = loaded_packed_archive.resolved_ref
            loaded_packed_archive_ref = loaded_packed_archive.resolved_ref
            context_archive_mount_path = build_inputs.pack_spec.mount_path
            context_archive_arch = resolved_pack_architecture
            upload_label = (
                "Uploaded temporary packed build context"
                if should_delete_packed_room_path
                else "Uploaded packed build context"
            )
            print(
                f"[green]{upload_label}[/green] {packed_room_path} ({packed_ref_name})"
            )

        mounts: list[ContainerMountSpec] = []
        if (
            len(build_inputs.normalized_room_mounts) > 0
            or len(build_inputs.normalized_project_mounts) > 0
            or len(build_inputs.normalized_image_mounts) > 0
        ):
            mounts.append(
                _parse_image_operation_mounts(
                    mount_room_path=build_inputs.normalized_room_mounts,
                    mount_project_path=build_inputs.normalized_project_mounts,
                    mount_image=build_inputs.normalized_image_mounts,
                )
            )
        build_id = await client.containers.build(
            tag=parsed_tag.value,
            mounts=mounts,
            context_path=build_inputs.context_path,
            dockerfile_path=build_inputs.dockerfile_path,
            optimize_image=optimize,
            private=private,
            credentials=_parse_creds(cred),
            context_archive_path=context_archive_path,
            context_archive_ref=context_archive_ref,
            context_archive_mount_path=context_archive_mount_path,
            context_archive_arch=context_archive_arch,
        )
        exit_code = await _stream_build_job_logs_and_wait_for_exit(
            client=client, build_id=build_id
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        if loaded_packed_archive_ref is not None:
            try:
                await client.containers.delete_image(image=loaded_packed_archive_ref)
            except Exception as exc:
                print(
                    "[yellow]Unable to delete temporary packed build image:[/yellow] "
                    f"{loaded_packed_archive_ref} ({exc})"
                )
        if should_delete_packed_room_path and packed_room_path is not None:
            try:
                await client.storage.delete(path=packed_room_path)
            except Exception as exc:
                print(
                    "[yellow]Unable to delete temporary packed build context:[/yellow] "
                    f"{packed_room_path} ({exc})"
                )
        await client.__aexit__(None, None, None)
        await account_client.close()


def _validate_deploy_build_stage_options(
    *,
    pack: str | None,
    context_path: str | None,
    dockerfile_path: str | None,
    arch: str,
    pack_room_path: str | None,
    optimize: bool,
) -> None:
    if pack is not None:
        return

    invalid_options: list[str] = []
    if context_path is not None:
        invalid_options.append("--context-path")
    if dockerfile_path is not None:
        invalid_options.append("--dockerfile-path")
    if arch != DEFAULT_ARCHITECTURE:
        invalid_options.append("--arch")
    if pack_room_path is not None:
        invalid_options.append("--pack-room-path")
    if not optimize:
        invalid_options.append("--no-optimize")

    if len(invalid_options) == 0:
        return

    if len(invalid_options) == 1:
        raise typer.BadParameter(f"{invalid_options[0]} requires --pack")

    raise typer.BadParameter(f"{', '.join(invalid_options)} require --pack")


@app.async_command("build", help="Build a container image inside a room.")
async def build_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    tag: Annotated[
        str,
        typer.Option(
            ...,
            help=(
                "Image tag to build, e.g. repo/name:tag. When --pack is used, "
                "this must start with room.meshagent.com/."
            ),
        ),
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
    arch: Annotated[
        str,
        typer.Option(
            "--arch",
            help=(
                "Architecture metadata for the packed build context image. Defaults "
                "to amd64 for room runtimes."
            ),
        ),
    ] = DEFAULT_ARCHITECTURE,
    pack_room_path: Annotated[
        Optional[str],
        typer.Option(
            "--pack-room-path",
            help=(
                "Room storage path for the uploaded packed archive. Defaults to "
                "a temporary path under /temp/build/packs/ that is deleted after "
                "the build completes. If a directory is provided, the repository "
                "path from --tag is appended."
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
    optimize: Annotated[
        bool,
        typer.Option(
            "--optimize/--no-optimize",
            help=(
                "Whether to optimize room image outputs to eStargz before publishing. "
                "Enabled by default."
            ),
        ),
    ] = True,
    cred: Annotated[
        list[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
) -> None:
    parsed_tag = _parse_build_tag(tag)
    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")
    resolved_project_id = await resolve_project_id(project_id=project_id)
    await _run_image_build_stage(
        resolved_project_id=resolved_project_id,
        resolved_room=resolved_room,
        parsed_tag=parsed_tag,
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        pack=pack,
        arch=arch,
        pack_room_path=pack_room_path,
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
        mount_image=mount_image,
        private=private,
        optimize=optimize,
        cred=cred,
    )


@app.async_command(
    "deploy",
    help="Create or update a room service from an image, optionally building it first.",
)
async def deploy_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    tag: Annotated[
        str,
        typer.Option(
            ...,
            help="Image tag to deploy, e.g. repo/name:tag.",
        ),
    ],
    pack: Annotated[
        Optional[str],
        typer.Option(
            "--pack",
            help=(
                "Pack a local directory, upload it to room storage, build the image, "
                "and then deploy it. Format '<path>[:<mount>]'. Defaults mount to "
                "/context."
            ),
        ),
    ] = None,
    context_path: Annotated[
        Optional[str],
        typer.Option(
            "--context-path",
            help=(
                "Build context path inside the packed build context (absolute path). "
                "Only used with --pack."
            ),
        ),
    ] = None,
    dockerfile_path: Annotated[
        Optional[str],
        typer.Option(
            "--dockerfile-path",
            help=(
                "Optional Dockerfile path inside the packed build context (absolute "
                "path). Only used with --pack."
            ),
        ),
    ] = None,
    arch: Annotated[
        str,
        typer.Option(
            "--arch",
            help=(
                "Architecture metadata for the packed build context image. Only used "
                "with --pack. Defaults to amd64 for room runtimes."
            ),
        ),
    ] = DEFAULT_ARCHITECTURE,
    pack_room_path: Annotated[
        Optional[str],
        typer.Option(
            "--pack-room-path",
            help=(
                "Room storage path for the uploaded packed archive during the build "
                "stage. Defaults to a temporary path under /temp/build/packs/ that "
                "is deleted after the build completes."
            ),
        ),
    ] = None,
    optimize: Annotated[
        bool,
        typer.Option(
            "--optimize/--no-optimize",
            help=(
                "Whether to optimize room image outputs to eStargz during the build "
                "stage. Enabled by default. Only used with --pack."
            ),
        ),
    ] = True,
    domain: Annotated[
        Optional[str],
        typer.Option(
            "--domain",
            help=(
                "Create or update a room route for the deployed service. "
                "Requires exactly one published service port."
            ),
        ),
    ] = None,
    liveness: Annotated[
        Optional[str],
        typer.Option(
            "--liveness",
            help=(
                "HTTP path to use for service liveness checks. Defaults to / for "
                "new or missing HTTP liveness paths."
            ),
        ),
    ] = None,
    room_mount: Annotated[
        list[str],
        typer.Option(
            "--room-mount",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    project_mount: Annotated[
        list[str],
        typer.Option(
            "--project-mount",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    empty_dir_mount: Annotated[
        list[str],
        typer.Option(
            "--empty-dir-mount",
            help="Mount empty dir at <mount>[:ro|rw]",
        ),
    ] = [],
    image_mount: Annotated[
        list[str],
        typer.Option(
            "--image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    env: Annotated[
        list[str],
        typer.Option(
            "--env",
            "-e",
            help="Set environment variable as KEY=VALUE",
        ),
    ] = [],
    env_secret: Annotated[
        list[str],
        typer.Option(
            "--env-secret",
            help="Set environment variable from a room secret as NAME=SECRET_ID",
        ),
    ] = [],
    identity: Annotated[
        Optional[str],
        typer.Option(
            "--identity",
            help=(
                "Identity name to use for --meshagent-token and --env-secret. "
                "Defaults to the current token identity or the derived service name."
            ),
        ),
    ] = None,
    meshagent_token: Annotated[
        Optional[str],
        typer.Option(
            "--meshagent-token",
            help=(
                "Inject MESHAGENT_TOKEN using userDefault, agentDefault, full, "
                "or a JSON ApiScope object."
            ),
        ),
    ] = None,
    private: Annotated[
        bool,
        typer.Option(
            "--private/--public",
            help=(
                "Whether published service ports should stay private or be "
                "public when they are created or updated. Defaults to private."
            ),
        ),
    ] = True,
) -> None:
    parsed_tag = _parse_build_tag(tag)
    _validate_deploy_build_stage_options(
        pack=pack,
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        arch=arch,
        pack_room_path=pack_room_path,
        optimize=optimize,
    )
    parsed_environment = _parse_environment_variables(values=env)
    parsed_secret_environment = _parse_environment_secret_variables(values=env_secret)
    parsed_storage = _parse_deploy_storage(
        room_mounts=room_mount,
        project_mounts=project_mount,
        image_mounts=image_mount,
        empty_dir_mounts=empty_dir_mount,
    )
    meshagent_token_scope = (
        _parse_meshagent_token_scope(value=meshagent_token)
        if meshagent_token is not None
        else None
    )
    identity_override = _normalize_deploy_identity(identity=identity)

    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")
    if domain is not None:
        domain = domain.strip()
        if domain == "":
            raise typer.BadParameter("--domain cannot be empty")
    normalized_liveness = _normalize_deploy_liveness(liveness=liveness)

    resolved_project_id = await resolve_project_id(project_id=project_id)
    packed_default_ports: list[PortSpec] | None = None
    packed_dockerfile_metadata: _PackedDockerfileMetadata | None = None
    runtime_container: _RuntimeContainerOverride | None = None
    if pack is not None:
        build_inputs = _resolve_build_stage_inputs(
            context_path=context_path,
            dockerfile_path=dockerfile_path,
            pack=pack,
            mount_room_path=[],
            mount_project_path=[],
            mount_image=[],
        )
        packed_dockerfile_metadata = _parse_packed_dockerfile_metadata(
            local_packed_dockerfile=build_inputs.local_packed_dockerfile
        )
        packed_default_ports = _infer_deploy_ports_from_packed_dockerfile_metadata(
            dockerfile_metadata=packed_dockerfile_metadata
        )
        runtime_container = _resolve_runtime_container_override(
            parsed_tag=parsed_tag,
            dockerfile_metadata=packed_dockerfile_metadata,
        )
    meshagent_token_identity = _derive_service_name(parsed_tag=parsed_tag)
    account_client, client = await _with_client(
        project_id=resolved_project_id,
        room=resolved_room,
    )
    try:
        existing_service = await _find_room_service_by_name(
            account_client=account_client,
            project_id=resolved_project_id,
            room_name=resolved_room,
            service_name=_derive_service_name(parsed_tag=parsed_tag),
        )
        storage = _resolve_deploy_storage(
            existing_service=existing_service,
            parsed_storage=parsed_storage,
            replace_room_mounts=len(room_mount) > 0,
            replace_project_mounts=len(project_mount) > 0,
            replace_image_mounts=len(image_mount) > 0,
            replace_empty_dir_mounts=len(empty_dir_mount) > 0,
            runtime_container=runtime_container,
        )
        _validate_packed_dockerfile_volume_mounts(
            dockerfile_metadata=packed_dockerfile_metadata,
            storage=storage,
        )
        resolved_environment = _resolve_deploy_environment(
            existing_service=existing_service,
            default_environment=(
                list(runtime_container.default_environment)
                if runtime_container is not None
                else None
            ),
            parsed_environment=parsed_environment,
            parsed_secret_environment=parsed_secret_environment,
            meshagent_token_scope=meshagent_token_scope,
            token_identity=meshagent_token_identity,
            identity_override=identity_override,
        )
        environment = resolved_environment.environment
        await _validate_deploy_environment_secrets(
            client=client,
            environment=environment,
            resolved_identity=resolved_environment.identity,
        )
        if pack is not None:
            await _run_image_build_stage(
                resolved_project_id=resolved_project_id,
                resolved_room=resolved_room,
                parsed_tag=parsed_tag,
                context_path=context_path,
                dockerfile_path=dockerfile_path,
                pack=pack,
                arch=arch,
                pack_room_path=pack_room_path,
                mount_room_path=[],
                mount_project_path=[],
                mount_image=[],
                private=False,
                optimize=optimize,
                cred=[],
            )
            existing_service = await _find_room_service_by_name(
                account_client=account_client,
                project_id=resolved_project_id,
                room_name=resolved_room,
                service_name=_derive_service_name(parsed_tag=parsed_tag),
            )
        resolved_environment = _resolve_deploy_environment(
            existing_service=existing_service,
            default_environment=(
                list(runtime_container.default_environment)
                if runtime_container is not None
                else None
            ),
            parsed_environment=parsed_environment,
            parsed_secret_environment=parsed_secret_environment,
            meshagent_token_scope=meshagent_token_scope,
            token_identity=meshagent_token_identity,
            identity_override=identity_override,
        )
        environment = resolved_environment.environment
        storage = _resolve_deploy_storage(
            existing_service=existing_service,
            parsed_storage=parsed_storage,
            replace_room_mounts=len(room_mount) > 0,
            replace_project_mounts=len(project_mount) > 0,
            replace_image_mounts=len(image_mount) > 0,
            replace_empty_dir_mounts=len(empty_dir_mount) > 0,
            runtime_container=runtime_container,
        )
        _validate_packed_dockerfile_volume_mounts(
            dockerfile_metadata=packed_dockerfile_metadata,
            storage=storage,
        )
        if pack is not None:
            await _validate_deploy_environment_secrets(
                client=client,
                environment=environment,
                resolved_identity=resolved_environment.identity,
            )
        deploy_plan = _build_deploy_service_spec(
            existing_service=existing_service,
            parsed_tag=parsed_tag,
            public=not private,
            liveness=normalized_liveness,
            environment=environment,
            storage=storage,
            default_ports=packed_default_ports,
            runtime_container=runtime_container,
        )
        await _apply_deploy_plan(
            account_client=account_client,
            client=client,
            project_id=resolved_project_id,
            room_name=resolved_room,
            deploy_plan=deploy_plan,
            domain=domain,
        )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command("pack", help="Pack a local directory into an OCI image archive.")
async def pack_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    path: Annotated[str, typer.Argument(help="Local directory to pack")],
    tag: Annotated[
        Optional[str],
        typer.Option(
            "--tag",
            help=(
                "Image reference to embed in the packed archive. Required with "
                "--room, and must start with room.meshagent.com/ there."
            ),
        ),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help=(
                "Local path to write the OCI archive tar. Required unless --room is "
                "set."
            ),
        ),
    ] = None,
    base: Annotated[
        Optional[str],
        typer.Option(
            "--base",
            help="Optional base image reference. Defaults to scratch semantics.",
        ),
    ] = None,
    arch: Annotated[
        str,
        typer.Option(
            "--arch",
            help="Architecture to use when resolving --base",
        ),
    ] = DEFAULT_ARCHITECTURE,
    room_path: Annotated[
        Optional[str],
        typer.Option(
            "--room-path",
            help=(
                "Room storage path to upload the archive to when --room is set. "
                "Defaults to the repository path from --tag."
            ),
        ),
    ] = None,
) -> None:
    source_dir = Path(path)
    output_path = Path(output).expanduser().resolve() if output is not None else None
    parsed_tag = _parse_build_tag(tag) if tag is not None else None
    resolved_room = resolve_room(room)
    if resolved_room is None:
        if output_path is None:
            raise typer.BadParameter("--output is required unless --room is set")
        try:
            packed_archive = await build_oci_archive(
                source_dir=source_dir,
                output_path=output_path,
                base_image=base,
                architecture=arch,
                ref_name=parsed_tag.value if parsed_tag is not None else None,
            )
        except ImagePackError as exc:
            raise typer.BadParameter(str(exc)) from exc

        print(
            f"[green]Wrote OCI archive[/green] {packed_archive.output_path} "
            f"({packed_archive.ref_name})"
        )
        return

    if parsed_tag is None:
        raise typer.BadParameter("--tag is required when --room is set")
    _require_room_pack_tag(parsed_tag=parsed_tag)
    remote_path = _resolve_build_pack_room_path(
        parsed_tag=parsed_tag,
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
            architecture=arch,
            ref_name=parsed_tag.value,
        )
    except Exception:
        await _close_pack_clients(account_client=account_client, client=client)
        raise

    if output_path is not None:
        print(
            f"[green]Wrote OCI archive[/green] {uploaded_archive.packed_archive.output_path} "
            f"({uploaded_archive.packed_archive.ref_name})"
        )
    print(f"[green]Uploaded OCI archive[/green] {uploaded_archive.remote_path}")
    await _close_pack_clients(account_client=account_client, client=client)
