from datetime import datetime, timezone

import pytest

from meshagent.api.client import Room, RoomSession
from meshagent.cli import sessions


class _FakeClient:
    def __init__(
        self,
        *,
        recent_sessions: list[RoomSession] | None = None,
        session_events: list[dict] | None = None,
        session_spans: list[dict] | None = None,
        session_metrics: list[dict] | None = None,
    ) -> None:
        self.recent_sessions = recent_sessions or []
        self.session_events = session_events or []
        self.session_spans = session_spans or []
        self.session_metrics = session_metrics or []
        self.closed = False
        self.list_recent_sessions_calls: list[tuple[str, int, str | None]] = []
        self.list_session_events_calls: list[tuple[str, str]] = []
        self.list_session_spans_calls: list[tuple[str, str]] = []
        self.list_session_metrics_calls: list[tuple[str, str]] = []
        self.get_room_calls: list[tuple[str, str]] = []

    async def list_recent_sessions(
        self,
        *,
        project_id: str,
        limit: int = 25,
        room_id: str | None = None,
    ) -> list[RoomSession]:
        self.list_recent_sessions_calls.append((project_id, limit, room_id))
        return self.recent_sessions

    async def list_session_events(
        self, *, project_id: str, session_id: str
    ) -> list[dict]:
        self.list_session_events_calls.append((project_id, session_id))
        return self.session_events

    async def list_session_spans(
        self, *, project_id: str, session_id: str
    ) -> list[dict]:
        self.list_session_spans_calls.append((project_id, session_id))
        return self.session_spans

    async def list_session_metrics(
        self, *, project_id: str, session_id: str
    ) -> list[dict]:
        self.list_session_metrics_calls.append((project_id, session_id))
        return self.session_metrics

    async def get_room(self, *, project_id: str, name: str) -> Room:
        self.get_room_calls.append((project_id, name))
        return Room(id="room-1", name=name, metadata={})

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

    assert fake_client.list_recent_sessions_calls == [("resolved-project", 25, None)]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {
                    "id": "session-1",
                    "room_id": "room-1",
                    "room_name": "demo",
                    "kind": "room",
                    "agent_id": None,
                    "agent_name": None,
                    "created_at": "2026-03-12T00:00:00Z",
                    "is_active": False,
                    "participants": {"user": 1},
                }
            ],
            (),
        )
    ]


@pytest.mark.asyncio
async def test_list_passes_limit_and_room_name(monkeypatch) -> None:
    fake_client = _FakeClient(
        recent_sessions=[
            RoomSession(
                id="session-1",
                room_id="room-1",
                room_name="demo-room",
                created_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
                is_active=False,
            ),
            RoomSession(
                id="session-2",
                room_id="room-2",
                room_name="other-room",
                created_at=datetime(2026, 3, 11, tzinfo=timezone.utc),
                is_active=False,
            ),
        ]
    )

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print_json_table(records: list[dict], *cols: str) -> None:
        assert records == [
            {
                "id": "session-1",
                "room_id": "room-1",
                "room_name": "demo-room",
                "kind": "room",
                "agent_id": None,
                "agent_name": None,
                "created_at": "2026-03-12T00:00:00Z",
                "is_active": False,
                "participants": None,
            }
        ]
        assert cols == ()

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print_json_table", fake_print_json_table)

    await sessions.list(project_id="project-1", limit=100, room_name="demo-room")

    assert fake_client.get_room_calls == [("resolved-project", "demo-room")]
    assert fake_client.list_recent_sessions_calls == [
        ("resolved-project", 1000, "room-1")
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_get_prints_session_events(monkeypatch) -> None:
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

    await sessions.get(project_id="project-1", session_id="session-1")

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


@pytest.mark.asyncio
async def test_logs_prints_otel_logs(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_events=[
            {"type": "room.started", "data": {"room": "demo"}},
            {
                "type": "otel.log",
                "created_at": "2026-03-12T00:00:00Z",
                "data": {
                    "log": "request finished",
                    "scope_name": "server",
                    "severity_text": "INFO",
                    "log_attributes": {"http.status_code": 200},
                    "resource_attributes": {"service.name": "demo"},
                },
            },
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

    await sessions.logs(
        project_id="project-1",
        session_id="session-1",
        include_attrs=True,
    )

    assert fake_client.list_session_events_calls == [("resolved-project", "session-1")]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {
                    "time": "2026-03-12 00:00:00",
                    "severity": "INFO",
                    "scope": "server",
                    "message": "request finished",
                    "attrs": ("http.status_code=200, service.name=demo"),
                }
            ],
            ("time", "severity", "scope", "message", "attrs"),
        )
    ]


