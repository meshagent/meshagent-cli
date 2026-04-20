import asyncio
from types import SimpleNamespace

import pytest

from meshagent.cli import containers
from meshagent.api.room_server_client import ImportedImage


class _FakeLogStream:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.cancelled = asyncio.Event()

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()


class _FakeContainers:
    def __init__(self, *, stream: _FakeLogStream, exit_code: int) -> None:
        self._stream = stream
        self._exit_code = exit_code
        self.log_calls: list[tuple[str, bool]] = []
        self.wait_calls: list[str] = []

    def logs(self, *, container_id: str, follow: bool = False) -> _FakeLogStream:
        self.log_calls.append((container_id, follow))
        return self._stream

    async def wait_for_exit(self, *, container_id: str) -> int:
        self.wait_calls.append(container_id)
        return self._exit_code


class _FakeClient:
    def __init__(self, *, stream: _FakeLogStream, exit_code: int) -> None:
        self.containers = _FakeContainers(stream=stream, exit_code=exit_code)


class _FakeBuildStream:
    def __init__(self, *, lines: list[str], result: str = "image-1") -> None:
        self._lines = lines
        self._result = result

    async def logs(self):
        for line in self._lines:
            yield line

    async def progress(self):
        if False:
            yield None

    def __await__(self):
        async def _done():
            return self._result

        return _done().__await__()


class _FakeBuildContainers:
    def __init__(self, *, stream: _FakeBuildStream) -> None:
        self._stream = stream
        self.build_log_calls: list[tuple[str, bool]] = []

    def get_build_logs(self, *, build_id: str, follow: bool = True) -> _FakeBuildStream:
        self.build_log_calls.append((build_id, follow))
        return self._stream


class _FakeBuildClient:
    def __init__(self, *, stream: _FakeBuildStream) -> None:
        self.containers = _FakeBuildContainers(stream=stream)


class _FakeLoadContainers:
    def __init__(self) -> None:
        self.load_calls: list[str] = []

    async def load(self, *, archive_path: str) -> ImportedImage:
        self.load_calls.append(archive_path)
        return ImportedImage(
            resolved_ref="registry.meshagent.com/images/example.tar:latest",
            refs=["registry.meshagent.com/images/example.tar:latest"],
        )


class _FakeLoadClient:
    def __init__(self) -> None:
        self.containers = _FakeLoadContainers()
        self.exit_calls: list[tuple[object | None, object | None, object | None]] = []

    async def __aexit__(
        self,
        exc_type: object | None,
        exc: object | None,
        tb: object | None,
    ) -> None:
        self.exit_calls.append((exc_type, exc, tb))


class _FakeAccountClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeConnectAccountClient:
    def __init__(self, *, connection: SimpleNamespace) -> None:
        self._connection = connection
        self.close_calls = 0
        self.connect_calls: list[dict[str, str]] = []

    async def connect_room(self, *, project_id: str, room: str) -> SimpleNamespace:
        self.connect_calls.append({"project_id": project_id, "room": room})
        return self._connection

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_stream_container_job_logs_and_wait_for_exit_cancels_follow_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeLogStream()
    client = _FakeClient(stream=stream, exit_code=17)
    drain_started = asyncio.Event()
    drain_cancelled = asyncio.Event()

    async def _fake_drain(log_stream, *, show_progress: bool) -> None:
        assert log_stream is stream
        assert show_progress is False
        drain_started.set()
        await stream.cancelled.wait()
        drain_cancelled.set()

    monkeypatch.setattr(containers, "_drain_stream_plain", _fake_drain)
    monkeypatch.setattr(containers, "_LOG_STREAM_SETTLE_TIMEOUT_SECONDS", 0.01)

    exit_code = await containers._stream_container_job_logs_and_wait_for_exit(
        client=client, container_id="container-1"
    )

    assert exit_code == 17
    assert client.containers.log_calls == [("container-1", True)]
    assert client.containers.wait_calls == ["container-1"]
    assert drain_started.is_set()
    assert stream.cancel_calls == 1
    assert drain_cancelled.is_set()


