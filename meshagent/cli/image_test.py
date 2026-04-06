import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from meshagent.api import ApiScope
from meshagent.api.room_ports import ROOM_INTERNAL_API_PORT
from meshagent.cli import image
from meshagent.api.specs.service import (
    ContainerMountSpec,
    ContainerSpec,
    EnvironmentVariable,
    ImageStorageMountSpec,
    PortSpec,
    ServiceMetadata,
    ServiceSpec,
    TokenValue,
)


def test_resolve_room_archive_path_uses_output_name_for_directory_targets() -> None:
    output_path = Path("/tmp/build/image.oci.tar")

    assert (
        image._resolve_room_archive_path(
            output_path=output_path,
            room_path="/archives/",
        )
        == "/archives/image.oci.tar"
    )


@pytest.mark.asyncio
async def test_pack_image_uploads_archive_to_room_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    output_path = tmp_path / "dist" / "sample.oci.tar"
    captured: dict[str, object] = {}

    async def _fake_build_oci_archive_to_writer(
        *,
        source_dir: Path,
        output_path: Path,
        archive_output,
        base_image: str | None,
        architecture: str,
        ref_name: str | None = None,
        on_packed_archive_ready=None,
    ) -> SimpleNamespace:
        resolved_output_path = output_path.expanduser().resolve()
        captured["source_dir"] = source_dir
        captured["output_path"] = resolved_output_path
        captured["base_image"] = base_image
        captured["architecture"] = architecture
        captured["ref_name"] = ref_name
        captured["on_packed_archive_ready"] = on_packed_archive_ready is not None
        packed_archive = SimpleNamespace(
            output_path=resolved_output_path,
            ref_name="sample:latest",
            manifest_digest="sha256:sampledigest",
        )
        if on_packed_archive_ready is not None:
            await on_packed_archive_ready(packed_archive)
        archive_output.write(b"oci-")
        archive_output.write(b"archive")
        return packed_archive

    class _FakeStorage:
        async def upload_stream(
            self,
            *,
            path: str,
            chunks,
            overwrite: bool,
            size: int | None,
            name: str,
        ) -> None:
            uploaded = bytearray()
            async for chunk in chunks:
                uploaded.extend(chunk)

            captured["remote_path"] = path
            captured["overwrite"] = overwrite
            captured["size"] = size
            captured["upload_name"] = name
            captured["uploaded_bytes"] = bytes(uploaded)

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _unexpected_build_oci_archive(**kwargs) -> SimpleNamespace:
        del kwargs
        raise AssertionError("room uploads should stream from the archive builder")

    monkeypatch.setattr(
        image,
        "build_oci_archive_to_writer",
        _fake_build_oci_archive_to_writer,
    )
    monkeypatch.setattr(image, "build_oci_archive", _unexpected_build_oci_archive)
    monkeypatch.setattr(
        image,
        "_with_client",
        _fake_with_client,
    )
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.pack_image(
        project_id="project-1",
        room="room-1",
        path=str(source_dir),
        tag="room.meshagent.com/sample/app:1",
        output=str(output_path),
        base="python:3.13",
        arch="arm64",
        room_path=None,
    )

    assert captured["source_dir"] == source_dir
    assert captured["output_path"] == output_path.resolve()
    assert captured["base_image"] == "python:3.13"
    assert captured["architecture"] == "arm64"
    assert captured["ref_name"] == "room.meshagent.com/sample/app:1"
    assert captured["on_packed_archive_ready"] is True
    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["remote_path"] == "/sample/app"
    assert captured["overwrite"] is True
    assert captured["size"] is None
    assert captured["upload_name"] == "sample.oci.tar"
    assert captured["uploaded_bytes"] == b"oci-archive"
    assert output_path.read_bytes() == b"oci-archive"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_pack_image_uploads_archive_to_room_without_local_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    captured: dict[str, object] = {}

    async def _fake_build_oci_archive_to_writer(
        *,
        source_dir: Path,
        output_path: Path,
        archive_output,
        base_image: str | None,
        architecture: str,
        ref_name: str | None = None,
        on_packed_archive_ready=None,
    ) -> SimpleNamespace:
        captured["source_dir"] = source_dir
        captured["output_path"] = output_path
        captured["base_image"] = base_image
        captured["architecture"] = architecture
        captured["ref_name"] = ref_name
        packed_archive = SimpleNamespace(
            output_path=output_path,
            ref_name="sample:latest",
            manifest_digest="sha256:sampledigest",
        )
        if on_packed_archive_ready is not None:
            await on_packed_archive_ready(packed_archive)
        archive_output.write(b"oci-archive")
        return packed_archive

    class _FakeStorage:
        async def upload_stream(
            self,
            *,
            path: str,
            chunks,
            overwrite: bool,
            size: int | None,
            name: str,
        ) -> None:
            uploaded = bytearray()
            async for chunk in chunks:
                uploaded.extend(chunk)

            captured["remote_path"] = path
            captured["overwrite"] = overwrite
            captured["size"] = size
            captured["upload_name"] = name
            captured["uploaded_bytes"] = bytes(uploaded)

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(
        image,
        "build_oci_archive_to_writer",
        _fake_build_oci_archive_to_writer,
    )
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.pack_image(
        project_id="project-1",
        room="room-1",
        path=str(source_dir),
        tag="room.meshagent.com/sample/app:1",
        output=None,
        base=None,
        arch="amd64",
        room_path=None,
    )

    assert captured["source_dir"] == source_dir
    assert captured["base_image"] is None
    assert captured["architecture"] == "amd64"
    assert captured["ref_name"] == "room.meshagent.com/sample/app:1"
    assert captured["remote_path"] == "/sample/app"
    assert captured["overwrite"] is True
    assert captured["size"] is None
    assert captured["upload_name"] == "app"
    assert captured["uploaded_bytes"] == b"oci-archive"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_pack_image_room_close_timeout_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()
    captured: dict[str, object] = {"prints": []}

    async def _fake_build_oci_archive_to_writer(
        *,
        source_dir: Path,
        output_path: Path,
        archive_output,
        base_image: str | None,
        architecture: str,
        ref_name: str | None = None,
        on_packed_archive_ready=None,
    ) -> SimpleNamespace:
        del source_dir, output_path, base_image, architecture, ref_name
        packed_archive = SimpleNamespace(
            output_path=Path("/tmp/ignored.tar"),
            ref_name="room.meshagent.com/sample/app:1",
            manifest_digest="sha256:sampledigest",
        )
        if on_packed_archive_ready is not None:
            await on_packed_archive_ready(packed_archive)
        archive_output.write(b"oci-archive")
        return packed_archive

    class _FakeStorage:
        async def upload_stream(
            self,
            *,
            path: str,
            chunks,
            overwrite: bool,
            size: int | None,
            name: str,
        ) -> None:
            del path, overwrite, size, name
            async for _chunk in chunks:
                pass

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            await asyncio.sleep(1)

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(
        image,
        "build_oci_archive_to_writer",
        _fake_build_oci_archive_to_writer,
    )
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "_CLIENT_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        image,
        "print",
        lambda *args, **kwargs: captured["prints"].append(
            " ".join(str(arg) for arg in args)
        ),
    )

    await image.pack_image(
        project_id="project-1",
        room="room-1",
        path=str(source_dir),
        tag="room.meshagent.com/sample/app:1",
        output=None,
        base=None,
        arch="amd64",
        room_path=None,
    )

    assert any("Uploaded OCI archive" in line for line in captured["prints"])
    assert any(
        "Timed out closing room client after upload" in line
        for line in captured["prints"]
    )
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_oci_archive_to_streaming_output_does_not_deadlock_when_queue_is_full(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive_output = image._StreamingArchiveOutput()
    packed_archive = SimpleNamespace(
        output_path=tmp_path / "ignored.tar",
        ref_name="room.meshagent.com/sample/app:1",
        manifest_digest="sha256:sampledigest",
    )

    for _ in range(image._ARCHIVE_STREAM_QUEUE_SIZE):
        archive_output._queue.put_nowait(b"queued")

    async def _fake_build_oci_archive_to_writer(**kwargs) -> SimpleNamespace:
        del kwargs
        return packed_archive

    async def _free_queue_space() -> None:
        await asyncio.sleep(0.01)
        assert archive_output._queue.get_nowait() == b"queued"

    monkeypatch.setattr(
        image,
        "build_oci_archive_to_writer",
        _fake_build_oci_archive_to_writer,
    )

    await asyncio.wait_for(
        asyncio.gather(
            image._build_oci_archive_to_streaming_output(
                source_dir=tmp_path,
                output_path=tmp_path / "ignored.tar",
                archive_output=archive_output,
                base_image=None,
                architecture="amd64",
                ref_name="room.meshagent.com/sample/app:1",
            ),
            _free_queue_space(),
        ),
        timeout=1,
    )


@pytest.mark.asyncio
async def test_build_image_starts_room_build_and_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_spec = object()
    credentials = [object()]
    captured: dict[str, object] = {}
    parse_mount_args: dict[str, object] = {}

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["build_id"] = build_id
        return 0

    def _fake_parse_image_operation_mounts(**kwargs):
        parse_mount_args.update(kwargs)
        return mount_spec

    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        _fake_parse_image_operation_mounts,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: credentials)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="repo/name:tag",
        context_path="/workspace",
        dockerfile_path="/workspace/Dockerfile",
        mount_room_path=["/src:/workspace"],
        mount_project_path=[],
        mount_image=[],
        private=True,
        optimize=True,
        cred=["registry,user,password"],
    )

    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert parse_mount_args == {
        "mount_room_path": ["/src:/workspace"],
        "mount_project_path": [],
        "mount_image": [],
    }
    assert captured["build_kwargs"] == {
        "tag": "repo/name:tag",
        "mounts": [mount_spec],
        "context_path": "/workspace",
        "dockerfile_path": "/workspace/Dockerfile",
        "optimize_image": True,
        "private": True,
        "credentials": credentials,
        "context_archive_path": None,
        "context_archive_ref": None,
        "context_archive_mount_path": None,
        "context_archive_arch": None,
    }
    assert captured["build_id"] == "build-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_pack_uploads_archive_and_defaults_context_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    parse_mount_args: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    temporary_pack_path = "/temp/build/packs/pack-123"

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

        async def load(self, *, archive_path: str):
            captured["loaded_archive_path"] = archive_path
            return SimpleNamespace(
                resolved_ref="room.meshagent.com/temp/build/packs/pack-123:latest"
            )

        async def pull_image(self, *, tag: str, credentials=None) -> None:
            del credentials
            captured["pulled_image"] = tag

        async def delete_image(self, *, image: str) -> None:
            captured["deleted_image"] = image

    class _FakeStorage:
        async def delete(self, path: str) -> None:
            captured["deleted_path"] = path

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.storage = _FakeStorage()
            self.services = SimpleNamespace(restart=self._restart)

        async def _restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["build_id"] = build_id
        return 0

    async def _fake_upload_oci_archive_to_room(**kwargs) -> SimpleNamespace:
        captured["upload_kwargs"] = kwargs
        return SimpleNamespace(
            packed_archive=SimpleNamespace(
                output_path=Path("/tmp/ignored.tar"),
                ref_name="room.meshagent.com/temp/build/packs/pack-123:latest",
            ),
            remote_path=temporary_pack_path,
        )

    def _fake_parse_image_operation_mounts(**kwargs):
        parse_mount_args.update(kwargs)
        raise AssertionError("_parse_image_operation_mounts should not be called")

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_upload_oci_archive_to_room",
        _fake_upload_oci_archive_to_room,
    )
    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        _fake_parse_image_operation_mounts,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )
    monkeypatch.setattr(
        image.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="pack-123"),
    )
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.build_image(
        project_id="project-1",
        room="room-1",
        tag="room.meshagent.com/website:1",
        context_path=None,
        dockerfile_path=None,
        pack=str(source_dir),
        arch="arm64",
        pack_room_path=None,
        mount_room_path=[],
        mount_project_path=[],
        mount_image=[],
        private=False,
        optimize=True,
        cred=[],
    )

    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["upload_kwargs"] == {
        "client": captured["wait_client"],
        "source_dir": source_dir,
        "remote_path": temporary_pack_path,
        "output_path": None,
        "base_image": None,
        "architecture": "arm64",
        "ref_name": "room.meshagent.com/temp/build/packs/pack-123:latest",
        "preserved_paths": frozenset(),
    }
    assert parse_mount_args == {}
    assert captured["loaded_archive_path"] == temporary_pack_path
    assert (
        captured["deleted_image"]
        == "room.meshagent.com/temp/build/packs/pack-123:latest"
    )
    assert captured["build_kwargs"] == {
        "tag": "room.meshagent.com/website:1",
        "mounts": [],
        "context_path": "/context",
        "dockerfile_path": "/context/Dockerfile",
        "optimize_image": True,
        "private": False,
        "credentials": [],
        "context_archive_path": temporary_pack_path,
        "context_archive_ref": "room.meshagent.com/temp/build/packs/pack-123:latest",
        "context_archive_mount_path": "/context",
        "context_archive_arch": "arm64",
    }
    assert captured["build_id"] == "build-1"
    assert captured["deleted_path"] == temporary_pack_path
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_pack_defaults_architecture_to_amd64(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

        async def load(self, *, archive_path: str):
            captured["loaded_archive_path"] = archive_path
            return SimpleNamespace(
                resolved_ref="room.meshagent.com/temp/build/packs/pack-123:latest"
            )

        async def delete_image(self, *, image: str) -> None:
            captured["deleted_image"] = image

    class _FakeStorage:
        async def delete(self, path: str) -> None:
            captured["deleted_path"] = path

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 0

    async def _fake_upload_oci_archive_to_room(**kwargs) -> SimpleNamespace:
        captured["upload_kwargs"] = kwargs
        return SimpleNamespace(
            packed_archive=SimpleNamespace(
                output_path=Path("/tmp/ignored.tar"),
                ref_name="room.meshagent.com/temp/build/packs/pack-123:latest",
            ),
            remote_path="/temp/build/packs/pack-123",
        )

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_upload_oci_archive_to_room",
        _fake_upload_oci_archive_to_room,
    )
    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )
    monkeypatch.setattr(
        image.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="pack-123"),
    )
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.build_image(
        project_id=None,
        room="room-1",
        tag="room.meshagent.com/website:1",
        context_path=None,
        dockerfile_path=None,
        pack=str(source_dir),
        pack_room_path=None,
        mount_room_path=[],
        mount_project_path=[],
        mount_image=[],
        private=False,
        cred=[],
    )

    assert captured["upload_kwargs"]["architecture"] == "amd64"
    assert captured["loaded_archive_path"] == "/temp/build/packs/pack-123"
    assert (
        captured["deleted_image"]
        == "room.meshagent.com/temp/build/packs/pack-123:latest"
    )


