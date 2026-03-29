from pathlib import Path
from types import SimpleNamespace

import pytest

from meshagent.cli import image


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
    ) -> SimpleNamespace:
        resolved_output_path = output_path.expanduser().resolve()
        captured["source_dir"] = source_dir
        captured["output_path"] = resolved_output_path
        captured["base_image"] = base_image
        captured["architecture"] = architecture
        archive_output.write(b"oci-")
        archive_output.write(b"archive")
        return SimpleNamespace(
            output_path=resolved_output_path,
            ref_name="sample:latest",
        )

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
        output=str(output_path),
        base="python:3.13",
        architecture="arm64",
        room_path="/archives/",
    )

    assert captured["source_dir"] == source_dir
    assert captured["output_path"] == output_path.resolve()
    assert captured["base_image"] == "python:3.13"
    assert captured["architecture"] == "arm64"
    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["remote_path"] == "/archives/sample.oci.tar"
    assert captured["overwrite"] is True
    assert captured["size"] is None
    assert captured["upload_name"] == "sample.oci.tar"
    assert captured["uploaded_bytes"] == b"oci-archive"
    assert output_path.read_bytes() == b"oci-archive"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True


@pytest.mark.asyncio
async def test_build_image_starts_room_build_and_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_spec = object()
    credentials = [object()]
    captured: dict[str, object] = {}

    class _FakeContainers:
        async def build(self, **kwargs) -> str:
            captured["build_kwargs"] = kwargs
            return "container-1"

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

    async def _fake_stream_container_job_logs_and_wait_for_exit(
        *,
        client,
        container_id: str,
    ) -> int:
        captured["wait_client"] = client
        captured["container_id"] = container_id
        return 0

    monkeypatch.setattr(image, "_parse_image_operation_mounts", lambda **_: mount_spec)
    monkeypatch.setattr(image, "_parse_creds", lambda values: credentials)
    monkeypatch.setattr(image, "_with_client", _fake_with_client)
    monkeypatch.setattr(
        image,
        "_stream_container_job_logs_and_wait_for_exit",
        _fake_stream_container_job_logs_and_wait_for_exit,
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
        cred=["registry,user,password"],
    )

    assert captured["project_id"] == "project-1"
    assert captured["room"] == "room-1"
    assert captured["build_kwargs"] == {
        "tag": "repo/name:tag",
        "mounts": [mount_spec],
        "context_path": "/workspace",
        "dockerfile_path": "/workspace/Dockerfile",
        "private": True,
        "credentials": credentials,
    }
    assert captured["container_id"] == "container-1"
    assert captured["room_client_closed"] is True
    assert captured["account_client_closed"] is True