@pytest.mark.asyncio
async def test_stream_container_job_logs_and_wait_for_exit_does_not_cancel_completed_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeLogStream()
    client = _FakeClient(stream=stream, exit_code=0)
    drain_started = asyncio.Event()

    async def _fake_drain(log_stream, *, show_progress: bool) -> None:
        assert log_stream is stream
        assert show_progress is False
        drain_started.set()

    monkeypatch.setattr(containers, "_drain_stream_plain", _fake_drain)

    exit_code = await containers._stream_container_job_logs_and_wait_for_exit(
        client=client, container_id="container-1"
    )

    assert exit_code == 0
    assert client.containers.log_calls == [("container-1", True)]
    assert client.containers.wait_calls == ["container-1"]
    assert drain_started.is_set()
    assert stream.cancel_calls == 0


@pytest.mark.asyncio
async def test_drain_stream_plain_does_not_double_space_newline_terminated_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _FakeBuildStream(lines=["step 1\n", "step 2\n"])

    result = await containers._drain_stream_plain(stream, show_progress=False)

    assert result == "image-1"
    assert capsys.readouterr().out == "step 1\nstep 2\n"


@pytest.mark.asyncio
async def test_drain_stream_plain_strips_cri_log_prefixes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _FakeBuildStream(
        lines=[
            "2026-03-30T04:21:56.562896627Z stderr F step 1\n",
            "2026-03-30T04:21:57.000000000Z stdout F step 2\n",
        ]
    )

    result = await containers._drain_stream_plain(stream, show_progress=False)

    assert result == "image-1"
    assert capsys.readouterr().out == "step 1\nstep 2\n"


@pytest.mark.asyncio
async def test_stream_build_job_logs_and_wait_for_exit_uses_build_log_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _FakeBuildStream(lines=["line 1\n"], result=0)
    client = _FakeBuildClient(stream=stream)

    async def _fake_drain(log_stream, *, show_progress: bool):
        assert log_stream is stream
        assert show_progress is False
        return await log_stream

    monkeypatch.setattr(containers, "_drain_stream_plain", _fake_drain)

    exit_code = await containers._stream_build_job_logs_and_wait_for_exit(
        client=client,
        build_id="build-1",
    )

    assert exit_code == 0
    assert client.containers.build_log_calls == [("build-1", True)]


@pytest.mark.asyncio
async def test_images_load_uses_room_storage_load_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_client = _FakeAccountClient()
    client = _FakeLoadClient()

    async def _fake_with_client(*, project_id, room):
        assert project_id == "project-1"
        assert room == "room-1"
        return account_client, client

    monkeypatch.setattr(containers, "_with_client", _fake_with_client)

    await containers.images_load(
        project_id="project-1",
        room="room-1",
        archive_path="/images/example.tar",
    )

    assert client.containers.load_calls == ["/images/example.tar"]
    assert client.exit_calls == [(None, None, None)]
    assert account_client.close_calls == 1
    assert (
        capsys.readouterr().out
        == "Image loaded: registry.meshagent.com/images/example.tar:latest\n"
    )


@pytest.mark.asyncio
async def test_with_client_uses_room_url_from_connection_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = SimpleNamespace(
        jwt="jwt-token",
        room_url="wss://room-router.meshagent.dev/custom/room-endpoint",
    )
    account_client = _FakeConnectAccountClient(connection=connection)
    protocol_calls: list[dict[str, str]] = []

    class _FakeProtocol:
        def __init__(self, *, url: str, token: str) -> None:
            protocol_calls.append({"url": url, "token": token})
            self.url = url
            self.token = token

        def create_factory(self):
            return lambda: self

    class _FakeRoomClient:
        def __init__(self, *, protocol_factory) -> None:
            self.protocol = protocol_factory()
            self.enter_calls = 0

        async def __aenter__(self) -> "_FakeRoomClient":
            self.enter_calls += 1
            return self

    async def _fake_get_client() -> _FakeConnectAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id):
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(containers, "get_client", _fake_get_client)
    monkeypatch.setattr(containers, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(containers, "resolve_room", lambda room: f"{room}-resolved")
    monkeypatch.setattr(containers, "WebSocketClientProtocol", _FakeProtocol)
    monkeypatch.setattr(containers, "RoomClient", _FakeRoomClient)

    returned_account_client, client = await containers._with_client(
        project_id="project-1",
        room="room-1",
    )

    assert returned_account_client is account_client
    assert account_client.connect_calls == [
        {"project_id": "resolved-project", "room": "room-1-resolved"}
    ]
    assert protocol_calls == [
        {
            "url": "wss://room-router.meshagent.dev/custom/room-endpoint",
            "token": "jwt-token",
        }
    ]
    assert isinstance(client, _FakeRoomClient)
    assert client.enter_calls == 1
