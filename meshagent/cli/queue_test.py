import base64
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest

from meshagent.cli import queue


class _FakeConnection:
    def __init__(self) -> None:
        self.jwt = "jwt-token"


class _FakeQueuesClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.queues: list[_FakeQueue] = []

    async def send(self, *, name: str, message: dict) -> None:
        self.sent.append({"name": name, "message": message})

    async def list(self) -> list["_FakeQueue"]:
        return self.queues


class _FakeQueue:
    def __init__(self, *, name: str, size: int) -> None:
        self.name = name
        self.size = size


class _FakeRoomClient:
    last_instance: "_FakeRoomClient | None" = None
    default_queues: list[_FakeQueue] = []

    def __init__(self, *, protocol) -> None:
        self.protocol = protocol
        self.queues = _FakeQueuesClient()
        self.queues.queues = list(type(self).default_queues)
        type(self).last_instance = self

    async def __aenter__(self) -> "_FakeRoomClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type
        del exc
        del tb


class _FakeAccountClient:
    def __init__(self) -> None:
        self.connect_calls: list[dict[str, str]] = []
        self.closed = False

    async def connect_room(self, *, project_id: str, room: str) -> _FakeConnection:
        self.connect_calls.append({"project_id": project_id, "room": room})
        return _FakeConnection()

    async def close(self) -> None:
        self.closed = True


async def _run_send_mail(
    *,
    monkeypatch: pytest.MonkeyPatch,
    room_name: str,
    queue_name: str,
    subject: str,
    body: str,
    from_address: str,
    attachment: list[str],
) -> tuple[_FakeAccountClient, list[str], "_FakeRoomClient"]:
    account_client = _FakeAccountClient()
    printed: list[str] = []

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    def _fake_resolve_room(room: str) -> str:
        return room

    def _fake_print(*args, **kwargs) -> None:
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(queue, "get_client", _fake_get_client)
    monkeypatch.setattr(queue, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(queue, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(queue, "RoomClient", _FakeRoomClient)
    monkeypatch.setattr(queue, "print", _fake_print)

    await queue.send_mail(
        project_id=None,
        room=room_name,
        queue=queue_name,
        subject=subject,
        body=body,
        from_address=from_address,
        attachment=attachment,
    )

    room_client = _FakeRoomClient.last_instance
    assert room_client is not None
    return account_client, printed, room_client


async def _run_size(
    *,
    monkeypatch: pytest.MonkeyPatch,
    room_name: str,
    queue_name: str,
    queues: list[_FakeQueue],
) -> tuple[_FakeAccountClient, list[str]]:
    account_client = _FakeAccountClient()
    printed: list[str] = []

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id):
        del project_id
        return "project-1"

    def _fake_resolve_room(room: str) -> str:
        return room

    def _fake_print(*args, **kwargs) -> None:
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(queue, "get_client", _fake_get_client)
    monkeypatch.setattr(queue, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(queue, "resolve_room", _fake_resolve_room)
    _FakeRoomClient.default_queues = queues
    monkeypatch.setattr(queue, "RoomClient", _FakeRoomClient)
    monkeypatch.setattr(queue, "print", _fake_print)

    await queue.size(
        project_id=None,
        room=room_name,
        queue=queue_name,
    )
    return account_client, printed


@pytest.mark.asyncio
async def test_send_mail_queues_base64_email_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attachment_path = tmp_path / "note.txt"
    attachment_path.write_text("hello attachment", encoding="utf-8")

    account_client, printed, room_client = await _run_send_mail(
        monkeypatch=monkeypatch,
        room_name="demo-room",
        queue_name="agent@mail.meshagent.com",
        subject="Test Subject",
        body="Test Body",
        from_address="sender@example.com",
        attachment=[str(attachment_path)],
    )

    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "demo-room"}
    ]
    assert account_client.closed is True
    assert printed == [
        "[bold green]Connecting to room...[/bold green]",
        "[bold green]Queued email message in agent@mail.meshagent.com.[/bold green]",
    ]
    assert room_client.queues.sent[0]["name"] == "agent@mail.meshagent.com"

    queued_message = room_client.queues.sent[0]["message"]
    assert isinstance(queued_message, dict)
    encoded_message = queued_message["base64"]
    assert isinstance(encoded_message, str)

    email_message = message_from_bytes(
        base64.b64decode(encoded_message),
        policy=default,
    )
    assert email_message["To"] == "agent@mail.meshagent.com"
    assert email_message["From"] == "sender@example.com"
    assert email_message["Subject"] == "Test Subject"
    body_part = email_message.get_body(("plain",))
    assert body_part is not None
    assert body_part.get_content().strip() == "Test Body"

    attachments = list(email_message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "note.txt"
    assert attachments[0].get_content().strip() == "hello attachment"


@pytest.mark.asyncio
async def test_send_mail_allows_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client, printed, room_client = await _run_send_mail(
        monkeypatch=monkeypatch,
        room_name="demo-room",
        queue_name="agent@mail.meshagent.com",
        subject="No Body",
        body="",
        from_address="sender@example.com",
        attachment=[],
    )

    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "demo-room"}
    ]
    assert account_client.closed is True
    assert printed == [
        "[bold green]Connecting to room...[/bold green]",
        "[bold green]Queued email message in agent@mail.meshagent.com.[/bold green]",
    ]
    assert room_client.queues.sent[0]["name"] == "agent@mail.meshagent.com"

    queued_message = room_client.queues.sent[0]["message"]
    assert isinstance(queued_message, dict)
    encoded_message = queued_message["base64"]
    assert isinstance(encoded_message, str)

    email_message = message_from_bytes(
        base64.b64decode(encoded_message),
        policy=default,
    )
    assert email_message["To"] == "agent@mail.meshagent.com"
    assert email_message["From"] == "sender@example.com"
    assert email_message["Subject"] == "No Body"
    body_part = email_message.get_body(("plain",))
    assert body_part is not None
    assert body_part.get_content().strip() == ""
    assert list(email_message.iter_attachments()) == []


@pytest.mark.asyncio
async def test_size_prints_matching_queue_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client, printed = await _run_size(
        monkeypatch=monkeypatch,
        room_name="demo-room",
        queue_name="jobs",
        queues=[
            _FakeQueue(name="jobs", size=3),
            _FakeQueue(name="other", size=1),
        ],
    )

    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "demo-room"}
    ]
    assert account_client.closed is True
    assert printed == ["3"]


@pytest.mark.asyncio
async def test_size_errors_for_missing_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(queue.typer.Exit) as exc_info:
        await _run_size(
            monkeypatch=monkeypatch,
            room_name="demo-room",
            queue_name="missing",
            queues=[_FakeQueue(name="jobs", size=3)],
        )

    assert exc_info.value.exit_code == 1