@pytest.mark.asyncio
async def test_build_image_pack_preserves_ignored_dockerfile_and_dockerignore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (source_dir / ".dockerignore").write_text("Dockerfile\n", encoding="utf-8")

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

        async def load(self, *, archive_path: str):
            del archive_path
            return SimpleNamespace(
                resolved_ref="room.meshagent.com/temp/build/packs/pack-123:latest"
            )

        async def delete_image(self, *, image: str) -> None:
            del image

    class _FakeStorage:
        async def delete(self, path: str) -> None:
            del path

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 0

    async def _fake_upload_oci_archive_to_room(**kwargs) -> SimpleNamespace:
        captured["upload_kwargs"] = kwargs
        return SimpleNamespace(
            packed_archive=SimpleNamespace(
                output_path=Path("/tmp/ignored.tar"),
                ref_name="room.meshagent.com/temp/build/packs/pack-123:latest",
            ),
            remote_path="/temp/build/packs/pack-123",
        )

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_upload_oci_archive_to_room",
        _fake_upload_oci_archive_to_room,
    )
    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )
    monkeypatch.setattr(
        image.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="pack-123"),
    )
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.build_image(
        project_id=None,
        room="room-1",
        tag="room.meshagent.com/website:1",
        context_path=None,
        dockerfile_path=None,
        pack=str(source_dir),
        pack_room_path=None,
        mount_room_path=[],
        mount_project_path=[],
        mount_image=[],
        private=False,
        optimize=True,
        cred=[],
    )

    assert captured["upload_kwargs"]["preserved_paths"] == frozenset(
        {".dockerignore", "Dockerfile"}
    )


