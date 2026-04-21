from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import aiohttp
import pathspec
import zstandard


DEFAULT_ARCHITECTURE = "amd64"
DEFAULT_OS = "linux"
_OCI_LAYOUT_MEDIA_TYPE = "application/vnd.oci.image.layout.v1+json"
_OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_OCI_LAYER_TAR_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
_OCI_LAYER_GZIP_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
_OCI_LAYER_ZSTD_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+zstd"
_DOCKER_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
_DOCKER_MANIFEST_LIST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
_DOCKER_CONFIG_MEDIA_TYPE = "application/vnd.docker.container.image.v1+json"
_DOCKER_LAYER_TAR_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar"
_DOCKER_LAYER_GZIP_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"
_DOCKER_FOREIGN_LAYER_GZIP_MEDIA_TYPE = (
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"
)
_MANIFEST_MEDIA_TYPES = frozenset(
    {
        _OCI_MANIFEST_MEDIA_TYPE,
        _DOCKER_MANIFEST_MEDIA_TYPE,
    }
)
_INDEX_MEDIA_TYPES = frozenset(
    {
        _OCI_INDEX_MEDIA_TYPE,
        _DOCKER_MANIFEST_LIST_MEDIA_TYPE,
    }
)
_MANIFEST_ACCEPT_HEADER = ", ".join(
    [
        _OCI_INDEX_MEDIA_TYPE,
        _OCI_MANIFEST_MEDIA_TYPE,
        _DOCKER_MANIFEST_LIST_MEDIA_TYPE,
        _DOCKER_MANIFEST_MEDIA_TYPE,
    ]
)
_CHUNK_SIZE = 1024 * 1024
_ZSTD_COMPRESSION_LEVEL = 3


class ImagePackError(Exception):
    pass


@dataclass(frozen=True)
class BlobDescriptor:
    digest: str
    size: int
    media_type: str

    @property
    def digest_hex(self) -> str:
        algorithm, separator, digest_hex = self.digest.partition(":")
        if algorithm != "sha256" or separator == "" or digest_hex == "":
            raise ImagePackError(
                f"only sha256 blob digests are supported, got {self.digest}"
            )
        return digest_hex


@dataclass(frozen=True)
class BaseImageSource:
    architecture: str
    os_name: str
    variant: str | None
    config: dict[str, object]
    layers: list[BlobDescriptor]
    fetch_blob_to_path: Callable[[BlobDescriptor, Path], Awaitable[None]]


@dataclass(frozen=True)
class PackedOciArchive:
    output_path: Path
    ref_name: str
    architecture: str
    os_name: str
    manifest_digest: str


@dataclass(frozen=True)
class _ArchiveBytesEntry:
    archive_path: str
    data: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class _ArchiveFileEntry:
    archive_path: str
    file_path: Path
    size: int


@dataclass(frozen=True)
class _PreparedLayerBlob:
    descriptor: BlobDescriptor
    diff_id: str
    file_path: Path


@dataclass
class _PreparedOciArchive:
    packed_archive: PackedOciArchive
    temp_dir: tempfile.TemporaryDirectory[str]
    entries: list[_ArchiveBytesEntry | _ArchiveFileEntry]

    def cleanup(self) -> None:
        self.temp_dir.cleanup()


@dataclass(frozen=True)
class _ImageReference:
    registry: str
    repository: str
    reference: str
    use_https: bool

    @property
    def api_base(self) -> str:
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.registry}/v2/{self.repository}"


class DockerIgnore:
    def __init__(self, dockerignore_path: Path):
        if dockerignore_path.exists():
            patterns = dockerignore_path.read_text(encoding="utf-8").splitlines()
        else:
            patterns = []

        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def matches(self, path: str) -> bool:
        return self._spec.match_file(path)


def _now_rfc3339() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0

    with path.open("rb") as file:
        while True:
            chunk = file.read(_CHUNK_SIZE)
            if chunk == b"":
                break
            digest.update(chunk)
            total += len(chunk)

    return f"sha256:{digest.hexdigest()}", total


def _clone_json_dict(value: dict[str, object]) -> dict[str, object]:
    return json.loads(json.dumps(value))


