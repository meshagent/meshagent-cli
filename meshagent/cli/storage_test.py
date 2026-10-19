import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import os
import sys

from aiohttp import web
from aiohttp.client_exceptions import ClientPayloadError
import pytest
import pytest_asyncio

from meshagent.api import Meshagent
from meshagent.api.client import PermissionDeniedError
from meshagent.cli import storage
from meshagent.cli.testing import CliRunner


@dataclass
class FilesFixture:
    url: str = ""
    config_calls: int = 0
    blobs: dict[str, bytes] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    requests: list[tuple[str, str]] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    clients: list[Meshagent] = field(default_factory=list)


@pytest_asyncio.fixture
async def files(monkeypatch) -> AsyncIterator[FilesFixture]:
    state = FilesFixture()
    app = web.Application(client_max_size=16 * 1024 * 1024)

    async def config(request):
        state.config_calls += 1
        return web.json_response({"domains": {"files": state.url}})

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer cli-token"
        state.requests.append((request.method, request.path))
        if request.path.endswith("/denied"):
            return web.json_response({"error": "denied"}, status=403)
        if request.path.endswith("/truncated") and request.method == "GET":
            response = web.StreamResponse(headers={"Content-Length": "10"})
            await response.prepare(request)
            await response.write(b"ab")
            request.transport.close()
            return response
        if request.method == "POST":
            body = await request.json()
            state.actions.append(body)
            if request.path == "/v1/presign":
                return web.json_response(
                    {
                        "url": "https://files.example/signed",
                        "method": body["method"],
                        "expires_at": "2030-01-01T00:00:00Z",
                    }
                )
            if request.query["action"] == "mkdir":
                state.directories.add(request.path)
            else:
                source = body["source"]
                target = source["target"]
                namespace = "rooms" if target["kind"] == "room" else "agents"
                source_path = f"/{target['project_id']}/{namespace}/{target['name']}/{source['path']}"
                state.blobs[request.path] = state.blobs[source_path]
            return web.Response(status=204)
        if "list" in request.query:
            prefix = request.path + request.query.get("prefix", "")
            prefix = prefix.rstrip("/") + "/"
            entries = {}
            for path in [*state.blobs, *state.directories]:
                if path.startswith(prefix):
                    tail = path[len(prefix) :]
                    name = tail.split("/")[0]
                    if name:
                        entries[name] = {
                            "name": name,
                            "is_folder": "/" in tail or path in state.directories,
                        }
            return web.json_response({"entries": list(entries.values())})
        if request.method == "PUT":
            state.blobs[request.path] = await request.read()
            return web.Response(status=204)
        if request.method == "DELETE":
            state.blobs.pop(request.path, None)
            return web.Response(status=204)
        if request.path in state.directories:
            return web.Response(
                headers={"X-Meshagent-Is-Folder": "true", "Content-Length": "0"}
            )
        if request.path not in state.blobs:
            return web.Response(status=404)
        return web.Response(body=state.blobs[request.path])

    app.router.add_get("/config", config)
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port, *_ = runner.addresses[0]
    state.url = f"http://{host}:{port}"

    async def get_client():
        client = Meshagent(base_url=state.url, token="cli-token")
        state.clients.append(client)
        return client

    async def resolve_project_id(*, project_id):
        return project_id or "project-1"

    monkeypatch.setattr(storage, "get_client", get_client)
    monkeypatch.setattr(storage, "resolve_project_id", resolve_project_id)
    monkeypatch.setattr(storage, "resolve_room", lambda room: room or "default-room")
    try:
        yield state
        assert all(client._session.closed for client in state.clients)
    finally:
        for client in state.clients:
            await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_room_upload_streams_binary_to_exact_path(files, tmp_path):
    source = tmp_path / "dump.rdb"
    data = b"\x00\xffredis-dump" * 100000
    source.write_bytes(data)
    await storage.storage_cp_command(
        project_id=None,
        room="MixedCase",
        source_path=str(source),
        dest_path="room:///backups/dump.rdb",
    )
    assert files.blobs["/project-1/rooms/MixedCase/backups/dump.rdb"] == data
    assert files.config_calls == 1
    assert not any("connect" in path for _, path in files.requests)