@pytest.mark.asyncio
async def test_build_image_pack_deletes_temporary_loaded_image_after_failed_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

        async def load(self, *, archive_path: str):
            captured["loaded_archive_path"] = archive_path
            return SimpleNamespace(
                resolved_ref="room.meshagent.com/temp/build/packs/pack-123:latest"
            )

        async def delete_image(self, *, image: str) -> None:
            captured["deleted_image"] = image

    class _FakeStorage:
        async def delete(self, path: str) -> None:
            captured["deleted_path"] = path

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()
            self.storage = _FakeStorage()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 17

    async def _fake_upload_oci_archive_to_room(**kwargs) -> SimpleNamespace:
        captured["upload_kwargs"] = kwargs
        return SimpleNamespace(
            packed_archive=SimpleNamespace(
                output_path=Path("/tmp/ignored.tar"),
                ref_name="room.meshagent.com/temp/build/packs/pack-123:latest",
            ),
            remote_path="/temp/build/packs/pack-123",
        )

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_upload_oci_archive_to_room",
        _fake_upload_oci_archive_to_room,
    )
    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )
    monkeypatch.setattr(
        image.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="pack-123"),
    )
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    with pytest.raises(typer.Exit) as exc_info:
        await image.build_image(
            project_id=None,
            room="room-1",
            tag="room.meshagent.com/website:1",
            context_path=None,
            dockerfile_path=None,
            pack=str(source_dir),
            pack_room_path=None,
            mount_room_path=[],
            mount_project_path=[],
            mount_image=[],
            private=False,
            optimize=True,
            cred=[],
        )

    assert exc_info.value.exit_code == 17
    assert (
        captured["deleted_image"]
        == "room.meshagent.com/temp/build/packs/pack-123:latest"
    )
    assert captured["deleted_path"] == "/temp/build/packs/pack-123"


