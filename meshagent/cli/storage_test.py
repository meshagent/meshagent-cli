from pathlib import Path
from types import SimpleNamespace

import pytest

from meshagent.api.messaging import FileContent
from meshagent.cli import storage


class _FakeStorageClient:
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, object]] = []
        self.download_calls: list[str] = []
        self.stats: dict[str, object | None] = {}
        self.exists_paths: set[str] = set()
        self.files: dict[str, FileContent] = {}

    async def stat(self, *, path: str):
        return self.stats.get(path)

    async def upload(
        self,
        *,
        path: str,
        data: bytes,
        overwrite: bool = False,
        name: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        self.upload_calls.append(
            {
                "path": path,
                "data": data,
                "overwrite": overwrite,
                "name": name,
                "mime_type": mime_type,
            }
        )

    async def exists(self, *, path: str) -> bool:
        return path in self.exists_paths

    async def download(self, *, path: str) -> FileContent:
        self.download_calls.append(path)
        return self.files[path]


class _FakeRoomClient:
    def __init__(self, *, storage_client: _FakeStorageClient) -> None:
        self.storage = storage_client

    async def __aenter__(self) -> "_FakeRoomClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        return None


class _FakeAccountClient:
    def __init__(self) -> None:
        self.closed = False

    async def connect_room(self, *, project_id: str, room: str) -> SimpleNamespace:
        assert project_id == "project-1"
        assert room == "jesse"
        return SimpleNamespace(jwt="token")

    async def close(self) -> None:
        self.closed = True


def _patch_storage_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_client: _FakeStorageClient,
    account_client: _FakeAccountClient,
) -> None:
    monkeypatch.setattr(storage, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "resolve_room", lambda room: room)

    async def fake_get_client() -> _FakeAccountClient:
        return account_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        del project_id
        return "project-1"

    monkeypatch.setattr(storage, "get_client", fake_get_client)
    monkeypatch.setattr(storage, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(storage, "websocket_room_url", lambda room_name: room_name)
    monkeypatch.setattr(storage, "WebSocketClientProtocol", lambda url, token: None)
    monkeypatch.setattr(
        storage,
        "RoomClient",
        lambda protocol: _FakeRoomClient(storage_client=storage_client),
    )


@pytest.mark.asyncio
async def test_storage_cp_uploads_local_file_to_exact_remote_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_file = tmp_path / "dump.rdb"
    source_file.write_bytes(b"redis-dump")

    storage_client = _FakeStorageClient()
    account_client = _FakeAccountClient()
    _patch_storage_command(
        monkeypatch,
        storage_client=storage_client,
        account_client=account_client,
    )

    await storage.storage_cp_command(
        project_id=None,
        room="jesse",
        source_path=str(source_file),
        dest_path="room:///backups/falkordb/dump.rdb",
    )

    assert storage_client.upload_calls == [
        {
            "path": "/backups/falkordb/dump.rdb",
            "data": b"redis-dump",
            "overwrite": True,
            "name": None,
            "mime_type": None,
        }
    ]
    assert account_client.closed is True


@pytest.mark.asyncio
async def test_storage_cp_copies_remote_file_to_exact_remote_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_client = _FakeStorageClient()
    storage_client.exists_paths.add("/source/dump.rdb")
    storage_client.files["/source/dump.rdb"] = FileContent(
        data=b"redis-dump",
        name="dump.rdb",
        mime_type="application/octet-stream",
    )
    account_client = _FakeAccountClient()
    _patch_storage_command(
        monkeypatch,
        storage_client=storage_client,
        account_client=account_client,
    )

    await storage.storage_cp_command(
        project_id=None,
        room="jesse",
        source_path="room:///source/dump.rdb",
        dest_path="room:///backups/falkordb/dump.rdb",
    )

    assert storage_client.download_calls == ["/source/dump.rdb"]
    assert storage_client.upload_calls == [
        {
            "path": "/backups/falkordb/dump.rdb",
            "data": b"redis-dump",
            "overwrite": True,
            "name": None,
            "mime_type": "application/octet-stream",
        }
    ]
    assert account_client.closed is True
