import pytest

from meshagent.cli import projects


class _FakeClient:
    def __init__(self, project_rows: list[dict[str, str]]) -> None:
        self._project_rows = project_rows
        self.closed = False

    async def list_projects(self) -> dict[str, list[dict[str, str]]]:
        return {"projects": self._project_rows}

    async def close(self) -> None:
        self.closed = True


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