def test_resolve_build_pack_room_path_defaults_to_repository_path() -> None:
    parsed_tag = image._parse_build_tag("room.meshagent.com/nested/website:1")

    assert (
        image._resolve_build_pack_room_path(parsed_tag=parsed_tag, room_path=None)
        == "/nested/website"
    )


def test_resolve_uploaded_build_pack_room_path_defaults_to_temp_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_tag = image._parse_build_tag("room.meshagent.com/website:1")
    monkeypatch.setattr(
        image.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="pack-123"),
    )

    assert image._resolve_uploaded_build_pack_room_path(
        parsed_tag=parsed_tag,
        room_path=None,
    ) == ("/temp/build/packs/pack-123", True)


def test_build_pack_ref_name_uses_room_storage_path_latest() -> None:
    assert image._build_pack_ref_name_for_room_path(room_path="/nested/website") == (
        "room.meshagent.com/nested/website:latest"
    )


def test_infer_deploy_ports_from_packed_dockerfile_reads_expose_lines(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        "FROM node:22-alpine\nEXPOSE 8080\nEXPOSE 8443/tcp 5353/udp \\\n  9000\n",
        encoding="utf-8",
    )

    ports = image._infer_deploy_ports_from_packed_dockerfile(
        local_packed_dockerfile=dockerfile_path
    )

    assert ports is not None
    assert [port.num for port in ports] == [8080, 8443, 9000]
    assert [port.type for port in ports] == ["http", "http", "http"]
    assert [port.published for port in ports] == [True, True, True]