@pytest.mark.asyncio
async def test_cross_target_copy_is_server_side_and_preserves_source(files):
    source = "/p1/rooms/source/folder/100%2F résumé.bin"
    files.blobs[source] = b"\x00\xffbinary"
    await storage.storage_cp_command(
        project_id=None,
        room=None,
        source_path="meshagent://p1/rooms/source/folder/100%252F%20r%C3%A9sum%C3%A9.bin",
        dest_path="meshagent://p2/agents/dest/copy.bin",
    )
    assert files.blobs["/p2/agents/dest/copy.bin"] == files.blobs[source]
    assert files.actions == [
        {
            "source": {
                "target": {"project_id": "p1", "kind": "room", "name": "source"},
                "path": "folder/100%2F résumé.bin",
            },
            "overwrite": True,
        }
    ]
    assert not any(method == "GET" for method, _ in files.requests)
    assert files.config_calls == 1


@pytest.mark.asyncio
async def test_agent_download_and_glob_keep_binary_bytes(files, tmp_path):
    files.blobs["/project-1/agents/worker/data/one.bin"] = b"\x00\xffone"
    files.blobs["/project-1/agents/worker/data/two.bin"] = b"\xfetwo"
    files.blobs["/project-1/agents/worker/data/no.txt"] = b"excluded"
    await storage.storage_cp_command(
        project_id=None,
        room=None,
        agent="worker",
        source_path="agent://data/*.bin",
        dest_path=str(tmp_path),
    )
    assert (tmp_path / "one.bin").read_bytes() == b"\x00\xffone"
    assert (tmp_path / "two.bin").read_bytes() == b"\xfetwo"
    assert not (tmp_path / "no.txt").exists()


@pytest.mark.asyncio
async def test_recursive_agent_upload_retains_empty_directories(files, tmp_path):
    source = tmp_path / "tree"
    (source / "empty").mkdir(parents=True)
    (source / "one.bin").write_bytes(b"one")
    await storage.storage_cp_command(
        project_id=None,
        room=None,
        agent="worker",
        source_path=str(source),
        dest_path="agent://backup",
        recursive=True,
    )
    assert files.directories == {
        "/project-1/agents/worker/backup",
        "/project-1/agents/worker/backup/empty",
    }
    assert files.blobs["/project-1/agents/worker/backup/one.bin"] == b"one"


async def run_files_cli(files: FilesFixture, args: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from meshagent.cli.cli import app; app()",
        *args,
        env={
            **os.environ,
            "MESHAGENT_API_URL": files.url,
            "MESHAGENT_API_KEY": "cli-token",
            "MESHAGENT_DISABLE_VERSION_CHECK": "1",
            "MESHAGENT_CLI_BUILD": "1",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout + stderr).decode()
    return stdout.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    [["files"], ["room", "storage"], ["agent", "storage"], ["agents", "storage"]],
)
@pytest.mark.parametrize("qualified", [False, True])
async def test_cli_agent_copy_does_not_require_room(
    files, tmp_path, monkeypatch, entrypoint, qualified
):
    monkeypatch.delenv("MESHAGENT_ROOM", raising=False)
    source = tmp_path / "source.bin"
    source.write_bytes(b"\x00\xffagent-upload")
    destination = (
        "meshagent://project-1/agents/worker/upload.bin"
        if qualified
        else "agent://upload.bin"
    )
    args = [*entrypoint, "cp", "--project-id", "project-1"]
    if not qualified:
        args.extend(["--agent", "worker"])
    args.extend([str(source), destination])
    await run_files_cli(files, args)
    assert files.blobs["/project-1/agents/worker/upload.bin"] == source.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["exists", "get", "ls", "rm", "presign"])
async def test_cli_agent_file_commands_do_not_require_room(files, monkeypatch, command):
    monkeypatch.delenv("MESHAGENT_ROOM", raising=False)
    files.blobs["/project-1/agents/worker/file.txt"] = b"agent-data"
    await run_files_cli(
        files,
        [
            "files",
            command,
            "--project-id",
            "project-1",
            "--agent",
            "worker",
            "agent://file.txt",
        ],
    )
    assert files.requests


