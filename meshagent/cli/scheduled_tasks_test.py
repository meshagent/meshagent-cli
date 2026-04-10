from datetime import datetime, timezone
import json
import os

import pytest
import typer

from meshagent.api.client import NotFoundError, ScheduledTask
from meshagent.cli import scheduled_tasks


def test_load_payload_from_inline_json() -> None:
    payload = scheduled_tasks._load_payload(
        payload='{"action":"sync","count":2}', payload_file=None
    )
    assert payload == {"action": "sync", "count": 2}


def test_load_payload_from_file(tmp_path) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"name": "task"}), encoding="utf-8")

    payload = scheduled_tasks._load_payload(
        payload=None, payload_file=str(payload_file)
    )
    assert payload == {"name": "task"}


def test_load_payload_requires_exactly_one_source() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._load_payload(payload=None, payload_file=None)

    with pytest.raises(typer.BadParameter):
        scheduled_tasks._load_payload(payload="{}", payload_file="/tmp/payload.json")


def test_parse_annotations_accepts_mapping() -> None:
    parsed = scheduled_tasks._parse_annotations('{"env":"prod"}')
    assert parsed == {"env": "prod"}


def test_parse_annotations_rejects_non_mapping() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_annotations('["a","b"]')


def test_parse_annotations_rejects_non_string_values() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_annotations('{"retries": 3}')


def test_parse_active_state_rejects_conflicting_flags() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_active_state(active=True, inactive=True)


class _FakeScheduledTasksClient:
    def __init__(
        self,
        *,
        tasks: list[ScheduledTask] | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.tasks = tasks or []
        self.delete_error = delete_error
        self.closed = False
        self.list_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    async def list_scheduled_tasks(
        self,
        *,
        project_id: str,
        room_name: str | None = None,
        task_id: str | None = None,
        active: bool | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ScheduledTask]:
        self.list_calls.append(
            {
                "project_id": project_id,
                "room_name": room_name,
                "task_id": task_id,
                "active": active,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.tasks

    async def update_scheduled_task(
        self,
        *,
        project_id: str,
        task_id: str,
        room_name: str | None = None,
        queue_name: str | None = None,
        payload: object | None = None,
        schedule: str | None = None,
        active: bool | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        self.update_calls.append(
            {
                "project_id": project_id,
                "task_id": task_id,
                "room_name": room_name,
                "queue_name": queue_name,
                "payload": payload,
                "schedule": schedule,
                "active": active,
                "annotations": annotations,
            }
        )

    async def delete_scheduled_task(self, *, project_id: str, task_id: str) -> None:
        self.delete_calls.append({"project_id": project_id, "task_id": task_id})
        if self.delete_error is not None:
            raise self.delete_error

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_scheduled_task_list_prints_table_rows(monkeypatch) -> None:
    fake_client = _FakeScheduledTasksClient(
        tasks=[
            ScheduledTask(
                id="task-1",
                project_id="project-1",
                room_name="room-1",
                queue_name="queue-1",
                payload={"action": "sync"},
                schedule="0 * * * *",
                active=True,
                once=False,
                annotations={"env": "prod"},
                last_status="succeeded",
                last_start_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            )
        ]
    )
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(scheduled_tasks, "print_json_table", fake_print_json_table)

    await scheduled_tasks.scheduled_task_list(
        project_id="project-1",
        room="room-1",
        task_id="task-1",
        active=True,
        inactive=False,
        limit=10,
        offset=5,
        o="table",
    )

    assert fake_client.list_calls == [
        {
            "project_id": "resolved-project",
            "room_name": "room-1",
            "task_id": "task-1",
            "active": True,
            "limit": 10,
            "offset": 5,
        }
    ]
    assert fake_client.closed is True
    assert printed == [
        (
            [
                {
                    "id": "task-1",
                    "project_id": "project-1",
                    "room_name": "room-1",
                    "queue_name": "queue-1",
                    "payload": {"action": "sync"},
                    "schedule": "0 * * * *",
                    "active": True,
                    "once": False,
                    "annotations": {"env": "prod"},
                    "room_id": "",
                    "last_run_id": "",
                    "last_start_time": "2026-03-16T00:00:00Z",
                    "last_end_time": "",
                    "last_status": "succeeded",
                    "last_return_message": "",
                }
            ],
            (
                "id",
                "room_name",
                "queue_name",
                "schedule",
                "active",
                "once",
                "last_status",
            ),
        )
    ]


@pytest.mark.asyncio
async def test_scheduled_task_list_defaults_room_from_env(
    monkeypatch,
) -> None:
    fake_client = _FakeScheduledTasksClient()

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setenv("MESHAGENT_ROOM", "room-from-env")

    await scheduled_tasks.scheduled_task_list(
        project_id="project-1",
        room=os.getenv("MESHAGENT_ROOM"),
        task_id=None,
        active=False,
        inactive=False,
        limit=200,
        offset=0,
        o="json",
    )

    assert fake_client.list_calls == [
        {
            "project_id": "resolved-project",
            "room_name": "room-from-env",
            "task_id": None,
            "active": None,
            "limit": 200,
            "offset": 0,
        }
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_scheduled_task_update_sends_partial_patch(monkeypatch) -> None:
    fake_client = _FakeScheduledTasksClient()
    printed: list[str] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print(*args, **kwargs) -> None:
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(scheduled_tasks, "print", fake_print)

    monkeypatch.setenv("MESHAGENT_ROOM", "room-from-env")

    await scheduled_tasks.scheduled_task_update(
        project_id="project-1",
        task_id="task-1",
        room=os.getenv("MESHAGENT_ROOM"),
        queue="queue-2",
        schedule="15 * * * *",
        payload='{"action":"refresh"}',
        payload_file=None,
        active=False,
        inactive=True,
        annotations='{"env":"staging"}',
    )

    assert fake_client.update_calls == [
        {
            "project_id": "resolved-project",
            "task_id": "task-1",
            "room_name": "room-from-env",
            "queue_name": "queue-2",
            "payload": {"action": "refresh"},
            "schedule": "15 * * * *",
            "active": False,
            "annotations": {"env": "staging"},
        }
    ]
    assert fake_client.closed is True
    assert printed == ["[green]Updated scheduled task:[/] task-1"]


@pytest.mark.asyncio
async def test_scheduled_task_delete_reports_not_found(monkeypatch) -> None:
    fake_client = _FakeScheduledTasksClient(
        delete_error=NotFoundError("Status=404, body={}")
    )
    printed: list[str] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    def fake_print(*args, **kwargs) -> None:
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(scheduled_tasks, "print", fake_print)

    with pytest.raises(typer.Exit) as exc_info:
        await scheduled_tasks.scheduled_task_delete(
            project_id="project-1",
            task_id="missing-task",
        )

    assert exc_info.value.exit_code == 1
    assert fake_client.delete_calls == [
        {"project_id": "resolved-project", "task_id": "missing-task"}
    ]
    assert fake_client.closed is True
    assert printed == ["[red]Scheduled task not found:[/] missing-task"]
