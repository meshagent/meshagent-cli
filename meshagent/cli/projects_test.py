from types import SimpleNamespace

import pytest

from meshagent.api.client import NotFoundError
from meshagent.cli import projects


class _FakeClient:
    def __init__(
        self,
        project_rows: list[dict[str, str]],
        *,
        created_project_id: str = "project-created",
    ) -> None:
        self._project_rows = list(project_rows)
        self._created_project_id = created_project_id
        self.created_project_names: list[str] = []
        self.closed = False

    async def list_projects(self) -> dict[str, list[dict[str, str]]]:
        return {"projects": self._project_rows}

    async def get_project(self, project_id: str) -> dict[str, str]:
        for project in self._project_rows:
            if project["id"] == project_id:
                return project
        raise NotFoundError("not found")

    async def get_project_by_key(self, project_key: str) -> dict[str, str]:
        for project in self._project_rows:
            if project.get("project_key") == project_key:
                return project
        raise NotFoundError("not found")

    async def create_project(self, name: str) -> dict[str, str]:
        self.created_project_names.append(name)
        created_row = {
            "id": self._created_project_id,
            "name": name,
            "project_key": name.lower().replace(" ", "-"),
        }
        self._project_rows.append(created_row)
        return created_row

    async def close(self) -> None:
        self.closed = True


class _FakeTTY:
    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


@pytest.mark.asyncio
async def test_list_includes_project_key_column(monkeypatch) -> None:
    client = _FakeClient(
        [
            {
                "id": "project-1",
                "name": "Powerboards",
                "project_key": "powerboards",
            }
        ]
    )
    printed_tables: list[tuple[object, ...]] = []

    async def _fake_get_client():
        return client

    async def _fake_get_active_project() -> str | None:
        return "project-1"

    def _fake_print_json_table(*args):
        printed_tables.append(args)

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "get_active_project", _fake_get_active_project)
    monkeypatch.setattr(projects, "print_json_table", _fake_print_json_table)

    await projects.list(o="table")

    assert printed_tables == [
        (
            [
                {
                    "id": "project-1",
                    "name": "*Powerboards",
                    "project_key": "powerboards",
                }
            ],
            "id",
            "name",
            "project_key",
        )
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_activate_sets_selected_project(monkeypatch) -> None:
    active_project_ids: list[str | None] = []
    output: list[str] = []
    client = _FakeClient([{"id": "project-1", "name": "Foo"}])

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)
    monkeypatch.setattr(projects, "print", output.append)

    result = await projects.activate(
        project_id="project-1",
        interactive=False,
        return_project_id=False,
    )

    assert result is None
    assert active_project_ids == ["project-1"]
    assert output == ["project-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_activate_accepts_project_key(monkeypatch) -> None:
    active_project_ids: list[str | None] = []
    output: list[str] = []
    client = _FakeClient([{"id": "project-1", "name": "Foo", "project_key": "foo"}])

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)
    monkeypatch.setattr(projects, "print", output.append)

    result = await projects.activate(
        project_id="foo",
        interactive=False,
        return_project_id=False,
    )

    assert result is None
    assert active_project_ids == ["project-1"]
    assert output == ["project-1"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_activate_internal_call_returns_project_id(monkeypatch) -> None:
    active_project_ids: list[str | None] = []
    client = _FakeClient([{"id": "project-1", "name": "Foo"}])

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)

    result = await projects.activate(
        project_id="project-1",
        interactive=False,
        return_project_id=True,
    )

    assert result == "project-1"
    assert active_project_ids == ["project-1"]
    assert client.closed is True


def test_should_launch_activate_tui_when_selector_missing_or_interactive() -> None:
    assert (
        projects._should_launch_activate_tui(
            project_id=None,
            interactive=False,
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is True
    )
    assert (
        projects._should_launch_activate_tui(
            project_id="project-1",
            interactive=True,
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is True
    )
    assert (
        projects._should_launch_activate_tui(
            project_id="project-1",
            interactive=False,
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is False
    )
    assert (
        projects._should_launch_activate_tui(
            project_id=None,
            interactive=False,
            stdin_is_tty=False,
            stdout_is_tty=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_activate_launches_tui_when_project_id_is_omitted_in_tty(
    monkeypatch,
) -> None:
    active_project_ids: list[str | None] = []
    output: list[str] = []
    client = _FakeClient(
        [
            {"id": "project-1", "name": "Alpha"},
            {"id": "project-2", "name": "Beta"},
        ]
    )

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    async def _fake_get_active_project() -> str | None:
        return "project-1"

    async def _fake_run_project_activate_tui(*, selectable_projects):
        assert [
            (project.id, project.name, project.is_active)
            for project in selectable_projects
        ] == [
            ("project-1", "Alpha", True),
            ("project-2", "Beta", False),
        ]
        return SimpleNamespace(
            status="completed",
            message=None,
            selected_project_id="project-2",
            new_project_name=None,
        )

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)
    monkeypatch.setattr(projects, "get_active_project", _fake_get_active_project)
    monkeypatch.setattr(
        projects, "_run_project_activate_tui", _fake_run_project_activate_tui
    )
    monkeypatch.setattr(projects.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects.sys, "stdout", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects, "print", output.append)

    result = await projects.activate(
        project_id=None,
        interactive=False,
        return_project_id=False,
    )

    assert result is None
    assert active_project_ids == ["project-2"]
    assert output == ["project-2"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_activate_creates_project_from_tui_input(monkeypatch) -> None:
    active_project_ids: list[str | None] = []
    output: list[str] = []
    client = _FakeClient([], created_project_id="project-new")

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_project_activate_tui(*, selectable_projects):
        assert selectable_projects == []
        return SimpleNamespace(
            status="completed",
            message=None,
            selected_project_id=None,
            new_project_name="New Project",
        )

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)
    monkeypatch.setattr(projects, "get_active_project", _fake_get_active_project)
    monkeypatch.setattr(
        projects, "_run_project_activate_tui", _fake_run_project_activate_tui
    )
    monkeypatch.setattr(projects.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects.sys, "stdout", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects, "print", output.append)

    result = await projects.activate(
        project_id=None,
        interactive=False,
        return_project_id=False,
    )

    assert result is None
    assert client.created_project_names == ["New Project"]
    assert active_project_ids == ["project-new"]
    assert output == ["project-new"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_activate_prints_cancel_message_when_tui_is_canceled(monkeypatch) -> None:
    output: list[str] = []
    active_project_calls: list[str | None] = []
    client = _FakeClient([{"id": "project-1", "name": "Alpha"}])

    async def _fake_get_client():
        return client

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_calls.append(project_id)

    async def _fake_get_active_project() -> str | None:
        return "project-1"

    async def _fake_run_project_activate_tui(*, selectable_projects):
        del selectable_projects
        return SimpleNamespace(
            status="canceled",
            message="Project activation canceled.",
            selected_project_id=None,
            new_project_name=None,
        )

    monkeypatch.setattr(projects, "get_client", _fake_get_client)
    monkeypatch.setattr(projects, "set_active_project", _fake_set_active_project)
    monkeypatch.setattr(projects, "get_active_project", _fake_get_active_project)
    monkeypatch.setattr(
        projects, "_run_project_activate_tui", _fake_run_project_activate_tui
    )
    monkeypatch.setattr(projects.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects.sys, "stdout", _FakeTTY(is_tty=True))
    monkeypatch.setattr(projects, "print", output.append)

    result = await projects.activate(
        project_id=None,
        interactive=False,
        return_project_id=False,
    )

    assert result is None
    assert active_project_calls == []
    assert output == ["Project activation canceled."]
    assert client.closed is True