@pytest.mark.asyncio
async def test_logs_json_preserves_raw_otel_log_payload(monkeypatch) -> None:
    raw_log = {
        "type": "otel.log",
        "created_at": "2026-03-12T00:00:00Z",
        "data": {
            "log": "request finished",
            "severity_number": 9,
            "log_attributes": {"http.status_code": 200},
        },
    }
    fake_client = _FakeClient(session_events=[raw_log])
    printed: list[dict] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_json", printed.append)

    await sessions.logs(
        project_id="project-1",
        session_id="session-1",
        output="json",
    )

    assert printed == [{"logs": [raw_log]}]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_metrics_prints_sum_and_histogram_rows(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_metrics=[
            {
                "kind": "sum",
                "metric_name": "http.server.request.count",
                "metric_unit": "{request}",
                "value": 3,
                "ended_at": "2026-03-12T00:00:00Z",
                "metric_attributes": {"http.request.method": "GET"},
            },
            {
                "kind": "histogram",
                "metric_name": "http.server.duration",
                "metric_unit": "ms",
                "count": 2,
                "sum": 42.0,
                "min": 10.0,
                "max": 32.0,
                "bucket_counts": [0, 1, 1],
                "explicit_bounds": [10.0, 50.0],
                "ended_at": "2026-03-12T00:00:01Z",
                "metric_attributes": {"http.route": "/health"},
            },
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

    await sessions.metrics(
        project_id="project-1",
        session_id="session-1",
        include_attrs=True,
        include_buckets=True,
    )

    assert fake_client.list_session_metrics_calls == [("resolved-project", "session-1")]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {
                    "time": "2026-03-12 00:00:00",
                    "kind": "sum",
                    "name": "http.server.request.count",
                    "value": "3",
                    "unit": "{request}",
                    "attrs": "http.request.method=GET",
                },
                {
                    "time": "2026-03-12 00:00:01",
                    "kind": "histogram",
                    "name": "http.server.duration",
                    "value": (
                        "count=2 sum=42 avg=21 min=10 max=32 "
                        "buckets=<=10:0, <=50:1, +Inf:1"
                    ),
                    "unit": "ms",
                    "attrs": "http.route=/health",
                },
            ],
            ("time", "kind", "name", "value", "unit", "attrs"),
        )
    ]


@pytest.mark.asyncio
async def test_metrics_json_preserves_histogram_shape(monkeypatch) -> None:
    histogram = {
        "kind": "histogram",
        "metric_name": "http.server.duration",
        "count": 2,
        "sum": 42.0,
        "bucket_counts": [0, 1, 1],
        "explicit_bounds": [10.0, 50.0],
    }
    fake_client = _FakeClient(session_metrics=[histogram])
    printed: list[dict] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_json", printed.append)

    await sessions.metrics(
        project_id="project-1",
        session_id="session-1",
        output="json",
    )

    assert printed == [{"metrics": [histogram]}]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_list_closes_client_when_no_rows_are_found(monkeypatch) -> None:
    fake_client = _FakeClient()
    printed: list[str] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print(message: str) -> None:
        printed.append(message)

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "print", fake_print)

    await sessions.list(project_id="project-1", room_name="demo-room")

    assert fake_client.closed is True
    assert printed == ["No recent sessions found for room demo-room"]


