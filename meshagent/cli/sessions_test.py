from datetime import datetime, timezone

import pytest

from meshagent.api.client import RoomSession
from meshagent.cli import sessions


class _FakeClient:
    def __init__(
        self,
        *,
        recent_sessions: list[RoomSession] | None = None,
        events: list[dict[str, object]] | None = None,
    ) -> None:
        self.recent_sessions = recent_sessions or []
        self.events = events or []
        self.closed = False
        self.project_id: str | None = None
        self.session_id: str | None = None

    async def list_recent_sessions(self, *, project_id: str) -> list[RoomSession]:
        self.project_id = project_id
        return self.recent_sessions

    async def list_session_events(
        self, *, project_id: str, session_id: str
    ) -> list[dict[str, object]]:
        self.project_id = project_id
        self.session_id = session_id
        return self.events

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_list_prints_typed_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    room_session = RoomSession(
        id="session-1",
        room_id="room-1",
        room_name="example-room",
        created_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
        is_active=False,
        participants=None,
    )
    client = _FakeClient(recent_sessions=[room_session])
    printed: dict[str, object] = {}

    async def fake_get_client() -> _FakeClient:
        return client

    async def fake_resolve_project_id(*, project_id: str | None = None) -> str:
        del project_id
        return "project-1"

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed["records"] = records
        printed["cols"] = cols

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    await sessions.list(project_id="ignored")

    assert client.project_id == "project-1"
    assert client.closed is True
    assert printed["records"] == [room_session.model_dump(mode="json")]
    assert printed["cols"] == ()


@pytest.mark.asyncio
async def test_show_prints_event_list(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [{"type": "message", "data": {"text": "hello"}}]
    client = _FakeClient(events=events)
    printed: dict[str, object] = {}

    async def fake_get_client() -> _FakeClient:
        return client

    async def fake_resolve_project_id(*, project_id: str | None = None) -> str:
        del project_id
        return "project-1"

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed["records"] = records
        printed["cols"] = cols

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    await sessions.show(project_id="ignored", session_id="session-1")

    assert client.project_id == "project-1"
    assert client.session_id == "session-1"
    assert client.closed is True
    assert printed["records"] == events
    assert printed["cols"] == ("type", "data")


@pytest.mark.asyncio
async def test_list_closes_client_when_printing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        recent_sessions=[
            RoomSession(
                id="session-1",
                room_id="room-1",
                room_name="example-room",
                created_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
                is_active=False,
                participants=None,
            )
        ]
    )

    async def fake_get_client() -> _FakeClient:
        return client

    async def fake_resolve_project_id(*, project_id: str | None = None) -> str:
        del project_id
        return "project-1"

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        del records, cols
        raise RuntimeError("boom")

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    with pytest.raises(RuntimeError, match="boom"):
        await sessions.list(project_id="ignored")

    assert client.closed is True
