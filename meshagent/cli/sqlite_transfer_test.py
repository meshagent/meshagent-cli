from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer

from meshagent.api import RoomException
from meshagent.cli import sqlite


@pytest.fixture
def room_client(monkeypatch):
    account = AsyncMock()
    account.connect_room.return_value.jwt = "test"
    monkeypatch.setattr(sqlite, "get_client", AsyncMock(return_value=account))
    monkeypatch.setattr(sqlite, "resolve_project_id", AsyncMock(return_value="project"))
    monkeypatch.setattr(sqlite, "resolve_room", lambda room: room)
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(sqlite, "RoomClient", MagicMock(return_value=client))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_backup_publishes_only_after_verified_stream(tmp_path, room_client, fail):
    destination = tmp_path / "backup.sqlite"

    async def backup(**kwargs):
        assert kwargs == {"database": "app", "namespace": ["team"]}
        yield b"database bytes"
        assert not destination.exists()
        if fail:
            raise RoomException("checksum mismatch")

    room_client.sqlite.backup = backup

    async def run():
        await sqlite.backup_database(
            project_id="project",
            room="room",
            database="app",
            namespace=["team"],
            output=destination,
        )

    if fail:
        with pytest.raises(typer.Exit):
            await run()
        assert not destination.exists()
    else:
        await run()
        assert destination.read_bytes() == b"database bytes"
    assert not list(tmp_path.glob(".sqlite-backup-*"))


@pytest.mark.asyncio
async def test_backup_never_overwrites_existing_file(tmp_path):
    destination = tmp_path / "backup.sqlite"
    destination.write_bytes(b"original")
    with pytest.raises(typer.BadParameter, match="already exists"):
        await sqlite.backup_database(
            project_id="project", room="room", database="app", output=destination
        )
    assert destination.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_restore_uploads_file_and_reports_only_acknowledged_success(
    tmp_path, room_client, capsys
):
    source = tmp_path / "backup.sqlite"
    original = b"SQLite format 3\x00" + b"x" * 700000
    source.write_bytes(original)

    async def restore(*, database, namespace, source):
        assert database == "new"
        assert namespace == ["team"]
        chunks = [chunk async for chunk in source]
        assert b"".join(chunks) == original
        assert all(len(chunk) <= 256 * 1024 for chunk in chunks)
        raise RoomException("replica sync failed")

    room_client.sqlite.restore = restore
    with pytest.raises(typer.Exit):
        await sqlite.restore_room_database(
            project_id="project",
            room="room",
            database="new",
            namespace=["team"],
            input=source,
        )
    assert "Restored database" not in capsys.readouterr().out
    assert source.read_bytes() == original


@pytest.mark.asyncio
async def test_restore_rejects_live_wal_file(tmp_path):
    source = tmp_path / "live.sqlite"
    source.write_bytes(b"sqlite")
    Path(str(source) + "-wal").touch()
    with pytest.raises(typer.BadParameter, match="standalone SQLite backup"):
        await sqlite.restore_room_database(
            project_id="project", room="room", database="new", input=source
        )