def test_infer_deploy_ports_from_packed_dockerfile_rejects_reserved_port(
    tmp_path: Path,
) -> None:
    dockerfile_path = tmp_path / "Dockerfile"
    dockerfile_path.write_text(
        f"FROM node:22-alpine\nEXPOSE {ROOM_INTERNAL_API_PORT}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        typer.BadParameter,
        match="reserved MeshAgent room infrastructure port",
    ):
        image._infer_deploy_ports_from_packed_dockerfile(
            local_packed_dockerfile=dockerfile_path
        )


@pytest.mark.asyncio
async def test_build_image_defaults_context_path_from_mount_room_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_spec = object()
    captured: dict[str, object] = {}
    parse_mount_args: dict[str, object] = {}

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 0

    def _fake_parse_image_operation_mounts(**kwargs):
        parse_mount_args.update(kwargs)
        return mount_spec

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(
        image,
        "_parse_image_operation_mounts",
        _fake_parse_image_operation_mounts,
    )
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id=None,
        room="room-1",
        tag="repo/name:tag",
        context_path=None,
        dockerfile_path=None,
        pack=None,
        pack_room_path=None,
        mount_room_path=["/src"],
        mount_project_path=[],
        mount_image=[],
        private=False,
        optimize=True,
        cred=[],
    )

    assert parse_mount_args == {
        "mount_room_path": ["/src:/context"],
        "mount_project_path": [],
        "mount_image": [],
    }
    assert captured["build_kwargs"] == {
        "tag": "repo/name:tag",
        "mounts": [mount_spec],
        "context_path": "/context",
        "dockerfile_path": None,
        "optimize_image": True,
        "private": False,
        "credentials": [],
        "context_archive_path": None,
        "context_archive_ref": None,
        "context_archive_mount_path": None,
        "context_archive_arch": None,
    }


@pytest.mark.asyncio
async def test_build_image_can_disable_room_image_optimization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "build-1"

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.containers = _FakeContainers()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_stream_build_job_logs_and_wait_for_exit(
        *,
        client,
        build_id: str,
    ) -> int:
        del client, build_id
        return 0

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "_parse_creds", lambda values: [])
    monkeypatch.setattr(
        image,
        "_stream_build_job_logs_and_wait_for_exit",
        _fake_stream_build_job_logs_and_wait_for_exit,
    )

    await image.build_image(
        project_id=None,
        room="room-1",
        tag="room.meshagent.com/website:1",
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        pack=None,
        pack_room_path=None,
        mount_room_path=[],
        mount_project_path=[],
        mount_image=[],
        private=False,
        optimize=False,
        cred=[],
    )

    assert captured["build_kwargs"] == {
        "tag": "room.meshagent.com/website:1",
        "mounts": [],
        "context_path": "/context",
        "dockerfile_path": "/context/Dockerfile",
        "optimize_image": False,
        "private": False,
        "credentials": [],
        "context_archive_path": None,
        "context_archive_ref": None,
        "context_archive_mount_path": None,
        "context_archive_arch": None,
    }


@pytest.mark.asyncio
async def test_build_image_requires_context_path_for_multiple_mount_targets() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--context-path is required when multiple mount targets are provided",
    ):
        await image.build_image(
            project_id=None,
            room="room-1",
            tag="repo/name:tag",
            context_path=None,
            dockerfile_path=None,
            pack=None,
            pack_room_path=None,
            mount_room_path=["/src:/workspace"],
            mount_project_path=["/docs:/docs"],
            mount_image=[],
            private=False,
            cred=[],
        )


@pytest.mark.asyncio
async def test_build_image_pack_requires_local_dockerfile_when_used_as_context(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "website"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter,
        match="no Dockerfile or Containerfile found in the packed context",
    ):
        await image.build_image(
            project_id=None,
            room="room-1",
            tag="room.meshagent.com/website:1",
            context_path=None,
            dockerfile_path=None,
            pack=str(source_dir),
            arch="arm64",
            pack_room_path=None,
            mount_room_path=[],
            mount_project_path=[],
            mount_image=[],
            private=False,
            cred=[],
        )


def test_require_room_pack_tag_rejects_non_room_meshagent_tag() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--pack requires --tag to start with room.meshagent.com/",
    ):
        image._require_room_pack_tag(parsed_tag=image._parse_build_tag("website:1"))


@pytest.mark.asyncio
async def test_pack_image_requires_output_without_room(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter, match="--output is required unless --room is set"
    ):
        await image.pack_image(
            project_id=None,
            room=None,
            path=str(source_dir),
            tag=None,
            output=None,
            base=None,
            arch="amd64",
            room_path=None,
        )


@pytest.mark.asyncio
async def test_pack_image_requires_tag_when_uploading_to_room(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter, match="--tag is required when --room is set"
    ):
        await image.pack_image(
            project_id=None,
            room="room-1",
            path=str(source_dir),
            tag=None,
            output=str(tmp_path / "sample.tar"),
            base=None,
            arch="arm64",
            room_path=None,
        )


