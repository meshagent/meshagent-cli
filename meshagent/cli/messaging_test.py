import pytest

from meshagent.cli import messaging
from meshagent.cli.messaging import wait_for_messaging_participants


class _FakeConnection:
    jwt = "jwt-token"
    room_url = "ws://connect-response.example.test/rooms/demo"


class _FakeMessaging:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)

    def get_participants(self):
        if not self.snapshots:
            return []
        return self.snapshots.pop(0)


class _FakeClient:
    def __init__(self, snapshots):
        self.messaging = _FakeMessaging(snapshots)


class _FakeAccountClient:
    def __init__(self) -> None:
        self.closed = False
        self.connect_calls: list[dict[str, str]] = []

    async def connect_room(self, *, project_id: str, room: str) -> _FakeConnection:
        self.connect_calls.append({"project_id": project_id, "room": room})
        return _FakeConnection()

    async def close(self) -> None:
        self.closed = True


class _FakeProtocol:
    created: list[dict[str, str]] = []

    def __init__(self, *, url: str, token: str) -> None:
        self.url = url
        self.token = token
        type(self).created.append({"url": url, "token": token})

    def create_factory(self):
        return lambda: self


class _FakeParticipant:
    def __init__(self, participant_id: str = "participant-1") -> None:
        self.id = participant_id
        self.role = "agent"
        self._attributes = {"messaging_enabled": True}


class _FakeRoomMessaging:
    def __init__(self) -> None:
        self.participants = [_FakeParticipant()]
        self.sent: list[dict[str, object]] = []
        self.broadcasts: list[dict[str, object]] = []
        self.enabled = False
        self.stopped = False

    async def enable(self) -> None:
        self.enabled = True

    def get_participants(self) -> list[_FakeParticipant]:
        return self.participants

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)

    async def broadcast_message(self, **kwargs) -> None:
        self.broadcasts.append(kwargs)

    async def stop(self) -> None:
        self.stopped = True


class _FakeRoomClient:
    last_instance: "_FakeRoomClient | None" = None

    def __init__(self, *, protocol_factory) -> None:
        self.protocol = protocol_factory()
        self.messaging = _FakeRoomMessaging()
        type(self).last_instance = self

    async def __aenter__(self) -> "_FakeRoomClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type
        del exc
        del tb


async def _install_command_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeAccountClient:
    account_client = _FakeAccountClient()
    _FakeProtocol.created.clear()
    _FakeRoomClient.last_instance = None

    async def fake_get_client() -> _FakeAccountClient:
        return account_client

    async def fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    def fake_resolve_room(room: str) -> str:
        return room

    monkeypatch.setattr(messaging, "get_client", fake_get_client)
    monkeypatch.setattr(messaging, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(messaging, "resolve_room", fake_resolve_room)
    monkeypatch.setattr(messaging, "WebSocketClientProtocol", _FakeProtocol)
    monkeypatch.setattr(messaging, "RoomClient", _FakeRoomClient)
    monkeypatch.setattr(messaging, "print", lambda *args, **kwargs: None)
    return account_client


@pytest.mark.asyncio
async def test_wait_for_messaging_participants_waits_for_discovery(monkeypatch):
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("meshagent.cli.messaging.asyncio.sleep", fake_sleep)

    participant = object()
    client = _FakeClient([[], [participant]])

    result = await wait_for_messaging_participants(client, timeout=1)

    assert result == [participant]
    assert sleep_calls == [0.25]


@pytest.mark.asyncio
async def test_list_uses_connect_room_url(monkeypatch):
    account_client = await _install_command_fakes(monkeypatch)

    await messaging.messaging_list_participants_command(
        project_id=None,
        room="demo",
    )

    assert account_client.connect_calls == [{"project_id": "project-1", "room": "demo"}]
    assert account_client.closed is True
    assert _FakeProtocol.created == [
        {"url": _FakeConnection.room_url, "token": _FakeConnection.jwt}
    ]


@pytest.mark.asyncio
async def test_send_uses_connect_room_url(monkeypatch):
    await _install_command_fakes(monkeypatch)

    await messaging.messaging_send_command(
        project_id=None,
        room="demo",
        to_participant_id="participant-1",
        type="chat.message",
        data='{"message": "hello"}',
    )

    room_client = _FakeRoomClient.last_instance
    assert room_client is not None
    assert _FakeProtocol.created == [
        {"url": _FakeConnection.room_url, "token": _FakeConnection.jwt}
    ]
    assert room_client.messaging.sent[0]["message"] == {"message": "hello"}


@pytest.mark.asyncio
async def test_broadcast_uses_connect_room_url(monkeypatch):
    await _install_command_fakes(monkeypatch)

    await messaging.messaging_broadcast_command(
        project_id=None,
        room="demo",
        data='{"message": "hello"}',
    )

    room_client = _FakeRoomClient.last_instance
    assert room_client is not None
    assert _FakeProtocol.created == [
        {"url": _FakeConnection.room_url, "token": _FakeConnection.jwt}
    ]
    assert room_client.messaging.broadcasts[0]["message"] == {"message": "hello"}
