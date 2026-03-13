from datetime import datetime, timezone

import pytest

from meshagent.api.client import RoomSession
from meshagent.cli import sessions


class _FakeClient:
    def __init__(
        self,
        *,
        recent_sessions: list[RoomSession] | None = None,
        session_events: list[dict] | None = None,
    ) -> None:
        self.recent_sessions = recent_sessions or []
        self.session_events = session_events or []
        self.closed = False
        self.list_recent_sessions_calls: list[str] = []
        self.list_session_events_calls: list[tuple[str, str]] = []

    async def list_recent_sessions(self, *, project_id: str) -> list[RoomSession]:
        self.list_recent_sessions_calls.append(project_id)
        return self.recent_sessions

    async def list_session_events(
        self, *, project_id: str, session_id: str
    ) -> list[dict]:
        self.list_session_events_calls.append((project_id, session_id))
        return self.session_events

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_list_prints_recent_sessions_as_rows(monkeypatch) -> None:
    fake_client = _FakeClient(
        recent_sessions=[
            RoomSession(
                id="session-1",
                room_id="room-1",
                room_name="demo",
                created_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
                is_active=False,
                participants={"user": 1},
            )
        ]
    )
    printed: list[tuple[list[dict], tuple[str, ...]]] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print_json_table(records: list[dict], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    await sessions.list(project_id="project-1")

    assert fake_client.list_recent_sessions_calls == ["resolved-project"]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {
                    "id": "session-1",
                    "room_id": "room-1",
                    "room_name": "demo",
                    "created_at": "2026-03-12T00:00:00Z",
                    "is_active": False,
                    "participants": {"user": 1},
                }
            ],
            (),
        )
    ]


@pytest.mark.asyncio
async def test_show_prints_session_events(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_events=[
            {"type": "room.started", "data": {"room": "demo"}},
            {"type": "room.stopped", "data": {"reason": "done"}},
        ]
    )
    printed: list[tuple[list[dict], tuple[str, ...]]] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print_json_table(records: list[dict], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    await sessions.show(project_id="project-1", session_id="session-1")

    assert fake_client.list_session_events_calls == [("resolved-project", "session-1")]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {"type": "room.started", "data": {"room": "demo"}},
                {"type": "room.stopped", "data": {"reason": "done"}},
            ],
            ("type", "data"),
        )
    ]