@pytest.mark.asyncio
async def test_denial_preserves_local_destination(files, tmp_path):
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"keep")
    with pytest.raises(PermissionDeniedError):
        await storage.storage_cp_command(
            project_id=None,
            room=None,
            agent="worker",
            source_path="agent://denied",
            dest_path=str(destination),
        )
    assert destination.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_interrupted_download_preserves_destination_and_removes_staging(
    files, tmp_path
):
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"keep")
    files.blobs["/project-1/agents/worker/truncated"] = b"0123456789"
    with pytest.raises(ClientPayloadError):
        await storage.storage_cp_command(
            project_id=None,
            room=None,
            agent="worker",
            source_path="agent://truncated",
            dest_path=str(destination),
        )
    assert destination.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_recursive_room_root_copy_to_agent_copies_contents_without_a_root_mutation(
    files,
):
    files.directories.add("/project-1/rooms/source/")
    files.blobs["/project-1/rooms/source/a.bin"] = b"binary"
    await storage.storage_cp_command(
        project_id=None,
        room=None,
        source_path="meshagent://project-1/rooms/source/",
        dest_path="meshagent://project-1/agents/dest/",
        recursive=True,
    )
    assert files.blobs["/project-1/agents/dest/a.bin"] == b"binary"
    assert all(body.get("source", {}).get("path") != "" for body in files.actions)


@pytest.mark.asyncio
async def test_local_copy_does_not_discover_or_authenticate(files, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"local")
    destination = tmp_path / "dest.bin"
    await storage.storage_cp_command(
        project_id=None, room=None, source_path=str(source), dest_path=str(destination)
    )
    assert destination.read_bytes() == b"local"
    assert files.config_calls == 0
    assert files.clients == []


@pytest.mark.asyncio
async def test_exists_denial_and_presign_use_agent_target(files, capsys):
    files.blobs["/project-1/agents/worker/file"] = b"data"
    await storage.storage_exists_command(
        project_id=None, room=None, agent="worker", path="agent://file"
    )
    assert "exists" in capsys.readouterr().out
    with pytest.raises(PermissionDeniedError):
        await storage.storage_exists_command(
            project_id=None, room=None, agent="worker", path="agent://denied"
        )
    await storage.storage_presign_command(
        project_id=None,
        room=None,
        agent="worker",
        path="agent://file",
        method="PUT",
        expires_in=120,
    )
    assert files.actions[-1] == {
        "target": {"project_id": "project-1", "kind": "agent", "name": "worker"},
        "path": "file",
        "method": "PUT",
        "expires_in": 120,
    }
    assert '"method":"PUT"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_recursive_listing_and_remove_require_explicit_recursive(files, capsys):
    files.directories.add("/project-1/agents/worker/tree")
    files.blobs["/project-1/agents/worker/tree/file.txt"] = b"text"
    await storage.storage_ls_command(
        project_id=None, room=None, agent="worker", path="agent://", recursive=True
    )
    assert "file.txt" in capsys.readouterr().out
    with pytest.raises(storage.typer.BadParameter, match="-r"):
        await storage.storage_rm_command(
            project_id=None, room=None, agent="worker", path="agent://tree"
        )
    assert not any(method == "DELETE" for method, _ in files.requests)


def test_help_exposes_agent_recursive_and_presign_options():
    runner = CliRunner()
    for command, expected in [
        ("cp", "--recursive"),
        ("get", "--agent"),
        ("presign", "--expires-in"),
    ]:
        result = runner.invoke(storage.app, [command, "--help"], color=False)
        assert result.exit_code == 0, result.output
        assert expected in result.output


def test_conflicting_defaults_and_invalid_qualified_targets_are_rejected():
    with pytest.raises(storage.typer.BadParameter, match="mutually exclusive"):
        storage.StorageContext("project", "room", "agent")
    with pytest.raises(storage.typer.BadParameter):
        storage.parse_path("meshagent://project/other/name/file")