@pytest.mark.asyncio
async def test_pack_image_requires_room_meshagent_tag_when_uploading_to_room(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sample"
    source_dir.mkdir()

    with pytest.raises(
        typer.BadParameter,
        match="--pack requires --tag to start with room.meshagent.com/",
    ):
        await image.pack_image(
            project_id=None,
            room="room-1",
            path=str(source_dir),
            tag="website:1",
            output=str(tmp_path / "sample.tar"),
            base=None,
            arch="arm64",
            room_path=None,
        )


def test_parse_build_tag_rejects_invalid_oci_reference() -> None:
    with pytest.raises(typer.BadParameter, match="invalid OCI image repository"):
        image._parse_build_tag("Bad/Name:latest")


def test_parse_env_token_scope_supports_presets_and_json() -> None:
    user_default = image._parse_env_token_scope(value="userDefault")
    custom = image._parse_env_token_scope(value='{"queues":{"send":["jobs"]}}')

    assert user_default.secrets is not None
    assert user_default.admin is None
    assert custom.queues is not None
    assert custom.queues.send == ["jobs"]


@pytest.mark.asyncio
async def test_deploy_image_creates_room_service_with_mounts_env_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=["/assets:/srv/assets:ro"],
        project_mount=["configs:/etc/config:rw"],
        empty_dir_mount=["/tmp/cache"],
        image_mount=["busybox=/opt/base:rw"],
        env=["FOO=bar"],
        env_token="agentDefault",
        private=True,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    assert created_service[0] == "project-1"
    assert created_service[1] == "room-1"
    service_spec = created_service[2]
    assert service_spec.metadata.name == "repo-web"
    assert service_spec.container is not None
    assert service_spec.container.image == "repo/web:1"
    assert service_spec.container.storage is not None
    assert service_spec.container.storage.room is not None
    assert service_spec.container.storage.room[0].subpath == "/assets"
    assert service_spec.container.storage.room[0].path == "/srv/assets"
    assert service_spec.container.storage.room[0].read_only is True
    assert service_spec.container.storage.project is not None
    assert service_spec.container.storage.project[0].subpath == "configs"
    assert service_spec.container.storage.project[0].path == "/etc/config"
    assert service_spec.container.storage.project[0].read_only is False
    assert service_spec.container.storage.images is not None
    assert service_spec.container.storage.images[0].image == "busybox"
    assert service_spec.container.storage.images[0].path == "/opt/base"
    assert service_spec.container.storage.images[0].read_only is False
    assert service_spec.container.storage.empty_dirs is not None
    assert service_spec.container.storage.empty_dirs[0].path == "/tmp/cache"
    env_by_name = {
        env_var.name: env_var for env_var in (service_spec.container.environment or [])
    }
    assert env_by_name["FOO"].value == "bar"
    assert env_by_name["MESHAGENT_TOKEN"].token is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.identity == "repo-web"
    assert env_by_name["MESHAGENT_TOKEN"].token.api is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.secrets is None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.services is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.role == "agent"
    assert "restarted_service_id" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_builds_before_deploying(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {"events": []}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        "FROM scratch\nEXPOSE 8080\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> None:
        captured["build_kwargs"] = kwargs
        captured["events"].append("build")

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["events"].append("create")
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="room.meshagent.com/repo/web:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        arch="arm64",
        pack_room_path="/packed/context",
        optimize=False,
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
    )

    build_kwargs = captured["build_kwargs"]
    assert build_kwargs["resolved_project_id"] == "project-1"
    assert build_kwargs["resolved_room"] == "room-1"
    assert build_kwargs["parsed_tag"].value == "room.meshagent.com/repo/web:1"
    assert build_kwargs["context_path"] == "/context"
    assert build_kwargs["dockerfile_path"] == "/context/Dockerfile"
    assert build_kwargs["pack"] == str(source_dir)
    assert build_kwargs["arch"] == "arm64"
    assert build_kwargs["pack_room_path"] == "/packed/context"
    assert build_kwargs["mount_room_path"] == []
    assert build_kwargs["mount_project_path"] == []
    assert build_kwargs["mount_image"] == []
    assert build_kwargs["private"] is False
    assert build_kwargs["optimize"] is False
    assert build_kwargs["cred"] == []
    assert captured["events"] == ["build", "create"]
    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.container is not None
    assert service_spec.container.image == "room.meshagent.com/repo/web:1"
    assert service_spec.metadata.annotations is not None
    assert (
        service_spec.metadata.annotations[image.ANNOTATION_REQUEST_VALIDATION_METHOD]
        == "cookie"
    )
    assert service_spec.ports is not None
    assert service_spec.ports[0].num == 8080
    assert service_spec.ports[0].published is True
    assert service_spec.ports[0].public is None
    assert service_spec.ports[0].liveness == "/"
    assert service_spec.ports[0].annotations == {
        image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie"
    }
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_pack_domain_uses_inferred_exposed_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    source_dir = tmp_path / "website"
    source_dir.mkdir()
    (source_dir / "Dockerfile").write_text(
        "FROM node:22-alpine\nEXPOSE 8080\n",
        encoding="utf-8",
    )

    async def _fake_run_image_build_stage(**kwargs) -> None:
        captured["build_kwargs"] = kwargs

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = SimpleNamespace(restart=self._restart)

        async def _restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def create_route(
            self,
            *,
            project_id: str,
            domain: str,
            room_name: str,
            port: str,
            annotations: dict[str, str] | None = None,
        ) -> None:
            captured["created_route"] = (
                project_id,
                domain,
                room_name,
                port,
                annotations,
            )

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_run_image_build_stage", _fake_run_image_build_stage)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="room.meshagent.com/repo/web:1",
        pack=str(source_dir),
        context_path="/context",
        dockerfile_path="/context/Dockerfile",
        arch="amd64",
        pack_room_path=None,
        optimize=True,
        domain="node.meshagent.dev",
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.ports is not None
    assert len(service_spec.ports) == 1
    assert service_spec.ports[0].num == 8080
    assert service_spec.ports[0].published is True
    assert service_spec.ports[0].public is None
    assert service_spec.ports[0].liveness == "/"
    assert service_spec.metadata.annotations is not None
    assert (
        service_spec.metadata.annotations[image.ANNOTATION_REQUEST_VALIDATION_METHOD]
        == "cookie"
    )
    assert service_spec.ports[0].annotations == {
        image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie"
    }
    assert captured["created_route"] == (
        "project-1",
        "node.meshagent.dev",
        "room-1",
        "8080",
        {image.ANNOTATION_SERVICE_ID: "repo-web"},
    )
    assert "restarted_service_id" not in captured
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_build_options_require_pack() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="--context-path requires --pack",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:1",
            pack=None,
            context_path="/context",
            dockerfile_path=None,
            arch=image.DEFAULT_ARCHITECTURE,
            pack_room_path=None,
            optimize=True,
            domain=None,
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            env_token=None,
            private=True,
        )


