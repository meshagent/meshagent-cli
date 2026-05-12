from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import shlex
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Optional
from urllib.parse import urlparse

from aiohttp import ClientTimeout
import typer
from pydantic import ValidationError
from rich import print

from meshagent.api.http import new_client_session
from meshagent.cli import async_typer
from meshagent.cli.containers import (
    _drain_stream_plain,
    _parse_creds,
    _stream_build_job_logs_and_wait_for_exit,
    _with_client,
)
from meshagent.cli.helper import (
    get_client,
    resolve_api_url,
    resolve_room,
    resolve_project_id,
    split_container_mount,
    split_empty_dir_mount,
    split_image_mount,
)
from meshagent.api import ApiScope, RoomClient
from meshagent.api.client import (
    ConflictError,
    CreateProjectRepositoryRequest,
    CreateRepositoryTokenRequest,
    Meshagent,
    MeshagentDeploymentConfig,
    NotFoundError,
    PermissionDeniedError,
    ProjectRepository,
)
from meshagent.api.image_runtime import (
    IMAGE_RUNTIME_BASES,
    IMAGE_RUNTIME_LABEL,
    IMAGE_RUNTIME_MOUNT_PATH,
    IMAGE_RUNTIME_MOUNT_SUBPATH,
    ImageRuntimeDefinition,
)
from meshagent.api.registry_auth import DEFAULT_REGISTRY_HOST, DEFAULT_REGISTRY_USERNAME
from meshagent.api.room_server_client import (
    DockerSecret,
    LogStream,
    ServiceRuntimeState,
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
    write_build_context_archive,
)
from meshagent.cli.meshagent_images import (
    PROD_MESHAGENT_IMAGE_PREFIX,
    meshagent_image_prefix as resolve_meshagent_image_prefix,
)
from meshagent.cli.version import __version__


app = async_typer.AsyncTyper(help="Pack local directories as OCI images")
_BUILD_CONTEXT_CHUNK_SIZE = 1024 * 1024
_CLIENT_CLOSE_TIMEOUT_SECONDS = 2.0
_DEFAULT_CONTEXT_MOUNT_PATH = "/context"
_DEFAULT_REPOSITORY_TOKEN_TTL_SECONDS = 3600
_DEPLOY_LIVENESS_REQUEST_TIMEOUT_SECONDS = 2.0
_DEPLOY_WAIT_POLL_INTERVAL_SECONDS = 1.0
_GENERATED_PACK_DOCKERFILE_NAME = ".meshagent-pack.Dockerfile"
_TOKEN_ENVIRONMENT_NAMES = (
    "MESHAGENT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
_DEFAULT_MESHAGENT_IMAGE_PREFIX = PROD_MESHAGENT_IMAGE_PREFIX
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_REPOSITORY_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_REGISTRY_COMPONENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PROJECT_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
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
        help="Existing room name.",
    ),
]
_DEPLOY_DOCKERFILE_HAPPY_PATH = (
    "Happy path for a Dockerfile app: run "
    "'meshagent deploy PATH --room <room> --tag <tag> --public --domain <domain>'. "
    "Use 'meshagent config get domains.pages' to find the pages domain for "
    "--domain."
)
_DEPLOY_MISSING_DOCKERFILE_GUIDANCE = (
    "If PATH does not include a Dockerfile yet, create a minimal Dockerfile in "
    "the app directory first or create one elsewhere in PATH and pass it with "
    "--dockerfile-path."
)


def default_pack_architecture() -> str:
    configured_arch = os.environ.get("MESHAGENT_ARCH")
    if configured_arch is None:
        return DEFAULT_ARCHITECTURE
    normalized_arch = configured_arch.strip()
    if normalized_arch == "":
        return DEFAULT_ARCHITECTURE
    return normalized_arch


def _meshagent_default_image_tag(*, image: str) -> str:
    repository = image.removeprefix("meshagent/").partition(":")[0]
    if repository.startswith("shell-"):
        return f"{__version__}-esgz"
    return __version__


def replace_meshagent_image_vars(image: str) -> str:
    resolved_image = image
    meshagent_image_prefix = resolve_meshagent_image_prefix()
    if resolved_image.startswith("meshagent/"):
        meshagent_default_tag: str | None = None
        if resolved_image.endswith(":default"):
            meshagent_default_tag = _meshagent_default_image_tag(image=resolved_image)
        resolved_image = resolved_image.replace(
            "meshagent/",
            meshagent_image_prefix,
            1,
        )
        if meshagent_default_tag is not None:
            resolved_image = resolved_image.replace(
                ":default",
                f":{meshagent_default_tag}",
            )

    return resolved_image.replace("{SERVER_VERSION}", __version__)


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
class _ResolvedBuildStageInputs:
    context_path: str
    dockerfile_path: str | None
    pack_spec: _BuildPackSpec
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


@dataclass(frozen=True)
class _AppliedDeployPlanResult:
    service_id: str
    created: bool
    route_target: _RoomRouteTarget | None


@dataclass(frozen=True)
class _ActiveDeployLogStream:
    container_id: str
    stream: LogStream[None]
    task: asyncio.Task[None]


