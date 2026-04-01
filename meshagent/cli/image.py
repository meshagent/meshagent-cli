from __future__ import annotations

import asyncio
import posixpath
import queue
import re
import threading
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
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
    ServiceMetadata,
    ServiceSpec,
    TokenValue,
)
from meshagent.cli.oci_archive import (
    DEFAULT_ARCHITECTURE,
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


def _parse_env_token_scope(*, value: str) -> ApiScope:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("--env-token cannot be empty")
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
            "--env-token must be one of userDefault, agentDefault, full, "
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
) -> TokenValue:
    identity = default_identity
    role = "agent"

    for env_var in existing_environment or []:
        if env_var.name != "MESHAGENT_TOKEN" or env_var.token is None:
            continue
        if env_var.token.identity.strip() != "":
            identity = env_var.token.identity.strip()
        if env_var.token.role is not None and env_var.token.role.strip() != "":
            role = env_var.token.role.strip()
        break

    return TokenValue(identity=identity, api=api_scope, role=role)


def _merge_deploy_environment(
    *,
    existing_environment: list[EnvironmentVariable] | None,
    parsed_environment: list[EnvironmentVariable],
    env_token_scope: ApiScope | None,
    token_identity: str,
) -> list[EnvironmentVariable] | None:
    environment = [
        env_var.model_copy(deep=True) for env_var in (existing_environment or [])
    ]

    for env_var in parsed_environment:
        _upsert_environment_variable(
            environment=environment,
            env_var=env_var,
        )

    if env_token_scope is not None:
        _upsert_environment_variable(
            environment=environment,
            env_var=EnvironmentVariable(
                name="MESHAGENT_TOKEN",
                token=_resolve_meshagent_token_value(
                    existing_environment=existing_environment,
                    default_identity=token_identity,
                    api_scope=env_token_scope,
                ),
            ),
        )

    return environment or None


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


def _build_deploy_service_spec(
    *,
    existing_service: ServiceSpec | None,
    parsed_tag: _ParsedImageTag,
    public: bool | None,
    environment: list[EnvironmentVariable] | None = None,
    storage: ContainerMountSpec | None = None,
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
        existing_service.container.model_copy(update={"image": parsed_tag.value})
        if existing_service is not None and existing_service.container is not None
        else ContainerSpec(image=parsed_tag.value)
    )
    if environment is not None:
        container = container.model_copy(update={"environment": environment})
    if storage is not None:
        container = container.model_copy(update={"storage": storage})

    ports = list(existing_service.ports or []) if existing_service is not None else []
    if public is not None and len(ports) > 0:
        ports = [_update_deploy_port(port=port, public=public) for port in ports]

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
    public: bool | None,
) -> dict[str, str]:
    updated_annotations = dict(annotations)
    if public is False:
        updated_annotations[ANNOTATION_REQUEST_VALIDATION_METHOD] = (
            _COOKIE_VALIDATION_METHOD
        )
    elif (
        public is True
        and updated_annotations.get(ANNOTATION_REQUEST_VALIDATION_METHOD)
        == _COOKIE_VALIDATION_METHOD
    ):
        del updated_annotations[ANNOTATION_REQUEST_VALIDATION_METHOD]

    return updated_annotations


def _update_deploy_port(*, port: PortSpec, public: bool) -> PortSpec:
    if not port.published:
        return port.model_copy(deep=True)

    annotations = _update_request_validation_annotations(
        annotations=dict(port.annotations or {}),
        public=public,
    )
    return port.model_copy(
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
    cred: Annotated[
        list[str],
        typer.Option(
            "--cred",
            help="Docker creds (username,password) or (registry,username,password)",
        ),
    ] = [],
) -> None:
    parsed_tag = _parse_build_tag(tag)
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
    resolved_project_id = await resolve_project_id(project_id=project_id)
    local_packed_dockerfile = _resolve_local_packed_dockerfile(
        pack_spec=pack_spec,
        dockerfile_path=dockerfile_path,
    )
    if local_packed_dockerfile is not None and not local_packed_dockerfile.is_file():
        raise typer.BadParameter(
            f"packed Dockerfile does not exist locally: {local_packed_dockerfile}"
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
    try:
        if pack_spec is not None:
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
                source_dir=pack_spec.source_dir,
                remote_path=requested_packed_room_path,
                output_path=None,
                base_image=None,
                architecture=resolved_pack_architecture,
                ref_name=packed_ref_name,
            )
            packed_room_path = uploaded_packed_archive.remote_path
            context_archive_path = packed_room_path
            context_archive_ref = packed_ref_name
            context_archive_mount_path = pack_spec.mount_path
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
            len(normalized_room_mounts) > 0
            or len(normalized_project_mounts) > 0
            or len(normalized_image_mounts) > 0
        ):
            mounts.append(
                _parse_image_operation_mounts(
                    mount_room_path=normalized_room_mounts,
                    mount_project_path=normalized_project_mounts,
                    mount_image=normalized_image_mounts,
                )
            )
        build_id = await client.containers.build(
            tag=parsed_tag.value,
            mounts=mounts,
            context_path=context_path,
            dockerfile_path=dockerfile_path,
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


@app.async_command("deploy", help="Create or update a room service from an image.")
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
    env_token: Annotated[
        Optional[str],
        typer.Option(
            "--env-token",
            help=(
                "Inject MESHAGENT_TOKEN using userDefault, agentDefault, full, "
                "or a JSON ApiScope object."
            ),
        ),
    ] = None,
    private: Annotated[
        Optional[bool],
        typer.Option(
            "--private/--public",
            help=(
                "Whether published service ports should stay private or be "
                "public when they are created or updated."
            ),
        ),
    ] = None,
) -> None:
    parsed_tag = _parse_build_tag(tag)
    parsed_environment = _parse_environment_variables(values=env)
    parsed_storage = _parse_deploy_storage(
        room_mounts=room_mount,
        project_mounts=project_mount,
        image_mounts=image_mount,
        empty_dir_mounts=empty_dir_mount,
    )
    env_token_scope = (
        _parse_env_token_scope(value=env_token) if env_token is not None else None
    )

    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")
    if domain is not None:
        domain = domain.strip()
        if domain == "":
            raise typer.BadParameter("--domain cannot be empty")

    resolved_project_id = await resolve_project_id(project_id=project_id)
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
        existing_container = (
            existing_service.container if existing_service is not None else None
        )
        environment = _merge_deploy_environment(
            existing_environment=(
                existing_container.environment if existing_container else None
            ),
            parsed_environment=parsed_environment,
            env_token_scope=env_token_scope,
            token_identity=_derive_service_name(parsed_tag=parsed_tag),
        )
        storage = _merge_deploy_storage(
            existing_storage=existing_container.storage if existing_container else None,
            parsed_storage=parsed_storage,
            replace_room_mounts=len(room_mount) > 0,
            replace_project_mounts=len(project_mount) > 0,
            replace_image_mounts=len(image_mount) > 0,
            replace_empty_dir_mounts=len(empty_dir_mount) > 0,
        )
        deploy_plan = _build_deploy_service_spec(
            existing_service=existing_service,
            parsed_tag=parsed_tag,
            public=None if private is None else not private,
            environment=environment,
            storage=storage,
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