@pytest.mark.asyncio
async def test_deploy_image_sets_cookie_validation_when_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return []

        async def create_room_service(
            self, *, project_id: str, room_name: str, service
        ):
            captured["created_service"] = (project_id, room_name, service)
            return "service-1"

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:1",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
        private=True,
    )

    created_service = captured["created_service"]
    assert isinstance(created_service, tuple)
    service_spec = created_service[2]
    assert service_spec.metadata.annotations is not None
    assert (
        service_spec.metadata.annotations[image.ANNOTATION_REQUEST_VALIDATION_METHOD]
        == "cookie"
    )
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_updates_existing_service_route_and_preserves_token_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[PortSpec(num=8080, type="http", published=True)],
        container=ContainerSpec(
            image="repo/web:old",
            environment=[
                EnvironmentVariable(name="KEEP", value="1"),
                EnvironmentVariable(
                    name="MESHAGENT_TOKEN",
                    token=TokenValue(
                        identity="existing-id",
                        api=ApiScope.agent_default(),
                        role="tool",
                    ),
                ),
            ],
            storage=ContainerMountSpec(
                images=[
                    ImageStorageMountSpec(
                        image="base:1",
                        path="/opt/base",
                        read_only=True,
                    )
                ]
            ),
        ),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            captured["updated_service"] = (
                project_id,
                room_name,
                service_id,
                service,
            )

        async def create_route(
            self,
            *,
            project_id: str,
            domain: str,
            room_name: str,
            port: str,
            annotations: dict[str, str] | None = None,
        ) -> None:
            captured["created_route"] = (
                project_id,
                domain,
                room_name,
                port,
                annotations,
            )

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain="app.meshagent.app",
        room_mount=["/workspace:/srv/work:rw"],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=["FOO=bar"],
        env_token="full",
        private=False,
    )

    updated_service = captured["updated_service"]
    assert isinstance(updated_service, tuple)
    assert updated_service[0] == "project-1"
    assert updated_service[1] == "room-1"
    assert updated_service[2] == "service-1"
    updated_spec = updated_service[3]
    assert updated_spec.container is not None
    assert updated_spec.container.image == "repo/web:2"
    assert updated_spec.container.storage is not None
    assert updated_spec.container.storage.room is not None
    assert updated_spec.container.storage.room[0].subpath == "/workspace"
    assert updated_spec.container.storage.room[0].path == "/srv/work"
    assert updated_spec.container.storage.room[0].read_only is False
    assert updated_spec.container.storage.images is not None
    assert updated_spec.container.storage.images[0].image == "base:1"
    env_by_name = {
        env_var.name: env_var for env_var in (updated_spec.container.environment or [])
    }
    assert env_by_name["KEEP"].value == "1"
    assert env_by_name["FOO"].value == "bar"
    assert env_by_name["MESHAGENT_TOKEN"].token is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.identity == "existing-id"
    assert env_by_name["MESHAGENT_TOKEN"].token.role == "tool"
    assert env_by_name["MESHAGENT_TOKEN"].token.api is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.admin is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.secrets is not None
    assert env_by_name["MESHAGENT_TOKEN"].token.api.tunnels is not None
    assert updated_spec.ports is not None
    assert updated_spec.ports[0].liveness == "/"
    assert updated_spec.ports[0].public is True
    assert captured["created_route"] == (
        "project-1",
        "app.meshagent.app",
        "room-1",
        "8080",
        {image.ANNOTATION_SERVICE_ID: "repo-web"},
    )
    assert captured["restarted_service_id"] == "service-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_deploy_image_sets_cookie_validation_on_private_published_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                public=True,
                annotations={"keep": "1"},
            )
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            captured["room_client_closed"] = True

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            captured["updated_service"] = (
                project_id,
                room_name,
                service_id,
                service,
            )

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_with_client(*, project_id, room):
        captured["project_id"] = project_id
        captured["room"] = room
        return _FakeAccountClient(), _FakeRoomClient()

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)
    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert isinstance(updated_service, tuple)
    updated_spec = updated_service[3]
    assert updated_spec.metadata.annotations is not None
    assert (
        updated_spec.metadata.annotations[image.ANNOTATION_REQUEST_VALIDATION_METHOD]
        == "cookie"
    )
    assert updated_spec.ports is not None
    assert updated_spec.ports[0].liveness == "/"
    assert updated_spec.ports[0].public is None
    assert updated_spec.ports[0].annotations == {
        "keep": "1",
        image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie",
    }
    assert captured["restarted_service_id"] == "service-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