def _require_dict(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ImagePackError(f"{context} must be an object")
    return dict(value)


def _require_list(value: object, *, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ImagePackError(f"{context} must be an array")
    return list(value)


def _require_str(value: object, *, context: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ImagePackError(f"{context} must be a non-empty string")
    return value


def _maybe_str(value: object, *, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ImagePackError(f"{context} must be a non-empty string when provided")
    return value


def _parse_blob_descriptor(
    value: object,
    *,
    context: str,
    media_type_mapper: Callable[[str], str] | None = None,
) -> BlobDescriptor:
    descriptor = _require_dict(value, context=context)
    digest = _require_str(descriptor.get("digest"), context=f"{context} digest")
    size = descriptor.get("size")
    if not isinstance(size, int) or size < 0:
        raise ImagePackError(f"{context} size must be a non-negative integer")

    media_type = _require_str(
        descriptor.get("mediaType"), context=f"{context} mediaType"
    )
    if media_type_mapper is not None:
        media_type = media_type_mapper(media_type)

    return BlobDescriptor(digest=digest, size=size, media_type=media_type)


def _normalize_layer_media_type(media_type: str) -> str:
    if media_type in {
        _OCI_LAYER_TAR_MEDIA_TYPE,
        _OCI_LAYER_GZIP_MEDIA_TYPE,
        _OCI_LAYER_ZSTD_MEDIA_TYPE,
    }:
        return media_type
    if media_type == _DOCKER_LAYER_TAR_MEDIA_TYPE:
        return _OCI_LAYER_TAR_MEDIA_TYPE
    if media_type == _DOCKER_LAYER_GZIP_MEDIA_TYPE:
        return _OCI_LAYER_GZIP_MEDIA_TYPE
    if media_type == _DOCKER_FOREIGN_LAYER_GZIP_MEDIA_TYPE:
        raise ImagePackError(
            "foreign base-image layers are not supported for local OCI packing"
        )
    raise ImagePackError(f"unsupported base-image layer media type: {media_type}")


def _default_ref_name(source_dir: Path) -> str:
    sanitized = re.sub(r"[^a-z0-9._-]+", "-", source_dir.name.lower()).strip("-")
    if sanitized == "":
        sanitized = "image"
    return f"{sanitized}:latest"


def _pack_history_entry(*, created_at: str, source_dir: Path) -> dict[str, object]:
    return {
        "created": created_at,
        "created_by": f"meshagent local-oci-archive {source_dir}",
        "comment": "packed local files into an OCI archive",
    }


def _build_config_from_scratch(
    *,
    architecture: str,
    os_name: str,
    layer_diff_id: str,
    created_at: str,
    source_dir: Path,
) -> dict[str, object]:
    return {
        "created": created_at,
        "architecture": architecture,
        "os": os_name,
        "config": {},
        "rootfs": {
            "type": "layers",
            "diff_ids": [layer_diff_id],
        },
        "history": [_pack_history_entry(created_at=created_at, source_dir=source_dir)],
    }


def _build_config_from_base(
    *,
    base_config: dict[str, object],
    layer_diff_id: str,
    created_at: str,
    source_dir: Path,
) -> dict[str, object]:
    merged = _clone_json_dict(base_config)

    rootfs = _require_dict(merged.get("rootfs"), context="base image config rootfs")
    rootfs_type = _require_str(rootfs.get("type"), context="base image config rootfs")
    if rootfs_type != "layers":
        raise ImagePackError(
            f"unsupported base image rootfs type for packing: {rootfs_type}"
        )

    diff_ids_raw = _require_list(
        rootfs.get("diff_ids"), context="base image config rootfs diff_ids"
    )
    diff_ids = [
        _require_str(diff_id, context="base image config diff_id")
        for diff_id in diff_ids_raw
    ]
    diff_ids.append(layer_diff_id)
    rootfs["diff_ids"] = diff_ids
    merged["rootfs"] = rootfs

    history_raw = merged.get("history")
    if history_raw is None:
        history: list[object] = []
    else:
        history = _require_list(history_raw, context="base image config history")
        for index, entry in enumerate(history):
            _require_dict(entry, context=f"base image config history[{index}]")

    history.append(_pack_history_entry(created_at=created_at, source_dir=source_dir))
    merged["history"] = history
    merged["created"] = created_at
    return merged


def _normalize_tar_info(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.pax_headers = {}
    return member


def _should_skip_path(
    *,
    absolute_path: Path,
    relative_path: Path,
    docker_ignore: DockerIgnore | None,
    excluded_paths: set[Path],
    preserved_paths: frozenset[str],
    is_dir: bool,
) -> bool:
    if absolute_path.resolve() in excluded_paths:
        return True

    name = absolute_path.name
    if name.startswith("._"):
        return True

    relative_posix = relative_path.as_posix()
    if relative_posix in preserved_paths:
        return False
    if is_dir and any(
        preserved_path.startswith(f"{relative_posix}/")
        for preserved_path in preserved_paths
    ):
        return False
    if relative_posix == ".dockerignore":
        return True

    if docker_ignore is None:
        return False

    if docker_ignore.matches(relative_posix):
        return True
    if is_dir and docker_ignore.matches(f"{relative_posix}/"):
        return True
    return False


def _add_path_to_tar(
    archive: tarfile.TarFile,
    *,
    absolute_path: Path,
    relative_path: Path,
) -> None:
    tar_info = archive.gettarinfo(str(absolute_path), arcname=relative_path.as_posix())
    tar_info = _normalize_tar_info(tar_info)

    if tar_info.ischr() or tar_info.isblk() or tar_info.isfifo():
        raise ImagePackError(
            f"unsupported filesystem entry for image packing: {absolute_path}"
        )

    if tar_info.isfile():
        with absolute_path.open("rb") as file:
            archive.addfile(tar_info, fileobj=file)
        return

    archive.addfile(tar_info)


def _write_layer_tar(
    *,
    source_dir: Path,
    layer_path: Path,
    excluded_paths: set[Path],
    preserved_paths: frozenset[str],
) -> str:
    dockerignore_path = source_dir / ".dockerignore"
    docker_ignore = (
        DockerIgnore(dockerignore_path) if dockerignore_path.exists() else None
    )

    with tarfile.open(layer_path, mode="w") as archive:
        for current_root, dirnames, filenames in os.walk(
            source_dir, topdown=True, followlinks=False
        ):
            current_path = Path(current_root)
            relative_root = current_path.relative_to(source_dir)

            dirnames.sort()
            filenames.sort()

            kept_dirnames: list[str] = []
            for dirname in dirnames:
                absolute_path = current_path / dirname
                relative_path = relative_root / dirname
                if _should_skip_path(
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                    docker_ignore=docker_ignore,
                    excluded_paths=excluded_paths,
                    preserved_paths=preserved_paths,
                    is_dir=True,
                ):
                    continue

                kept_dirnames.append(dirname)
                _add_path_to_tar(
                    archive,
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                )

            dirnames[:] = kept_dirnames

            for filename in filenames:
                absolute_path = current_path / filename
                relative_path = relative_root / filename
                if _should_skip_path(
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                    docker_ignore=docker_ignore,
                    excluded_paths=excluded_paths,
                    preserved_paths=preserved_paths,
                    is_dir=False,
                ):
                    continue

                _add_path_to_tar(
                    archive,
                    absolute_path=absolute_path,
                    relative_path=relative_path,
                )

    digest, _size = _sha256_file(layer_path)
    return digest


def write_build_context_archive(
    *,
    source_dir: Path,
    output_path: Path,
    preserved_paths: frozenset[str] = frozenset(),
    injected_files: dict[str, bytes] | None = None,
) -> None:
    _write_layer_tar(
        source_dir=source_dir,
        layer_path=output_path,
        excluded_paths=set(),
        preserved_paths=preserved_paths,
    )
    if injected_files is None or len(injected_files) == 0:
        return

    with tarfile.open(output_path, mode="a") as archive:
        for archive_path, data in injected_files.items():
            _add_bytes_to_archive(
                archive,
                path=archive_path,
                data=data,
            )


def _compress_zstd_to_path(*, source_path: Path, destination_path: Path) -> None:
    compressor = zstandard.ZstdCompressor(level=_ZSTD_COMPRESSION_LEVEL)
    with source_path.open("rb") as source, destination_path.open("wb") as destination:
        with compressor.stream_writer(destination) as writer:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if chunk == b"":
                    break
                writer.write(chunk)


def _prepare_layer_blob(
    *,
    source_dir: Path,
    temp_path: Path,
    excluded_paths: set[Path],
    preserved_paths: frozenset[str],
) -> _PreparedLayerBlob:
    uncompressed_layer_path = temp_path / "layer.tar"
    diff_id = _write_layer_tar(
        source_dir=source_dir,
        layer_path=uncompressed_layer_path,
        excluded_paths=excluded_paths,
        preserved_paths=preserved_paths,
    )

    compressed_layer_path = temp_path / "layer.tar.zst"
    _compress_zstd_to_path(
        source_path=uncompressed_layer_path,
        destination_path=compressed_layer_path,
    )
    digest, size = _sha256_file(compressed_layer_path)
    return _PreparedLayerBlob(
        descriptor=BlobDescriptor(
            digest=digest,
            size=size,
            media_type=_OCI_LAYER_ZSTD_MEDIA_TYPE,
        ),
        diff_id=diff_id,
        file_path=compressed_layer_path,
    )


def _add_bytes_to_archive(
    archive: tarfile.TarFile, *, path: str, data: bytes, mode: int = 0o644
) -> None:
    info = tarfile.TarInfo(name=path)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    info = _normalize_tar_info(info)
    archive.addfile(info, fileobj=io.BytesIO(data))


def _add_file_to_archive(
    archive: tarfile.TarFile, *, archive_path: str, file_path: Path, size: int
) -> None:
    info = tarfile.TarInfo(name=archive_path)
    info.size = size
    info.mode = 0o644
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    info = _normalize_tar_info(info)
    with file_path.open("rb") as file:
        archive.addfile(info, fileobj=file)


async def _prepare_oci_archive_with_base(
    *,
    source_dir: Path,
    output_path: Path,
    architecture: str,
    ref_name: str,
    base_source: BaseImageSource | None,
    preserved_paths: frozenset[str],
) -> _PreparedOciArchive:
    created_at = _now_rfc3339()
    excluded_paths = {output_path.resolve()}

    temp_dir = tempfile.TemporaryDirectory(prefix="meshagent-oci-pack-")
    temp_path = Path(temp_dir.name)
    local_layer = _prepare_layer_blob(
        source_dir=source_dir,
        temp_path=temp_path,
        excluded_paths=excluded_paths,
        preserved_paths=preserved_paths,
    )

    if base_source is None:
        image_architecture = architecture
        image_os = DEFAULT_OS
        image_variant = None
        layers: list[BlobDescriptor] = []
        config_json = _build_config_from_scratch(
            architecture=image_architecture,
            os_name=image_os,
            layer_diff_id=local_layer.diff_id,
            created_at=created_at,
            source_dir=source_dir,
        )
    else:
        image_architecture = base_source.architecture
        image_os = base_source.os_name
        image_variant = base_source.variant
        layers = list(base_source.layers)
        config_json = _build_config_from_base(
            base_config=base_source.config,
            layer_diff_id=local_layer.diff_id,
            created_at=created_at,
            source_dir=source_dir,
        )

    layers.append(local_layer.descriptor)

    config_bytes = _json_bytes(config_json)
    config_descriptor = BlobDescriptor(
        digest=_sha256_bytes(config_bytes),
        size=len(config_bytes),
        media_type=_OCI_CONFIG_MEDIA_TYPE,
    )

    manifest_json = {
        "schemaVersion": 2,
        "mediaType": _OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": config_descriptor.media_type,
            "digest": config_descriptor.digest,
            "size": config_descriptor.size,
        },
        "layers": [
            {
                "mediaType": layer.media_type,
                "digest": layer.digest,
                "size": layer.size,
            }
            for layer in layers
        ],
    }
    manifest_bytes = _json_bytes(manifest_json)
    manifest_descriptor = BlobDescriptor(
        digest=_sha256_bytes(manifest_bytes),
        size=len(manifest_bytes),
        media_type=_OCI_MANIFEST_MEDIA_TYPE,
    )

    platform_descriptor: dict[str, object] = {
        "architecture": image_architecture,
        "os": image_os,
    }
    if image_variant is not None:
        platform_descriptor["variant"] = image_variant

    index_json = {
        "schemaVersion": 2,
        "mediaType": _OCI_INDEX_MEDIA_TYPE,
        "manifests": [
            {
                "mediaType": manifest_descriptor.media_type,
                "digest": manifest_descriptor.digest,
                "size": manifest_descriptor.size,
                "platform": platform_descriptor,
                "annotations": {
                    "org.opencontainers.image.ref.name": ref_name,
                },
            }
        ],
    }
    index_bytes = _json_bytes(index_json)

    entries: list[_ArchiveBytesEntry | _ArchiveFileEntry] = [
        _ArchiveBytesEntry(
            archive_path="oci-layout",
            data=_json_bytes({"imageLayoutVersion": "1.0.0"}),
        ),
        _ArchiveBytesEntry(archive_path="index.json", data=index_bytes),
        _ArchiveBytesEntry(
            archive_path=f"blobs/sha256/{config_descriptor.digest_hex}",
            data=config_bytes,
        ),
        _ArchiveBytesEntry(
            archive_path=f"blobs/sha256/{manifest_descriptor.digest_hex}",
            data=manifest_bytes,
        ),
    ]

    if base_source is not None:
        for layer in base_source.layers:
            blob_path = temp_path / layer.digest_hex
            await base_source.fetch_blob_to_path(layer, blob_path)
            entries.append(
                _ArchiveFileEntry(
                    archive_path=f"blobs/sha256/{layer.digest_hex}",
                    file_path=blob_path,
                    size=layer.size,
                )
            )

    entries.append(
        _ArchiveFileEntry(
            archive_path=f"blobs/sha256/{local_layer.descriptor.digest_hex}",
            file_path=local_layer.file_path,
            size=local_layer.descriptor.size,
        )
    )

    return _PreparedOciArchive(
        packed_archive=PackedOciArchive(
            output_path=output_path,
            ref_name=ref_name,
            architecture=image_architecture,
            os_name=image_os,
            manifest_digest=manifest_descriptor.digest,
        ),
        temp_dir=temp_dir,
        entries=entries,
    )


def _write_prepared_oci_archive(
    *, prepared_archive: _PreparedOciArchive, archive_output: BinaryIO
) -> None:
    with tarfile.open(fileobj=archive_output, mode="w|") as archive:
        for entry in prepared_archive.entries:
            if isinstance(entry, _ArchiveBytesEntry):
                _add_bytes_to_archive(
                    archive,
                    path=entry.archive_path,
                    data=entry.data,
                    mode=entry.mode,
                )
                continue

            _add_file_to_archive(
                archive,
                archive_path=entry.archive_path,
                file_path=entry.file_path,
                size=entry.size,
            )


async def _build_oci_archive_with_base(
    *,
    source_dir: Path,
    output_path: Path,
    archive_output: BinaryIO,
    architecture: str,
    ref_name: str,
    base_source: BaseImageSource | None,
    preserved_paths: frozenset[str],
    on_packed_archive_ready: Callable[[PackedOciArchive], Awaitable[None]]
    | None = None,
) -> PackedOciArchive:
    prepared_archive = await _prepare_oci_archive_with_base(
        source_dir=source_dir,
        output_path=output_path,
        architecture=architecture,
        ref_name=ref_name,
        base_source=base_source,
        preserved_paths=preserved_paths,
    )
    try:
        if on_packed_archive_ready is not None:
            await on_packed_archive_ready(prepared_archive.packed_archive)
        await asyncio.to_thread(
            _write_prepared_oci_archive,
            prepared_archive=prepared_archive,
            archive_output=archive_output,
        )
    finally:
        prepared_archive.cleanup()

    return prepared_archive.packed_archive


def _parse_image_reference(reference: str) -> _ImageReference:
    cleaned = reference.strip()
    if cleaned == "":
        raise ImagePackError("--base cannot be empty")

    digest_reference: str | None = None
    if "@" in cleaned:
        cleaned, digest_reference = cleaned.rsplit("@", 1)
        if digest_reference.strip() == "":
            raise ImagePackError(f"invalid image digest reference: {reference}")

    tag_reference: str | None = None
    last_colon = cleaned.rfind(":")
    last_slash = cleaned.rfind("/")
    if digest_reference is None and last_colon > last_slash:
        tag_reference = cleaned[last_colon + 1 :]
        cleaned = cleaned[:last_colon]

    if cleaned == "":
        raise ImagePackError(f"invalid base image reference: {reference}")

    parts = cleaned.split("/")
    if parts[0] == "":
        raise ImagePackError(f"invalid base image reference: {reference}")

    registry = parts[0]
    repository_parts = parts[1:]
    if "." not in registry and ":" not in registry and registry != "localhost":
        registry = "registry-1.docker.io"
        repository_parts = parts

    if len(repository_parts) == 0:
        raise ImagePackError(
            f"missing repository name in base image reference: {reference}"
        )

    if registry == "registry-1.docker.io" and len(repository_parts) == 1:
        repository_parts = ["library", repository_parts[0]]

    repository = "/".join(repository_parts)
    final_reference = digest_reference or tag_reference or "latest"
    use_https = not (
        registry == "localhost"
        or registry.startswith("localhost:")
        or registry.startswith("127.0.0.1")
    )
    return _ImageReference(
        registry=registry,
        repository=repository,
        reference=final_reference,
        use_https=use_https,
    )


def _parse_bearer_challenge(header_value: str) -> dict[str, str]:
    scheme, separator, remainder = header_value.partition(" ")
    if separator == "" or scheme.lower() != "bearer":
        raise ImagePackError("unsupported registry authentication challenge")

    return {
        match[0]: match[1] for match in re.findall(r'([a-zA-Z_]+)="([^"]*)"', remainder)
    }


class _RegistryClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._tokens: dict[tuple[str, str], str] = {}

    async def __aenter__(self) -> "_RegistryClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("registry client session is not active")
        return self._session

    async def resolve_base_image(
        self, *, reference: str, architecture: str
    ) -> BaseImageSource:
        image_reference = _parse_image_reference(reference)

        manifest_bytes, manifest_media_type = await self._fetch_manifest(
            image_reference=image_reference,
            reference=image_reference.reference,
        )
        if manifest_media_type in _INDEX_MEDIA_TYPES:
            descriptor = _select_platform_manifest(
                manifest_bytes=manifest_bytes,
                architecture=architecture,
            )
            manifest_bytes, manifest_media_type = await self._fetch_manifest(
                image_reference=image_reference,
                reference=descriptor.digest,
            )

        if manifest_media_type not in _MANIFEST_MEDIA_TYPES:
            raise ImagePackError(
                f"unsupported base image manifest type: {manifest_media_type}"
            )

        manifest_json = json.loads(manifest_bytes)
        manifest = _require_dict(manifest_json, context="base image manifest")
        config_descriptor = _parse_blob_descriptor(
            manifest.get("config"),
            context="base image config descriptor",
        )

        config_bytes = await self._fetch_blob_bytes(
            image_reference=image_reference,
            descriptor=config_descriptor,
        )
        config_json = json.loads(config_bytes)
        config = _require_dict(config_json, context="base image config")

        base_architecture = _require_str(
            config.get("architecture"), context="base image architecture"
        )
        base_os = _require_str(config.get("os"), context="base image os")
        base_variant = _maybe_str(config.get("variant"), context="base image variant")

        if base_architecture != architecture:
            raise ImagePackError(
                "resolved base image architecture "
                f"{base_architecture!r} does not match requested architecture "
                f"{architecture!r}"
            )

        raw_layers = _require_list(
            manifest.get("layers"), context="base image manifest layers"
        )
        layers = [
            _parse_blob_descriptor(
                layer,
                context=f"base image layer[{index}]",
                media_type_mapper=_normalize_layer_media_type,
            )
            for index, layer in enumerate(raw_layers)
        ]

        return BaseImageSource(
            architecture=base_architecture,
            os_name=base_os,
            variant=base_variant,
            config=config,
            layers=layers,
            fetch_blob_to_path=lambda descriptor, destination: (
                self._download_blob_to_path(
                    image_reference=image_reference,
                    descriptor=descriptor,
                    destination=destination,
                )
            ),
        )

    async def _fetch_manifest(
        self, *, image_reference: _ImageReference, reference: str
    ) -> tuple[bytes, str]:
        manifest_path = f"manifests/{reference}"
        content_type, body = await self._get_response_bytes(
            image_reference=image_reference,
            api_path=manifest_path,
            accept=_MANIFEST_ACCEPT_HEADER,
        )
        media_type = content_type.split(";", 1)[0].strip()
        return body, media_type

    async def _fetch_blob_bytes(
        self, *, image_reference: _ImageReference, descriptor: BlobDescriptor
    ) -> bytes:
        _, body = await self._get_response_bytes(
            image_reference=image_reference,
            api_path=f"blobs/{descriptor.digest}",
        )
        if len(body) != descriptor.size:
            raise ImagePackError(
                f"base image blob size mismatch for {descriptor.digest}: "
                f"expected {descriptor.size}, got {len(body)}"
            )
        if _sha256_bytes(body) != descriptor.digest:
            raise ImagePackError(
                f"base image blob digest mismatch for {descriptor.digest}"
            )
        return body

    async def _download_blob_to_path(
        self,
        *,
        image_reference: _ImageReference,
        descriptor: BlobDescriptor,
        destination: Path,
    ) -> None:
        token = await self._ensure_bearer_token(
            image_reference=image_reference,
            repository_scope=image_reference.repository,
        )

        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{image_reference.api_base}/blobs/{descriptor.digest}"
        async with self.session.get(url, headers=headers) as response:
            if response.status == 401:
                token = await self._refresh_bearer_token(
                    image_reference=image_reference,
                    response=response,
                )
                headers["Authorization"] = f"Bearer {token}"
            else:
                await self._write_response_to_path(
                    response=response,
                    descriptor=descriptor,
                    destination=destination,
                )
                return

        async with self.session.get(url, headers=headers) as response:
            await self._write_response_to_path(
                response=response,
                descriptor=descriptor,
                destination=destination,
            )

    async def _write_response_to_path(
        self,
        *,
        response: aiohttp.ClientResponse,
        descriptor: BlobDescriptor,
        destination: Path,
    ) -> None:
        if response.status != 200:
            body = await response.text()
            raise ImagePackError(
                f"failed to download base image blob {descriptor.digest}: "
                f"{response.status} {body}"
            )

        digest = hashlib.sha256()
        total = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as file:
            async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                if chunk == b"":
                    continue
                digest.update(chunk)
                total += len(chunk)
                file.write(chunk)

        resolved_digest = f"sha256:{digest.hexdigest()}"
        if total != descriptor.size:
            raise ImagePackError(
                f"base image blob size mismatch for {descriptor.digest}: "
                f"expected {descriptor.size}, got {total}"
            )
        if resolved_digest != descriptor.digest:
            raise ImagePackError(
                f"base image blob digest mismatch for {descriptor.digest}"
            )

    async def _get_response_bytes(
        self,
        *,
        image_reference: _ImageReference,
        api_path: str,
        accept: str | None = None,
    ) -> tuple[str, bytes]:
        token = await self._ensure_bearer_token(
            image_reference=image_reference,
            repository_scope=image_reference.repository,
        )

        headers: dict[str, str] = {}
        if accept is not None:
            headers["Accept"] = accept
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{image_reference.api_base}/{api_path}"
        async with self.session.get(url, headers=headers) as response:
            if response.status == 401:
                token = await self._refresh_bearer_token(
                    image_reference=image_reference,
                    response=response,
                )
                headers["Authorization"] = f"Bearer {token}"
            else:
                return await self._response_bytes_or_error(response)

        async with self.session.get(url, headers=headers) as response:
            return await self._response_bytes_or_error(response)

    async def _response_bytes_or_error(
        self, response: aiohttp.ClientResponse
    ) -> tuple[str, bytes]:
        if response.status != 200:
            body = await response.text()
            raise ImagePackError(
                f"registry request failed with status {response.status}: {body}"
            )
        content_type = response.headers.get("Content-Type", "")
        body = await response.read()
        return content_type, body

    async def _ensure_bearer_token(
        self, *, image_reference: _ImageReference, repository_scope: str
    ) -> str | None:
        return self._tokens.get((image_reference.registry, repository_scope))

    async def _refresh_bearer_token(
        self,
        *,
        image_reference: _ImageReference,
        response: aiohttp.ClientResponse,
    ) -> str:
        header_value = response.headers.get("WWW-Authenticate")
        if header_value is None:
            raise ImagePackError("registry request requires authentication")

        challenge = _parse_bearer_challenge(header_value)
        realm = _require_str(challenge.get("realm"), context="registry auth realm")
        service = _maybe_str(challenge.get("service"), context="registry auth service")
        scope = (
            _maybe_str(challenge.get("scope"), context="registry auth scope")
            or f"repository:{image_reference.repository}:pull"
        )

        params: dict[str, str] = {"scope": scope}
        if service is not None:
            params["service"] = service

        async with self.session.get(realm, params=params) as token_response:
            if token_response.status != 200:
                body = await token_response.text()
                raise ImagePackError(
                    f"failed to authenticate to registry: "
                    f"{token_response.status} {body}"
                )

            token_body = await token_response.json()

        token_value = token_body.get("token")
        if not isinstance(token_value, str) or token_value.strip() == "":
            token_value = token_body.get("access_token")

        token = _require_str(token_value, context="registry bearer token")
        scope_key = image_reference.repository
        self._tokens[(image_reference.registry, scope_key)] = token
        return token


def _select_platform_manifest(
    *, manifest_bytes: bytes, architecture: str
) -> BlobDescriptor:
    index_json = json.loads(manifest_bytes)
    index = _require_dict(index_json, context="base image index")
    manifests = _require_list(
        index.get("manifests"), context="base image index manifests"
    )

    for index_position, manifest in enumerate(manifests):
        manifest_descriptor = _require_dict(
            manifest, context=f"base image index manifest[{index_position}]"
        )
        platform = _require_dict(
            manifest_descriptor.get("platform"),
            context=f"base image index manifest[{index_position}] platform",
        )
        os_name = _require_str(
            platform.get("os"),
            context=f"base image index manifest[{index_position}] os",
        )
        manifest_architecture = _require_str(
            platform.get("architecture"),
            context=f"base image index manifest[{index_position}] architecture",
        )
        if os_name != DEFAULT_OS or manifest_architecture != architecture:
            continue

        return _parse_blob_descriptor(
            manifest_descriptor,
            context=f"base image index manifest[{index_position}]",
        )

    raise ImagePackError(
        f"no linux/{architecture} manifest found in the base image index"
    )


def _resolve_build_oci_archive_inputs(
    *,
    source_dir: Path,
    output_path: Path,
    ref_name: str | None,
) -> tuple[Path, Path, str]:
    resolved_source_dir = source_dir.expanduser().resolve()
    if not resolved_source_dir.exists():
        raise ImagePackError(f"path to pack does not exist: {source_dir}")
    if not resolved_source_dir.is_dir():
        raise ImagePackError(f"path to pack must be a directory: {source_dir}")

    resolved_output_path = output_path.expanduser().resolve()
    if resolved_output_path.exists() and resolved_output_path.is_dir():
        raise ImagePackError(
            f"output path must be a file, got directory: {output_path}"
        )

    resolved_ref_name = (
        ref_name if ref_name is not None else _default_ref_name(resolved_source_dir)
    )
    return resolved_source_dir, resolved_output_path, resolved_ref_name


async def _build_oci_archive_resolved(
    *,
    source_dir: Path,
    output_path: Path,
    archive_output: BinaryIO,
    base_image: str | None,
    architecture: str,
    ref_name: str,
    base_source: BaseImageSource | None,
    preserved_paths: frozenset[str],
    on_packed_archive_ready: Callable[[PackedOciArchive], Awaitable[None]]
    | None = None,
) -> PackedOciArchive:
    if base_image is None:
        return await _build_oci_archive_with_base(
            source_dir=source_dir,
            output_path=output_path,
            archive_output=archive_output,
            architecture=architecture,
            ref_name=ref_name,
            base_source=base_source,
            preserved_paths=preserved_paths,
            on_packed_archive_ready=on_packed_archive_ready,
        )

    if base_source is not None:
        return await _build_oci_archive_with_base(
            source_dir=source_dir,
            output_path=output_path,
            archive_output=archive_output,
            architecture=architecture,
            ref_name=ref_name,
            base_source=base_source,
            preserved_paths=preserved_paths,
            on_packed_archive_ready=on_packed_archive_ready,
        )

    async with _RegistryClient() as registry_client:
        resolved_base_source = await registry_client.resolve_base_image(
            reference=base_image,
            architecture=architecture,
        )
        return await _build_oci_archive_with_base(
            source_dir=source_dir,
            output_path=output_path,
            archive_output=archive_output,
            architecture=architecture,
            ref_name=ref_name,
            base_source=resolved_base_source,
            preserved_paths=preserved_paths,
            on_packed_archive_ready=on_packed_archive_ready,
        )


async def build_oci_archive(
    *,
    source_dir: Path,
    output_path: Path,
    base_image: str | None = None,
    architecture: str = DEFAULT_ARCHITECTURE,
    ref_name: str | None = None,
    base_source: BaseImageSource | None = None,
    preserved_paths: frozenset[str] | None = None,
) -> PackedOciArchive:
    resolved_source_dir, resolved_output_path, resolved_ref_name = (
        _resolve_build_oci_archive_inputs(
            source_dir=source_dir,
            output_path=output_path,
            ref_name=ref_name,
        )
    )
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_output_path.open("wb") as archive_output:
        return await _build_oci_archive_resolved(
            source_dir=resolved_source_dir,
            output_path=resolved_output_path,
            archive_output=archive_output,
            base_image=base_image,
            architecture=architecture,
            ref_name=resolved_ref_name,
            base_source=base_source,
            preserved_paths=preserved_paths or frozenset(),
        )


async def build_oci_archive_to_writer(
    *,
    source_dir: Path,
    output_path: Path,
    archive_output: BinaryIO,
    base_image: str | None = None,
    architecture: str = DEFAULT_ARCHITECTURE,
    ref_name: str | None = None,
    base_source: BaseImageSource | None = None,
    preserved_paths: frozenset[str] | None = None,
    on_packed_archive_ready: Callable[[PackedOciArchive], Awaitable[None]]
    | None = None,
) -> PackedOciArchive:
    resolved_source_dir, resolved_output_path, resolved_ref_name = (
        _resolve_build_oci_archive_inputs(
            source_dir=source_dir,
            output_path=output_path,
            ref_name=ref_name,
        )
    )
    return await _build_oci_archive_resolved(
        source_dir=resolved_source_dir,
        output_path=resolved_output_path,
        archive_output=archive_output,
        base_image=base_image,
        architecture=architecture,
        ref_name=resolved_ref_name,
        base_source=base_source,
        preserved_paths=preserved_paths or frozenset(),
        on_packed_archive_ready=on_packed_archive_ready,
    )