def test_span_tree_lines_indent_child_spans() -> None:
    lines = sessions._span_tree_lines(
        [
            {
                "trace_id": "trace-1",
                "span_id": "child",
                "parent_span_id": "root",
                "span_name": "child span",
                "created_at": "2026-03-12T00:00:00Z",
                "duration": 1_500_000,
            },
            {
                "trace_id": "trace-1",
                "span_id": "root",
                "span_name": "root span",
                "created_at": "2026-03-12T00:00:00Z",
                "duration": 2_000_000_000,
            },
        ]
    )

    assert lines == [
        "name          time                 duration",
        "root span     2026-03-12 00:00:00  2.00s",
        "  child span  2026-03-12 00:00:00  1.5ms",
    ]


@pytest.mark.asyncio
async def test_traces_prints_session_spans_as_tree(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_spans=[
            {
                "trace_id": "trace-1",
                "span_id": "root",
                "span_name": "root span",
                "created_at": "2026-03-12T00:00:00",
                "duration": 1_000_000,
            }
        ]
    )
    printed: list[str] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print(message: str) -> None:
        printed.append(message)

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_tree_line", fake_print)

    await sessions.traces(project_id="project-1", session_id="session-1")

    assert fake_client.list_session_spans_calls == [("resolved-project", "session-1")]
    assert fake_client.closed is True
    assert printed == [
        "name       time                 duration",
        "root span  2026-03-12 00:00:00  1.0ms",
    ]


@pytest.mark.asyncio
async def test_traces_accepts_session_id_option(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_spans=[
            {
                "trace_id": "trace-1",
                "span_id": "root",
                "span_name": "root span",
                "created_at": "2026-03-12T00:00:00",
                "duration": 1_000_000,
            }
        ]
    )

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_tree_line", lambda message: None)

    await sessions.traces(project_id="project-1", session_id="session-explicit")

    assert fake_client.list_recent_sessions_calls == []
    assert fake_client.list_session_spans_calls == [
        ("resolved-project", "session-explicit")
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_traces_rejects_conflicting_session_ids(monkeypatch) -> None:
    fake_client = _FakeClient()

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)

    with pytest.raises(Exception, match="must match"):
        await sessions.traces(
            project_id="project-1",
            session_arg="session-arg",
            session_id="session-option",
        )

    assert fake_client.list_session_spans_calls == []
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_traces_can_filter_and_print_attrs(monkeypatch) -> None:
    fake_client = _FakeClient(
        session_spans=[
            {
                "trace_id": "trace-1",
                "span_id": "root",
                "span_name": "root span",
                "created_at": "2026-03-12T00:00:00",
                "duration": 1_000_000,
                "span_attributes": {"cache_hit": True},
            },
            {
                "trace_id": "trace-1",
                "span_id": "fast",
                "span_name": "fast span",
                "created_at": "2026-03-12T00:00:01",
                "duration": 1_000,
                "span_attributes": {"cache_hit": False},
            },
        ]
    )
    printed: list[str] = []

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_tree_line", printed.append)

    await sessions.traces(
        project_id="project-1",
        session_id="session-1",
        min_duration="1ms",
        include_attrs=True,
    )

    assert printed == [
        "name       time                 duration  attrs",
        "root span  2026-03-12 00:00:00  1.0ms     cache_hit=True",
    ]


@pytest.mark.asyncio
async def test_traces_uses_most_recent_room_session(monkeypatch) -> None:
    fake_client = _FakeClient(
        recent_sessions=[
            RoomSession(
                id="session-1",
                room_id="room-1",
                room_name="demo-room",
                created_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
                is_active=False,
            )
        ],
        session_spans=[
            {
                "trace_id": "trace-1",
                "span_id": "root",
                "span_name": "root span",
                "created_at": "2026-03-12T00:00:00",
                "duration": 1_000_000,
            }
        ],
    )

    async def fake_get_client() -> _FakeClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(sessions, "get_client", fake_get_client)
    monkeypatch.setattr(sessions, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(sessions, "_print_tree_line", lambda message: None)

    await sessions.traces(project_id="project-1", room_name="demo-room")

    assert fake_client.get_room_calls == [("resolved-project", "demo-room")]
    assert fake_client.list_recent_sessions_calls == [
        ("resolved-project", 1000, "room-1")
    ]
    assert fake_client.list_session_spans_calls == [("resolved-project", "session-1")]
    assert fake_client.closed is True
