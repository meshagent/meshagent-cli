from datetime import datetime, timezone
import os

import pytest
import typer

from meshagent.api.client import (
    NotFoundError,
    Room,
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTasksPage,
)
from meshagent.api.specs.service import ScheduledTaskQueueSpec, ScheduledTaskSpec
from meshagent.cli import scheduled_tasks


def test_load_scheduled_task_spec_from_yaml(tmp_path) -> None:
    spec_file = tmp_path / "task.yaml"
    spec_file.write_text(
        """
version: v1
kind: ScheduledTask
schedule: 0 * * * *
queue:
  name: jobs
  payload:
    action: sync
""",
        encoding="utf-8",
    )

    spec = scheduled_tasks._load_scheduled_task_spec(str(spec_file))

    assert spec.schedule == "0 * * * *"
    assert spec.queue is not None
    assert spec.queue.name == "jobs"
    assert spec.queue.payload == {"action": "sync"}


def test_parse_active_state_rejects_conflicting_flags() -> None:
    with pytest.raises(typer.BadParameter):
        scheduled_tasks._parse_active_state(active=True, inactive=True)


class _FakeScheduledTasksClient:
    def __init__(
        self,
        *,
        tasks: list[ScheduledTask] | None = None,
        task_pages: list[ScheduledTasksPage] | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.tasks = tasks or []
        self.task_pages = task_pages
        self.delete_error = delete_error
        self.closed = False
        self.create_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.run_calls: list[dict[str, object]] = []
        self.runs: list[ScheduledTaskRun] = []

    async def list_scheduled_tasks_page(
        self,
        *,
        project_id: str,
        room_id: str | None = None,
        task_id: str | None = None,
        active: bool | None = None,
        page_size: int = 100,
        offset: int = 0,
        continuation_token: str | None = None,
        filter: str | None = None,
    ) -> ScheduledTasksPage:
        self.list_calls.append(
            {
                "project_id": project_id,
                "room_id": room_id,
                "task_id": task_id,
                "active": active,
                "page_size": page_size,
                "offset": offset,
                "continuation_token": continuation_token,
                "filter": filter,
            }
        )
        if self.task_pages is not None:
            return self.task_pages.pop(0)
        return ScheduledTasksPage(tasks=self.tasks)

    async def create_scheduled_task(
        self,
        *,
        project_id: str,
        room_name: str,
        spec: ScheduledTaskSpec,
    ) -> str:
        self.create_calls.append(
            {
                "project_id": project_id,
                "room_name": room_name,
                "spec": spec,
            }
        )
        return "created-task"

    async def update_scheduled_task(
        self,
        *,
        project_id: str,
        task_id: str,
        spec: ScheduledTaskSpec,
    ) -> None:
        self.update_calls.append(
            {
                "project_id": project_id,
                "task_id": task_id,
                "spec": spec,
            }
        )

    async def get_room(self, *, project_id: str, name: str) -> Room:
        del project_id
        return Room(id=f"{name}-id", name=name, metadata={})

    async def list_scheduled_task_runs(
        self,
        *,
        project_id: str,
        task_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ScheduledTaskRun]:
        self.run_calls.append(
            {
                "project_id": project_id,
                "task_id": task_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.runs

    async def delete_scheduled_task(self, *, project_id: str, task_id: str) -> None:
        self.delete_calls.append({"project_id": project_id, "task_id": task_id})
        if self.delete_error is not None:
            raise self.delete_error

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_scheduled_task_add_uses_room_name(monkeypatch, tmp_path) -> None:
    fake_client = _FakeScheduledTasksClient()
    spec_file = tmp_path / "task.yaml"
    spec_file.write_text(
        """
version: v1
kind: ScheduledTask
schedule: 0 * * * *
queue:
  name: queue-1
  payload:
    action: sync
""",
        encoding="utf-8",
    )
    printed: list[str] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(scheduled_tasks, "print", lambda value: printed.append(value))

    await scheduled_tasks.scheduled_task_add(
        project_id="project-1",
        room="room-1",
        file=str(spec_file),
    )

    assert len(fake_client.create_calls) == 1
    call = fake_client.create_calls[0]
    spec = call["spec"]
    assert isinstance(spec, ScheduledTaskSpec)
    assert spec.queue is not None
    assert spec.queue.name == "queue-1"
    assert fake_client.create_calls == [
        {
            "project_id": "resolved-project",
            "room_name": "room-1",
            "spec": spec,
        }
    ]
    assert fake_client.closed is True
    assert printed == ["[green]Created scheduled task:[/] created-task"]


@pytest.mark.asyncio
async def test_scheduled_task_list_prints_table_rows(monkeypatch) -> None:
    fake_client = _FakeScheduledTasksClient(
        tasks=[
            ScheduledTask(
                id="task-1",
                project_id="project-1",
                room_id="room-1-id",
                room_name="room-1",
                spec=ScheduledTaskSpec(
                    schedule="0 * * * *",
                    queue=ScheduledTaskQueueSpec(
                        name="queue-1",
                        payload={"action": "sync"},
                    ),
                ),
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
        filter="queue-1",
        limit=10,
        offset=5,
        o="table",
    )

    assert fake_client.list_calls == [
        {
            "project_id": "resolved-project",
            "room_id": "room-1-id",
            "task_id": "task-1",
            "active": True,
            "page_size": 10,
            "offset": 5,
            "continuation_token": None,
            "filter": "queue-1",
        }
    ]
    assert fake_client.closed is True
    assert len(printed) == 1
    records, columns = printed[0]
    assert columns == (
        "id",
        "room_name",
        "target",
        "queue_name",
        "container_image",
        "schedule",
        "active",
        "once",
        "last_status",
    )
    assert records[0]["id"] == "task-1"
    assert records[0]["room_name"] == "room-1"
    assert records[0]["target"] == "queue"
    assert records[0]["queue_name"] == "queue-1"
    assert records[0]["schedule"] == "0 * * * *"


@pytest.mark.asyncio
async def test_scheduled_task_list_defaults_room_from_env(
    monkeypatch,
) -> None:
    page_tasks = [
        ScheduledTask.model_construct(id=f"task-{index}") for index in range(100)
    ]
    fake_client = _FakeScheduledTasksClient(
        task_pages=[
            ScheduledTasksPage(tasks=page_tasks),
            ScheduledTasksPage(tasks=page_tasks),
        ]
    )

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(scheduled_tasks, "print", lambda *args, **kwargs: None)
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
            "room_id": "room-from-env-id",
            "task_id": None,
            "active": None,
            "page_size": 100,
            "offset": 0,
            "continuation_token": None,
            "filter": None,
        },
        {
            "project_id": "resolved-project",
            "room_id": "room-from-env-id",
            "task_id": None,
            "active": None,
            "page_size": 100,
            "offset": 100,
            "continuation_token": None,
            "filter": None,
        },
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_scheduled_task_list_project_applies_offset_across_pages(
    monkeypatch,
) -> None:
    first_page = [
        ScheduledTask.model_construct(id=f"task-{index}") for index in range(100)
    ]
    second_page = [
        ScheduledTask.model_construct(id=f"task-{index}") for index in range(100, 103)
    ]
    fake_client = _FakeScheduledTasksClient(
        task_pages=[
            ScheduledTasksPage(
                tasks=first_page,
                continuation_token="next-page",
            ),
            ScheduledTasksPage(tasks=second_page),
        ]
    )
    printed: list[list[dict[str, object]]] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.delenv("MESHAGENT_ROOM", raising=False)
    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(
        scheduled_tasks,
        "print_json_table",
        lambda records, *columns: printed.append(records),
    )

    await scheduled_tasks.scheduled_task_list(
        project_id="project-1",
        room=None,
        task_id=None,
        active=False,
        inactive=False,
        filter="daily",
        count=2,
        limit=100,
        offset=101,
        o="table",
    )

    assert fake_client.list_calls == [
        {
            "project_id": "resolved-project",
            "room_id": None,
            "task_id": None,
            "active": None,
            "page_size": 100,
            "offset": 0,
            "continuation_token": None,
            "filter": "daily",
        },
        {
            "project_id": "resolved-project",
            "room_id": None,
            "task_id": None,
            "active": None,
            "page_size": 3,
            "offset": 0,
            "continuation_token": "next-page",
            "filter": "daily",
        },
    ]
    assert [[record["id"] for record in records] for records in printed] == [
        ["task-101", "task-102"]
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_scheduled_task_update_replaces_spec(monkeypatch, tmp_path) -> None:
    fake_client = _FakeScheduledTasksClient()
    printed: list[str] = []
    spec_file = tmp_path / "task.yaml"
    spec_file.write_text(
        """
version: v1
kind: ScheduledTask
schedule: 15 * * * *
inactive: false
queue:
  name: queue-2
  payload:
    action: refresh
""".replace("inactive: false", "active: false"),
        encoding="utf-8",
    )

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
        file=str(spec_file),
    )

    assert len(fake_client.update_calls) == 1
    call = fake_client.update_calls[0]
    spec = call["spec"]
    assert isinstance(spec, ScheduledTaskSpec)
    assert spec.queue is not None
    assert spec.queue.name == "queue-2"
    assert spec.queue.payload == {"action": "refresh"}
    assert spec.active is False
    assert fake_client.update_calls == [
        {
            "project_id": "resolved-project",
            "task_id": "task-1",
            "spec": spec,
        }
    ]
    assert fake_client.closed is True
    assert printed == ["[green]Updated scheduled task:[/] task-1"]


@pytest.mark.asyncio
async def test_scheduled_task_runs_prints_rows(monkeypatch) -> None:
    fake_client = _FakeScheduledTasksClient()
    fake_client.runs = [
        ScheduledTaskRun(
            id="run-1",
            task_id="task-1",
            project_id="project-1",
            room_id="room-1-id",
            room_name="room-1",
            target="container",
            status="failed",
            error="boom",
            container_id="container-1",
            scheduled_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
            started_at=datetime(2026, 3, 16, 0, 0, 1, tzinfo=timezone.utc),
            completed_at=datetime(2026, 3, 16, 0, 0, 2, tzinfo=timezone.utc),
        )
    ]
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    async def fake_get_client() -> _FakeScheduledTasksClient:
        return fake_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(scheduled_tasks, "get_client", fake_get_client)
    monkeypatch.setattr(scheduled_tasks, "resolve_project_id", fake_resolve_project_id)

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(scheduled_tasks, "print_json_table", fake_print_json_table)

    await scheduled_tasks.scheduled_task_runs(
        project_id="project-1",
        task_id="task-1",
        count=10,
        limit=100,
        offset=5,
        o="table",
    )

    assert fake_client.run_calls == [
        {
            "project_id": "resolved-project",
            "task_id": "task-1",
            "limit": 10,
            "offset": 5,
        }
    ]
    assert fake_client.closed is True


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