def _parse_build_pack(value: str) -> _BuildPackSpec:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("PATH cannot be empty")

    if ":" not in cleaned:
        return _BuildPackSpec(
            source_dir=Path(cleaned),
            mount_path=_DEFAULT_CONTEXT_MOUNT_PATH,
        )

    source_dir_text, mount_path = (part.strip() for part in cleaned.rsplit(":", 1))
    if source_dir_text == "" or mount_path == "":
        raise typer.BadParameter("PATH must be in the form '<path>[:<mount>]'")

    return _BuildPackSpec(
        source_dir=Path(source_dir_text),
        mount_path=mount_path,
    )


def _resolve_build_context_path(
    *,
    context_path: str | None,
    mount_path: str,
) -> str:
    if context_path is not None:
        if not context_path.startswith("/"):
            raise typer.BadParameter("--context-path must be an absolute path")
        return context_path

    if not mount_path.startswith("/"):
        raise typer.BadParameter("PATH mount path must be an absolute path")
    return mount_path


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


def _normalize_project_registry(project_registry: str | None) -> str | None:
    if project_registry is None:
        return None
    normalized = project_registry.strip()
    if normalized == "":
        return None
    return normalized


def _derive_project_registry_from_api_base(api_base: str | None) -> str | None:
    normalized_api_base = _normalize_project_registry(api_base)
    if normalized_api_base is None:
        return None

    parsed = urlparse(
        normalized_api_base
        if "://" in normalized_api_base
        else f"https://{normalized_api_base}"
    )
    hostname = parsed.hostname
    if hostname is None or hostname == "":
        return None
    if not hostname.startswith("api."):
        return None

    derived_registry = f"registry.{hostname.removeprefix('api.')}"
    if parsed.port is None:
        return derived_registry
    return f"{derived_registry}:{parsed.port}"


def _resolve_project_registry_from_config(
    *,
    config: MeshagentDeploymentConfig,
    api_url: str | None = None,
) -> str:
    configured_registry = _normalize_project_registry(config.domains.registry)
    if configured_registry is not None:
        return configured_registry

    configured_api_domain = _derive_project_registry_from_api_base(config.domains.api)
    if configured_api_domain is not None:
        return configured_api_domain

    derived_from_api_url = _derive_project_registry_from_api_base(api_url)
    if derived_from_api_url is not None:
        return derived_from_api_url

    return DEFAULT_REGISTRY_HOST


async def _get_project_registry() -> str:
    account_client = await get_client()
    try:
        config = await account_client.get_config()
    finally:
        await account_client.close()

    return _resolve_project_registry_from_config(
        config=config,
        api_url=resolve_api_url(),
    )


def _project_registry_tag_format(*, project_registry: str) -> str:
    return f"{project_registry}/<project-key>/<repository>:<tag>"


def _format_image_reference(
    *,
    registry: str | None,
    repository: str,
    tag: str | None,
) -> str:
    repository_ref = repository if registry is None else f"{registry}/{repository}"
    if tag is None:
        return repository_ref
    return f"{repository_ref}:{tag}"


def _room_registry_tag_needs_project_key(*, parsed_tag: _ParsedImageTag) -> bool:
    return len(parsed_tag.repository.split("/")) == 1


def _room_registry_tag_needs_normalization(
    *,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
) -> bool:
    if parsed_tag.registry is None:
        return True
    if parsed_tag.registry != project_registry:
        return False
    return _room_registry_tag_needs_project_key(parsed_tag=parsed_tag)


def _normalize_room_registry_tag(
    *,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
    project_key: str,
) -> _ParsedImageTag:
    if parsed_tag.registry is not None and parsed_tag.registry != project_registry:
        return parsed_tag

    resolved_registry = parsed_tag.registry
    resolved_repository = parsed_tag.repository

    if resolved_registry is None:
        resolved_registry = project_registry

    if _room_registry_tag_needs_project_key(parsed_tag=parsed_tag):
        resolved_repository = f"{project_key}/{resolved_repository}"

    if (
        resolved_registry == parsed_tag.registry
        and resolved_repository == parsed_tag.repository
    ):
        return parsed_tag

    return _parse_build_tag(
        _format_image_reference(
            registry=resolved_registry,
            repository=resolved_repository,
            tag=parsed_tag.tag,
        )
    )


async def _resolve_room_registry_target(
    *,
    project_id: str,
    parsed_tag: _ParsedImageTag,
) -> tuple[str, _ParsedImageTag]:
    account_client = await get_client()
    try:
        config = await account_client.get_config()
        project_registry = _resolve_project_registry_from_config(
            config=config,
            api_url=resolve_api_url(),
        )

        if not _room_registry_tag_needs_normalization(
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        ):
            return project_registry, parsed_tag

        if not _room_registry_tag_needs_project_key(parsed_tag=parsed_tag):
            return project_registry, _normalize_room_registry_tag(
                parsed_tag=parsed_tag,
                project_registry=project_registry,
                project_key="",
            )

        project = await account_client.get_project_info(project_id)
        return project_registry, _normalize_room_registry_tag(
            parsed_tag=parsed_tag,
            project_registry=project_registry,
            project_key=project.project_key,
        )
    finally:
        await account_client.close()