def test_update_request_validation_annotations_removes_cookie_when_public() -> None:
    assert image._update_request_validation_annotations(
        annotations={
            image.ANNOTATION_REQUEST_VALIDATION_METHOD: "cookie",
            "keep": "1",
        },
        public=True,
    ) == {"keep": "1"}


@pytest.mark.asyncio
async def test_deploy_image_preserves_existing_liveness_when_default_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                liveness="/ready",
            )
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id
            captured["updated_service"] = service

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert updated_service.ports is not None
    assert updated_service.ports[0].liveness == "/ready"


@pytest.mark.asyncio
async def test_deploy_image_liveness_flag_overrides_http_ports_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(
                num=8080,
                type="http",
                published=True,
                liveness="/ready",
            ),
            PortSpec(
                num=9090,
                type="tcp",
                published=False,
            ),
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeServices:
        async def restart(self, *, service_id: str) -> None:
            captured["restarted_service_id"] = service_id

    class _FakeRoomClient:
        def __init__(self) -> None:
            self.services = _FakeServices()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            del project_id, room_name
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id
            captured["updated_service"] = service

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    await image.deploy_image(
        project_id="project-1",
        room="room-1",
        tag="repo/web:2",
        domain=None,
        liveness="/healthz",
        room_mount=[],
        project_mount=[],
        empty_dir_mount=[],
        image_mount=[],
        env=[],
        env_token=None,
        private=True,
    )

    updated_service = captured["updated_service"]
    assert updated_service.ports is not None
    assert updated_service.ports[0].liveness == "/healthz"
    assert updated_service.ports[1].liveness is None


@pytest.mark.asyncio
async def test_deploy_image_domain_requires_exactly_one_published_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    existing_service = ServiceSpec(
        version="v1",
        kind="Service",
        id="service-1",
        metadata=ServiceMetadata(
            name="repo-web",
            annotations={image.ANNOTATION_SERVICE_ID: "repo-web"},
        ),
        ports=[
            PortSpec(num=8080, type="http", published=True),
            PortSpec(num=9090, type="http", published=True),
        ],
        container=ContainerSpec(image="repo/web:old"),
    )

    class _FakeRoomClient:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb

    class _FakeAccountClient:
        async def list_room_services(self, *, project_id: str, room_name: str):
            captured.setdefault("list_room_services", []).append(
                (project_id, room_name)
            )
            return [existing_service]

        async def update_room_service(
            self,
            *,
            project_id: str,
            room_name: str,
            service_id: str,
            service,
        ) -> None:
            del project_id, room_name, service_id, service
            captured["update_room_service_called"] = True

        async def close(self) -> None:
            return None

    async def _fake_with_client(*, project_id, room):
        del project_id, room
        return _FakeAccountClient(), _FakeRoomClient()

    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(image, "resolve_room", lambda room: room)

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    monkeypatch.setattr(image, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(image, "print", lambda *args, **kwargs: None)

    with pytest.raises(
        typer.BadParameter,
        match="--domain requires exactly one published service port",
    ):
        await image.deploy_image(
            project_id="project-1",
            room="room-1",
            tag="repo/web:2",
            domain="app.meshagent.app",
            room_mount=[],
            project_mount=[],
            empty_dir_mount=[],
            image_mount=[],
            env=[],
            env_token=None,
            private=True,
        )

    assert "update_room_service_called" not in captured
