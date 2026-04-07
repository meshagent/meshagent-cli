import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard

from meshagent.cli.oci_archive import (
    BaseImageSource,
    BlobDescriptor,
    _normalize_layer_media_type,
    build_oci_archive,
    build_oci_archive_to_writer,
)


def _read_member_bytes(archive: tarfile.TarFile, path: str) -> bytes:
    member = archive.getmember(path)
    file = archive.extractfile(member)
    assert file is not None
    return file.read()


def _sha256_digest(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _zstd_decompress(data: bytes) -> bytes:
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
        return reader.read()


@pytest.mark.asyncio
async def test_build_oci_archive_writes_oci_layout_and_respects_dockerignore(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    (source_dir / "keep.txt").write_text("keep", encoding="utf-8")
    (source_dir / "ignore.txt").write_text("ignore", encoding="utf-8")
    (source_dir / ".dockerignore").write_text("ignore.txt\n", encoding="utf-8")
    nested_dir = source_dir / "nested"
    nested_dir.mkdir()
    (nested_dir / "child.txt").write_text("child", encoding="utf-8")

    output_path = tmp_path / "dist" / "sample.oci.tar"

    packed_archive = await build_oci_archive(
        source_dir=source_dir,
        output_path=output_path,
    )

    assert packed_archive.output_path == output_path.resolve()
    assert packed_archive.output_path.exists()
    assert packed_archive.ref_name == "sample:latest"

    with tarfile.open(packed_archive.output_path, mode="r") as archive:
        assert "oci-layout" in archive.getnames()
        assert "index.json" in archive.getnames()

        index_json = json.loads(_read_member_bytes(archive, "index.json"))
        manifest_descriptor = index_json["manifests"][0]
        assert manifest_descriptor["annotations"] == {
            "org.opencontainers.image.ref.name": "sample:latest"
        }
        assert manifest_descriptor["platform"] == {
            "architecture": "amd64",
            "os": "linux",
        }

        manifest_path = f"blobs/sha256/{manifest_descriptor['digest'].split(':', 1)[1]}"
        manifest_json = json.loads(_read_member_bytes(archive, manifest_path))
        config_descriptor = manifest_json["config"]
        config_path = f"blobs/sha256/{config_descriptor['digest'].split(':', 1)[1]}"
        config_json = json.loads(_read_member_bytes(archive, config_path))
        layer_descriptor = manifest_json["layers"][0]
        layer_path = f"blobs/sha256/{layer_descriptor['digest'].split(':', 1)[1]}"
        layer_blob_bytes = _read_member_bytes(archive, layer_path)

    assert (
        layer_descriptor["mediaType"] == "application/vnd.oci.image.layer.v1.tar+zstd"
    )
    layer_tar_bytes = _zstd_decompress(layer_blob_bytes)
    assert config_json["rootfs"]["diff_ids"] == [_sha256_digest(layer_tar_bytes)]

    with tarfile.open(fileobj=io.BytesIO(layer_tar_bytes), mode="r:") as layer_archive:
        layer_names = set(layer_archive.getnames())

    assert "keep.txt" in layer_names
    assert "nested" in layer_names
    assert "nested/child.txt" in layer_names
    assert "ignore.txt" not in layer_names
    assert ".dockerignore" not in layer_names


@pytest.mark.asyncio
async def test_build_oci_archive_preserves_selected_build_files_when_requested(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (source_dir / ".dockerignore").write_text("Dockerfile\n", encoding="utf-8")
    (source_dir / "app.txt").write_text("app", encoding="utf-8")

    output_path = tmp_path / "dist" / "sample.oci.tar"

    packed_archive = await build_oci_archive(
        source_dir=source_dir,
        output_path=output_path,
        preserved_paths=frozenset({".dockerignore", "Dockerfile"}),
    )

    with tarfile.open(packed_archive.output_path, mode="r") as archive:
        index_json = json.loads(_read_member_bytes(archive, "index.json"))
        manifest_descriptor = index_json["manifests"][0]
        manifest_path = f"blobs/sha256/{manifest_descriptor['digest'].split(':', 1)[1]}"
        manifest_json = json.loads(_read_member_bytes(archive, manifest_path))
        layer_descriptor = manifest_json["layers"][0]
        layer_path = f"blobs/sha256/{layer_descriptor['digest'].split(':', 1)[1]}"
        layer_blob_bytes = _read_member_bytes(archive, layer_path)

    layer_tar_bytes = _zstd_decompress(layer_blob_bytes)
    with tarfile.open(fileobj=io.BytesIO(layer_tar_bytes), mode="r:") as layer_archive:
        layer_names = set(layer_archive.getnames())

    assert ".dockerignore" in layer_names
    assert "Dockerfile" in layer_names
    assert "app.txt" in layer_names


@pytest.mark.asyncio
async def test_build_oci_archive_appends_local_layer_to_base_image(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    (source_dir / "extra.txt").write_text("extra", encoding="utf-8")

    base_layer_bytes = b"base-layer"
    base_layer_descriptor = BlobDescriptor(
        digest=_sha256_digest(base_layer_bytes),
        size=len(base_layer_bytes),
        media_type="application/vnd.oci.image.layer.v1.tar",
    )

    async def _fetch_blob_to_path(
        descriptor: BlobDescriptor, destination: Path
    ) -> None:
        assert descriptor == base_layer_descriptor
        destination.write_bytes(base_layer_bytes)

    base_source = BaseImageSource(
        architecture="amd64",
        os_name="linux",
        variant=None,
        config={
            "architecture": "amd64",
            "os": "linux",
            "config": {
                "Entrypoint": ["/bin/sh"],
                "Env": ["BASE=1"],
            },
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + ("1" * 64)],
            },
            "history": [
                {
                    "created": "2026-01-01T00:00:00Z",
                    "created_by": "base image",
                }
            ],
        },
        layers=[base_layer_descriptor],
        fetch_blob_to_path=_fetch_blob_to_path,
    )

    output_path = tmp_path / "dist" / "sample.oci.tar"

    packed_archive = await build_oci_archive(
        source_dir=source_dir,
        output_path=output_path,
        base_image="meshagent/python:default",
        architecture="amd64",
        base_source=base_source,
    )

    with tarfile.open(packed_archive.output_path, mode="r") as archive:
        index_json = json.loads(_read_member_bytes(archive, "index.json"))
        manifest_descriptor = index_json["manifests"][0]
        manifest_path = f"blobs/sha256/{manifest_descriptor['digest'].split(':', 1)[1]}"
        manifest_json = json.loads(_read_member_bytes(archive, manifest_path))

        config_descriptor = manifest_json["config"]
        config_path = f"blobs/sha256/{config_descriptor['digest'].split(':', 1)[1]}"
        config_json = json.loads(_read_member_bytes(archive, config_path))

        base_blob_path = f"blobs/sha256/{base_layer_descriptor.digest.split(':', 1)[1]}"
        assert _read_member_bytes(archive, base_blob_path) == base_layer_bytes
        local_layer_descriptor = manifest_json["layers"][1]
        local_blob_path = (
            f"blobs/sha256/{local_layer_descriptor['digest'].split(':', 1)[1]}"
        )
        local_blob_bytes = _read_member_bytes(archive, local_blob_path)

    assert [layer["digest"] for layer in manifest_json["layers"]] == [
        base_layer_descriptor.digest,
        local_layer_descriptor["digest"],
    ]
    assert (
        local_layer_descriptor["mediaType"]
        == "application/vnd.oci.image.layer.v1.tar+zstd"
    )
    assert config_json["config"] == {
        "Entrypoint": ["/bin/sh"],
        "Env": ["BASE=1"],
    }
    assert config_json["rootfs"]["diff_ids"] == [
        "sha256:" + ("1" * 64),
        _sha256_digest(_zstd_decompress(local_blob_bytes)),
    ]
    assert len(config_json["history"]) == 2


def test_normalize_layer_media_type_accepts_oci_zstd() -> None:
    assert (
        _normalize_layer_media_type("application/vnd.oci.image.layer.v1.tar+zstd")
        == "application/vnd.oci.image.layer.v1.tar+zstd"
    )


@pytest.mark.asyncio
async def test_build_oci_archive_resolves_tilde_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello", encoding="utf-8")

    packed_archive = await build_oci_archive(
        source_dir=source_dir,
        output_path=Path("~/archives/sample.oci.tar"),
    )

    assert (
        packed_archive.output_path
        == (home_dir / "archives" / "sample.oci.tar").resolve()
    )
    assert packed_archive.output_path.exists()


@pytest.mark.asyncio
async def test_build_oci_archive_to_writer_writes_oci_layout(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello", encoding="utf-8")

    output_path = tmp_path / "dist" / "sample.oci.tar"
    archive_buffer = io.BytesIO()

    packed_archive = await build_oci_archive_to_writer(
        source_dir=source_dir,
        output_path=output_path,
        archive_output=archive_buffer,
    )

    assert packed_archive.output_path == output_path.resolve()
    assert not output_path.exists()

    archive_buffer.seek(0)
    with tarfile.open(fileobj=archive_buffer, mode="r:") as archive:
        assert "oci-layout" in archive.getnames()
        assert "index.json" in archive.getnames()