async def _resolve_deploy_image_tag(
    *,
    project_id: str,
    parsed_tag: _ParsedImageTag,
) -> _ParsedImageTag:
    if not _room_registry_tag_needs_project_key(parsed_tag=parsed_tag):
        return parsed_tag

    _project_registry, resolved_tag = await _resolve_room_registry_target(
        project_id=project_id,
        parsed_tag=parsed_tag,
    )
    return resolved_tag


def _require_room_pack_tag(
    *,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
) -> None:
    if parsed_tag.registry != project_registry:
        raise typer.BadParameter(
            "PATH requires --tag to use "
            f"{_project_registry_tag_format(project_registry=project_registry)}"
        )
    _validate_project_registry_repository(
        parsed_tag=parsed_tag,
        project_registry=project_registry,
    )


def _validate_project_registry_repository(
    *,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
) -> None:
    repository_parts = parsed_tag.repository.split("/")
    if len(repository_parts) < 2:
        raise typer.BadParameter(
            "room image tags must be in the form "
            f"{_project_registry_tag_format(project_registry=project_registry)}"
        )
    if _PROJECT_KEY_RE.fullmatch(repository_parts[0]) is None:
        raise typer.BadParameter(
            f"room image tags must use a valid project key after {project_registry}/"
        )


def _split_project_registry_repository(
    *,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
) -> tuple[str, str]:
    _validate_project_registry_repository(
        parsed_tag=parsed_tag,
        project_registry=project_registry,
    )
    project_key, repository_name = parsed_tag.repository.split("/", 1)
    return project_key, repository_name


def _find_project_repository(
    *,
    repositories: list[ProjectRepository],
    repository_name: str,
) -> ProjectRepository | None:
    for repository in repositories:
        if repository.name == repository_name:
            return repository
    return None


def _registry_create_command(*, project_id: str, repository_name: str) -> str:
    return shlex.join(
        [
            "meshagent",
            "registry",
            "create",
            "--project-id",
            project_id,
            "--name",
            repository_name,
        ]
    )


def _registry_list_command(*, project_id: str) -> str:
    return shlex.join(
        [
            "meshagent",
            "registry",
            "list",
            "--project-id",
            project_id,
        ]
    )


def _missing_project_repository_message(
    *,
    project_id: str,
    project_key: str,
    repository_name: str,
    auto_create_attempted: bool,
) -> str:
    command_hint = _registry_create_command(
        project_id=project_id,
        repository_name=repository_name,
    )
    list_hint = _registry_list_command(project_id=project_id)
    attempted_text = (
        " The CLI tried to create it automatically but your credentials do not have "
        "permission to create registry repositories."
        if auto_create_attempted
        else ""
    )
    return (
        "the target repository does not exist in the selected project: "
        f"{project_key}/{repository_name}."
        f"{attempted_text} "
        f"Create it with `{command_hint}` or inspect existing repositories with "
        f"`{list_hint}`."
    )


async def _ensure_project_repository(
    *,
    account_client: Meshagent,
    project_id: str,
    project_key: str,
    repository_name: str,
) -> ProjectRepository:
    repositories = await account_client.list_repositories(project_id=project_id)
    repository = _find_project_repository(
        repositories=repositories,
        repository_name=repository_name,
    )
    if repository is not None:
        return repository

    try:
        return await account_client.create_repository(
            project_id=project_id,
            repository=CreateProjectRepositoryRequest(
                name=repository_name,
                description="",
                annotations={},
            ),
        )
    except ConflictError:
        repositories = await account_client.list_repositories(project_id=project_id)
        repository = _find_project_repository(
            repositories=repositories,
            repository_name=repository_name,
        )
        if repository is not None:
            return repository
        raise
    except PermissionDeniedError as exc:
        raise typer.BadParameter(
            _missing_project_repository_message(
                project_id=project_id,
                project_key=project_key,
                repository_name=repository_name,
                auto_create_attempted=True,
            )
        ) from exc


async def _resolve_project_registry_build_credentials(
    *,
    account_client: Meshagent,
    project_id: str,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
) -> list[DockerSecret]:
    if parsed_tag.registry != project_registry:
        return []

    expected_project_key, repository_name = _split_project_registry_repository(
        parsed_tag=parsed_tag,
        project_registry=project_registry,
    )
    project = await account_client.get_project_info(project_id)
    if project.project_key != expected_project_key:
        raise typer.BadParameter(
            "the image tag project key does not match the selected project: "
            f"expected {project_registry}/{project.project_key}/..., "
            f"got {project_registry}/{expected_project_key}/..."
        )

    repository = await _ensure_project_repository(
        account_client=account_client,
        project_id=project_id,
        project_key=expected_project_key,
        repository_name=repository_name,
    )

    token = await account_client.create_repository_token(
        project_id=project_id,
        repository_id=repository.id,
        request=CreateRepositoryTokenRequest(
            actions=["pull", "push"],
            expires_in_seconds=_DEFAULT_REPOSITORY_TOKEN_TTL_SECONDS,
        ),
    )
    return [
        DockerSecret(
            registry=project_registry,
            username=DEFAULT_REGISTRY_USERNAME,
            password=token.token,
        )
    ]


def _generated_pack_dockerfile_path(*, mount_path: str) -> str:
    return posixpath.join(mount_path, _GENERATED_PACK_DOCKERFILE_NAME)


def _build_generated_pack_dockerfile(*, base_image: str | None) -> bytes:
    resolved_base_image = "scratch" if base_image is None else base_image.strip()
    if resolved_base_image == "":
        raise typer.BadParameter("--base cannot be empty")

    return f"FROM {resolved_base_image}\nCOPY . /\n".encode("utf-8")


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
    pack: str,
) -> _ResolvedBuildStageInputs:
    pack_spec = _parse_build_pack(pack)
    resolved_context_path = _resolve_build_context_path(
        context_path=context_path,
        mount_path=pack_spec.mount_path,
    )
    dockerfile_path = _infer_packed_dockerfile_path(
        pack_spec=pack_spec,
        context_path=resolved_context_path,
        dockerfile_path=dockerfile_path,
    )

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
        context_path=resolved_context_path,
        dockerfile_path=dockerfile_path,
        pack_spec=pack_spec,
        local_packed_dockerfile=local_packed_dockerfile,
        preserved_packed_build_paths=preserved_packed_build_paths,
    )


def _default_builder_name(*, client: RoomClient) -> str:
    del client
    return "builder"


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
    default_identity: str,
    api_scope: ApiScope,
    identity_override: str | None,
) -> TokenValue:
    identity = identity_override if identity_override is not None else default_identity
    return TokenValue(identity=identity, api=api_scope, role="agent")


def _resolve_deploy_identity(
    *,
    existing_environment: list[EnvironmentVariable] | None,
    default_identity: str,
    identity_override: str | None,
) -> str:
    if identity_override is not None:
        return identity_override

    for env_name in _TOKEN_ENVIRONMENT_NAMES:
        for env_var in existing_environment or []:
            if env_var.name != env_name or env_var.token is None:
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
        if env_var.name not in _TOKEN_ENVIRONMENT_NAMES or env_var.token is None:
            continue
        environment[index] = env_var.model_copy(
            update={
                "token": env_var.token.model_copy(
                    update={"identity": identity},
                )
            }
        )


def _upsert_environment_token_variables(
    *,
    environment: list[EnvironmentVariable],
    token_value: TokenValue,
) -> None:
    for env_name in _TOKEN_ENVIRONMENT_NAMES:
        _upsert_environment_variable(
            environment=environment,
            env_var=EnvironmentVariable(
                name=env_name,
                token=token_value.model_copy(deep=True),
            ),
        )


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


def _build_deploy_environment(
    *,
    default_environment: list[EnvironmentVariable] | None,
    parsed_environment: list[EnvironmentVariable],
    parsed_secret_environment: list[_ParsedEnvironmentSecretVariable],
    meshagent_token_scope: ApiScope | None,
    token_identity: str,
    identity_override: str | None,
) -> _ResolvedDeployEnvironment:
    environment = [
        env_var.model_copy(deep=True) for env_var in (default_environment or [])
    ]

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
        _upsert_environment_token_variables(
            environment=environment,
            token_value=_resolve_meshagent_token_value(
                default_identity=token_identity,
                api_scope=meshagent_token_scope,
                identity_override=identity_override,
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
    del existing_service
    return _build_deploy_environment(
        default_environment=default_environment,
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


def _find_service_port(
    *,
    service_spec: ServiceSpec,
    port: str,
) -> PortSpec | None:
    for service_port in service_spec.ports or []:
        if str(service_port.num) == port:
            return service_port
    return None


def _resolve_domain_liveness_path(
    *,
    service_spec: ServiceSpec,
    route_target: _RoomRouteTarget,
) -> str | None:
    service_port = _find_service_port(service_spec=service_spec, port=route_target.port)
    if service_port is None:
        return None
    if service_port.type == "tcp":
        return None
    return service_port.liveness


def _build_domain_liveness_url(*, domain: str, liveness_path: str) -> str:
    parsed = urlparse(domain if "://" in domain else f"https://{domain}")
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    base_path = parsed.path
    if netloc == "":
        netloc = parsed.path
        base_path = ""
    if base_path != "/" and base_path.endswith("/"):
        base_path = base_path.rstrip("/")
    return f"{scheme}://{netloc}{base_path}{liveness_path}"


async def _probe_liveness_url(*, url: str) -> bool:
    try:
        async with new_client_session(
            timeout=ClientTimeout(total=_DEPLOY_LIVENESS_REQUEST_TIMEOUT_SECONDS)
        ) as session:
            async with session.get(
                url,
                headers={
                    "Accept": "*/*",
                    "User-Agent": f"meshagent-cli/{__version__}",
                },
            ) as resp:
                status_code = resp.status
    except Exception:
        return False
    return 200 <= status_code < 400


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
) -> _AppliedDeployPlanResult:
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
    return _AppliedDeployPlanResult(
        service_id=deploy_result.service_id,
        created=deploy_result.created,
        route_target=route_target,
    )


def _format_deploy_room_not_found_message(*, room_name: str) -> str:
    return (
        f"Room does not exist: {room_name}\n"
        "Create it first with "
        f"'meshagent rooms create --name {shlex.quote(room_name)} --if-not-exists', "
        "then retry deploy."
    )


async def _get_service_runtime_state(
    *,
    client: RoomClient,
    service_id: str,
) -> ServiceRuntimeState | None:
    service_list = await client.services.list_with_state()
    return service_list.service_states.get(service_id)


def _start_deploy_log_stream(
    *,
    client: RoomClient,
    container_id: str,
) -> _ActiveDeployLogStream:
    stream = client.containers.logs(container_id=container_id, follow=True)
    task = asyncio.create_task(_drain_stream_plain(stream, show_progress=False))
    return _ActiveDeployLogStream(container_id=container_id, stream=stream, task=task)


async def _stop_deploy_log_stream(
    *,
    active_logs: _ActiveDeployLogStream | None,
) -> None:
    if active_logs is None:
        return
    if not active_logs.task.done():
        await asyncio.gather(active_logs.stream.cancel(), return_exceptions=True)
    await asyncio.gather(active_logs.task, return_exceptions=True)


def _print_service_exited_before_live(
    *,
    service_name: str,
    service_id: str,
    exit_code: int | None,
) -> None:
    exit_code_text = str(exit_code) if exit_code is not None else "unknown"
    print(
        "[red]Service container exited before the service was live:[/] "
        f"{service_name} ({service_id}), exit code {exit_code_text}"
    )


async def _wait_for_deployed_service_live(
    *,
    client: RoomClient,
    service_id: str,
    service_name: str,
    previous_container_id: str | None,
    domain: str | None,
    liveness_path: str | None,
) -> None:
    active_logs: _ActiveDeployLogStream | None = None
    liveness_url = (
        _build_domain_liveness_url(domain=domain, liveness_path=liveness_path)
        if domain is not None and liveness_path is not None
        else None
    )

    print(f"[cyan]Waiting for service to go live:[/] {service_name} ({service_id})")
    if domain is not None and liveness_url is not None:
        print(f"[cyan]Waiting for liveness URL:[/] {liveness_url}")
    elif domain is not None:
        print(
            f"[yellow]Route created for {domain}, but the service has no HTTP liveness path to probe.[/]"
        )

    try:
        while True:
            state = await _get_service_runtime_state(
                client=client, service_id=service_id
            )
            if state is None:
                await asyncio.sleep(_DEPLOY_WAIT_POLL_INTERVAL_SECONDS)
                continue

            container_id = state.container_id
            if (
                previous_container_id is not None
                and container_id == previous_container_id
            ):
                await asyncio.sleep(_DEPLOY_WAIT_POLL_INTERVAL_SECONDS)
                continue

            if state.restart_count > 0:
                _print_service_exited_before_live(
                    service_name=service_name,
                    service_id=service_id,
                    exit_code=state.last_exit_code,
                )
                raise typer.Exit(code=1)

            if active_logs is None and container_id is not None:
                print(f"[cyan]Tailing container logs:[/] {container_id}")
                active_logs = _start_deploy_log_stream(
                    client=client,
                    container_id=container_id,
                )
            elif (
                active_logs is not None
                and container_id is not None
                and container_id != active_logs.container_id
            ):
                _print_service_exited_before_live(
                    service_name=service_name,
                    service_id=service_id,
                    exit_code=state.last_exit_code,
                )
                raise typer.Exit(code=1)

            if container_id is None or state.state != "running":
                await asyncio.sleep(_DEPLOY_WAIT_POLL_INTERVAL_SECONDS)
                continue

            if liveness_url is None:
                print(f"[green]Service is live:[/] {service_name} ({service_id})")
                return

            if await _probe_liveness_url(url=liveness_url):
                print(f"[green]Liveness URL responded:[/] {liveness_url}")
                return

            await asyncio.sleep(_DEPLOY_WAIT_POLL_INTERVAL_SECONDS)
    finally:
        await _stop_deploy_log_stream(active_logs=active_logs)


async def _iter_file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as file_obj:
        while True:
            chunk = await asyncio.to_thread(
                file_obj.read,
                _BUILD_CONTEXT_CHUNK_SIZE,
            )
            if chunk == b"":
                return
            yield chunk


async def _build_local_context_archive(
    *,
    source_dir: Path,
    preserved_paths: frozenset[str],
    injected_files: dict[str, bytes] | None = None,
) -> tuple[Path, int, tempfile.TemporaryDirectory[str]]:
    temp_dir = tempfile.TemporaryDirectory(prefix="meshagent-build-context-")
    archive_path = Path(temp_dir.name) / "context.tar"
    try:
        await asyncio.to_thread(
            write_build_context_archive,
            source_dir=source_dir,
            output_path=archive_path,
            preserved_paths=preserved_paths,
            injected_files=injected_files,
        )
        archive_size = archive_path.stat().st_size
        return archive_path, archive_size, temp_dir
    except Exception:
        temp_dir.cleanup()
        raise


async def _run_image_pack_stage(
    *,
    resolved_project_id: str | None,
    resolved_room: str,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
    source_dir: Path,
    base_image: str | None,
) -> None:
    account_client, client = await _with_client(
        project_id=resolved_project_id,
        room=resolved_room,
    )
    context_archive_temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if resolved_project_id is None:
            raise typer.BadParameter(
                "a project id is required to pack a room registry image"
            )
        _require_room_pack_tag(
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        )
        registry_credentials = await _resolve_project_registry_build_credentials(
            account_client=account_client,
            project_id=resolved_project_id,
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        )
        source_pack_spec = _BuildPackSpec(
            source_dir=source_dir,
            mount_path=_DEFAULT_CONTEXT_MOUNT_PATH,
        )
        (
            archive_path,
            archive_size,
            context_archive_temp_dir,
        ) = await _build_local_context_archive(
            source_dir=source_dir,
            preserved_paths=_preserved_packed_build_paths(
                pack_spec=source_pack_spec,
                context_path=_DEFAULT_CONTEXT_MOUNT_PATH,
                dockerfile_path=None,
            ),
            injected_files={
                _GENERATED_PACK_DOCKERFILE_NAME: _build_generated_pack_dockerfile(
                    base_image=base_image
                )
            },
        )
        build_id = await client.containers.build(
            tags=[parsed_tag.value],
            mount_path=_DEFAULT_CONTEXT_MOUNT_PATH,
            context_path=_DEFAULT_CONTEXT_MOUNT_PATH,
            dockerfile_path=_generated_pack_dockerfile_path(
                mount_path=_DEFAULT_CONTEXT_MOUNT_PATH
            ),
            optimize_image=True,
            private=False,
            credentials=registry_credentials,
            builder_name=_default_builder_name(client=client),
            chunks=_iter_file_chunks(archive_path),
            size=archive_size,
        )
        exit_code = await _stream_build_job_logs_and_wait_for_exit(
            client=client,
            build_id=build_id,
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        if context_archive_temp_dir is not None:
            context_archive_temp_dir.cleanup()
        await client.__aexit__(None, None, None)
        await account_client.close()


async def _run_image_build_stage(
    *,
    resolved_project_id: str | None,
    resolved_room: str,
    parsed_tag: _ParsedImageTag,
    project_registry: str,
    context_path: str | None,
    dockerfile_path: str | None,
    pack: str,
    arch: str,
    builder_name: str | None,
    private: bool,
    optimize: bool,
    cred: list[str],
    add_latest_tag: bool = False,
) -> None:
    del arch
    build_inputs = _resolve_build_stage_inputs(
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        pack=pack,
    )

    account_client, client = await _with_client(
        project_id=resolved_project_id,
        room=resolved_room,
    )
    context_archive_temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        credentials = _parse_creds(cred)
        if resolved_project_id is None:
            raise typer.BadParameter(
                "a project id is required to build a room registry image"
            )
        _require_room_pack_tag(
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        )
        registry_credentials = await _resolve_project_registry_build_credentials(
            account_client=account_client,
            project_id=resolved_project_id,
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        )
        if len(registry_credentials) > 0:
            credentials = [*registry_credentials, *credentials]
        resolved_builder_name = (
            builder_name
            if builder_name is not None
            else _default_builder_name(client=client)
        )
        (
            archive_path,
            archive_size,
            context_archive_temp_dir,
        ) = await _build_local_context_archive(
            source_dir=build_inputs.pack_spec.source_dir,
            preserved_paths=build_inputs.preserved_packed_build_paths,
        )
        tags = [parsed_tag.value]
        if add_latest_tag and parsed_tag.latest_ref != parsed_tag.value:
            tags.append(parsed_tag.latest_ref)
        build_id = await client.containers.build(
            tags=tags,
            mount_path=build_inputs.pack_spec.mount_path,
            context_path=build_inputs.context_path,
            dockerfile_path=build_inputs.dockerfile_path,
            optimize_image=optimize,
            private=private,
            credentials=credentials,
            builder_name=resolved_builder_name,
            chunks=_iter_file_chunks(archive_path),
            size=archive_size,
        )
        exit_code = await _stream_build_job_logs_and_wait_for_exit(
            client=client, build_id=build_id
        )
        if exit_code != 0:
            raise typer.Exit(code=exit_code)
    finally:
        if context_archive_temp_dir is not None:
            context_archive_temp_dir.cleanup()
        await client.__aexit__(None, None, None)
        await account_client.close()


def _validate_deploy_build_stage_options(
    *,
    pack: str | None,
    context_path: str | None,
    dockerfile_path: str | None,
    builder_name: str | None,
    optimize: bool,
    cred: list[str],
    add_latest_tag: bool,
) -> None:
    if pack is not None:
        return

    invalid_options: list[str] = []
    if context_path is not None:
        invalid_options.append("--context-path")
    if dockerfile_path is not None:
        invalid_options.append("--dockerfile-path")
    if builder_name is not None:
        invalid_options.append("--builder-name")
    if not optimize:
        invalid_options.append("--no-optimize")
    if len(cred) > 0:
        invalid_options.append("--cred")
    if add_latest_tag:
        invalid_options.append("--latest")

    if len(invalid_options) == 0:
        return

    if len(invalid_options) == 1:
        raise typer.BadParameter(f"{invalid_options[0]} requires PATH")

    raise typer.BadParameter(f"{', '.join(invalid_options)} require PATH")


@app.async_command(
    "build",
    help="Build a container image inside a room.",
    hidden=True,
)
async def build_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    pack: Annotated[
        str,
        typer.Argument(
            ...,
            metavar="PATH",
            help=(
                "Local directory to stream as the build context. Format "
                "'<path>[:<mount>]'. Defaults mount to /context."
            ),
        ),
    ],
    tag: Annotated[
        str,
        typer.Option(
            ...,
            help=(
                "Image tag to build. Supports <repository>:<tag>, "
                "<project-key>/<repository>:<tag>, or "
                "<registry>/<project-key>/<repository>:<tag>. Shorthand forms "
                "resolve against the configured MeshAgent registry."
            ),
        ),
    ],
    context_path: Annotated[
        Optional[str],
        typer.Option(
            "--context-path",
            help=(
                "Build context path inside the streamed build context (absolute "
                "path). Defaults to the PATH mount path."
            ),
        ),
    ] = None,
    dockerfile_path: Annotated[
        Optional[str],
        typer.Option(
            "--dockerfile-path",
            help=(
                "Optional Dockerfile path inside the streamed build context "
                "(absolute path)."
            ),
        ),
    ] = None,
    builder_name: Annotated[
        Optional[str],
        typer.Option(
            "--builder-name",
            help="Optional reusable builder name for streamed local builds.",
        ),
    ] = None,
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
    latest: Annotated[
        bool,
        typer.Option(
            "--latest",
            help="Also publish the built image as :latest in the same repository.",
        ),
    ] = False,
) -> None:
    parsed_tag = _parse_build_tag(tag)
    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")
    resolved_project_id = await resolve_project_id(project_id=project_id)
    project_registry, parsed_tag = await _resolve_room_registry_target(
        project_id=resolved_project_id,
        parsed_tag=parsed_tag,
    )
    _require_room_pack_tag(
        parsed_tag=parsed_tag,
        project_registry=project_registry,
    )
    await _run_image_build_stage(
        resolved_project_id=resolved_project_id,
        resolved_room=resolved_room,
        parsed_tag=parsed_tag,
        project_registry=project_registry,
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        pack=pack,
        arch=default_pack_architecture(),
        builder_name=builder_name,
        private=private,
        optimize=optimize,
        cred=cred,
        add_latest_tag=latest,
    )


@app.async_command(
    "deploy",
    help=(
        "Create or update a room service from an image, optionally building it "
        "first. The target room must already exist. "
        f"{_DEPLOY_DOCKERFILE_HAPPY_PATH} {_DEPLOY_MISSING_DOCKERFILE_GUIDANCE}"
    ),
    hidden=True,
)
async def deploy_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    pack: Annotated[
        Optional[str],
        typer.Argument(
            metavar="PATH",
            help=(
                "Local directory to stream as the build context before deploy. "
                "Format '<path>[:<mount>]'. Defaults mount to /context. PATH is "
                "typically the app directory you want to deploy."
            ),
        ),
    ] = None,
    tag: Annotated[
        str,
        typer.Option(
            ...,
            help=(
                "Image tag to deploy, e.g. repo/name:tag. When used with PATH, "
                "shorthand <repository>:<tag> and "
                "<project-key>/<repository>:<tag> resolve against the "
                "configured MeshAgent registry."
            ),
        ),
    ],
    context_path: Annotated[
        Optional[str],
        typer.Option(
            "--context-path",
            help=(
                "Build context path inside the packed build context (absolute path). "
                "Only used with PATH."
            ),
        ),
    ] = None,
    dockerfile_path: Annotated[
        Optional[str],
        typer.Option(
            "--dockerfile-path",
            help=(
                "Optional Dockerfile path inside the packed build context (absolute "
                "path). Only used with PATH. Use this when the app directory has "
                "no top-level Dockerfile or when you create the Dockerfile under "
                "a different path."
            ),
        ),
    ] = None,
    optimize: Annotated[
        bool,
        typer.Option(
            "--optimize/--no-optimize",
            help=(
                "Whether to optimize room image outputs to eStargz during the build "
                "stage. Enabled by default. Only used with PATH."
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
    builder_name: Annotated[
        Optional[str],
        typer.Option(
            "--builder-name",
            help="Optional reusable builder name for streamed local pack builds.",
        ),
    ] = None,
    latest: Annotated[
        bool,
        typer.Option(
            "--latest",
            help=(
                "Also publish the built PATH image as :latest in the same "
                "repository. Only used with PATH."
            ),
        ),
    ] = False,
    domain: Annotated[
        Optional[str],
        typer.Option(
            "--domain",
            help=(
                "Create or update a room route for the deployed service and "
                "return a public URL. Use this with --public when you need an "
                "external URL from deploy. Use 'meshagent config get domains.pages' "
                "to find the pages domain for --domain. Requires exactly one "
                "published service port."
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
    wait: Annotated[
        bool,
        typer.Option(
            "--wait/--no-wait",
            help=(
                "Wait for the deployed service to start, stream container logs, "
                "and verify the route liveness URL when --domain is provided."
            ),
        ),
    ] = True,
) -> None:
    """Create or update a room service from an image."""
    parsed_tag = _parse_build_tag(tag)
    project_registry: str | None = None
    _validate_deploy_build_stage_options(
        pack=pack,
        context_path=context_path,
        dockerfile_path=dockerfile_path,
        builder_name=builder_name,
        optimize=optimize,
        cred=cred,
        add_latest_tag=latest,
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
    resolved_project_id = await resolve_project_id(project_id=project_id)
    if pack is not None:
        project_registry, parsed_tag = await _resolve_room_registry_target(
            project_id=resolved_project_id,
            parsed_tag=parsed_tag,
        )
        _require_room_pack_tag(
            parsed_tag=parsed_tag,
            project_registry=project_registry,
        )
    else:
        parsed_tag = await _resolve_deploy_image_tag(
            project_id=resolved_project_id,
            parsed_tag=parsed_tag,
        )
    if domain is not None:
        domain = domain.strip()
        if domain == "":
            raise typer.BadParameter("--domain cannot be empty")
    normalized_liveness = _normalize_deploy_liveness(liveness=liveness)

    packed_default_ports: list[PortSpec] | None = None
    packed_dockerfile_metadata: _PackedDockerfileMetadata | None = None
    runtime_container: _RuntimeContainerOverride | None = None
    if pack is not None:
        build_inputs = _resolve_build_stage_inputs(
            context_path=context_path,
            dockerfile_path=dockerfile_path,
            pack=pack,
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
    try:
        account_client, client = await _with_client(
            project_id=resolved_project_id,
            room=resolved_room,
        )
    except NotFoundError as exc:
        print(
            f"[red]{_format_deploy_room_not_found_message(room_name=resolved_room)}[/red]"
        )
        raise typer.Exit(1) from exc
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
            assert project_registry is not None
            await _run_image_build_stage(
                resolved_project_id=resolved_project_id,
                resolved_room=resolved_room,
                parsed_tag=parsed_tag,
                project_registry=project_registry,
                context_path=context_path,
                dockerfile_path=dockerfile_path,
                pack=pack,
                arch=default_pack_architecture(),
                builder_name=builder_name,
                private=False,
                optimize=optimize,
                cred=cred,
                add_latest_tag=latest,
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
        previous_runtime_state = (
            await _get_service_runtime_state(
                client=client,
                service_id=existing_service.id,
            )
            if existing_service is not None
            and existing_service.id is not None
            and existing_service.id != ""
            else None
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
        deploy_result = await _apply_deploy_plan(
            account_client=account_client,
            client=client,
            project_id=resolved_project_id,
            room_name=resolved_room,
            deploy_plan=deploy_plan,
            domain=domain,
        )
        if wait:
            await _wait_for_deployed_service_live(
                client=client,
                service_id=deploy_result.service_id,
                service_name=deploy_plan.spec.metadata.name,
                previous_container_id=(
                    previous_runtime_state.container_id
                    if previous_runtime_state is not None
                    else None
                ),
                domain=domain,
                liveness_path=(
                    _resolve_domain_liveness_path(
                        service_spec=deploy_plan.spec,
                        route_target=deploy_result.route_target,
                    )
                    if deploy_result.route_target is not None
                    else None
                ),
            )
    finally:
        await client.__aexit__(None, None, None)
        await account_client.close()


@app.async_command(
    "pack",
    help="Publish a local directory as an image inside a room using a generated Dockerfile.",
)
async def pack_image(
    *,
    project_id: ImageProjectIdOption = None,
    room: ImageRoomOption = None,
    path: Annotated[str, typer.Argument(help="Local directory to publish")],
    tag: Annotated[
        str,
        typer.Option(
            ...,
            "--tag",
            help=(
                "Image reference to publish. Supports <repository>:<tag>, "
                "<project-key>/<repository>:<tag>, or "
                "<registry>/<project-key>/<repository>:<tag>. Shorthand forms "
                "resolve against the configured MeshAgent registry."
            ),
        ),
    ],
    base: Annotated[
        Optional[str],
        typer.Option(
            "--base",
            help="Optional base image reference for the generated Dockerfile.",
        ),
    ] = None,
) -> None:
    source_dir = Path(path)
    parsed_tag = _parse_build_tag(tag)
    resolved_room = resolve_room(room)
    if resolved_room is None:
        raise typer.BadParameter("--room is required unless MESHAGENT_ROOM is set")
    resolved_project_id = await resolve_project_id(project_id=project_id)
    project_registry, parsed_tag = await _resolve_room_registry_target(
        project_id=resolved_project_id,
        parsed_tag=parsed_tag,
    )
    _require_room_pack_tag(
        parsed_tag=parsed_tag,
        project_registry=project_registry,
    )

    await _run_image_pack_stage(
        resolved_project_id=resolved_project_id,
        resolved_room=resolved_room,
        parsed_tag=parsed_tag,
        project_registry=project_registry,
        source_dir=source_dir,
        base_image=base,
    )
