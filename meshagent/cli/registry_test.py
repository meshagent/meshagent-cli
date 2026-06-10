from __future__ import annotations

from datetime import datetime, timezone

import pytest
import typer
from meshagent.cli.testing import CliRunner

from meshagent.api.client import NotFoundError, ProjectRepository
from meshagent.cli import async_typer, cli, registry


def _repository(
    *,
    repository_id: str = "repo-1",
    project_id: str = "resolved-project",
    name: str = "apps/demo",
    description: str = "Demo registry",
    annotations: dict[str, str] | None = None,
) -> ProjectRepository:
    return ProjectRepository(
        id=repository_id,
        project_id=project_id,
        name=name,
        description=description,
        annotations=annotations or {"team": "platform"},
        created_at=datetime(2026, 4, 19, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_registry_create_calls_client_with_repository_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    class _FakeClient:
        closed = False
        repository: ProjectRepository | None = None

        async def create_repository(
            self, *, project_id: str, repository
        ) -> ProjectRepository:
            assert project_id == "resolved-project"
            self.repository = repository
            return _repository()

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    await registry.registry_create(
        project_id="project-1",
        name="apps/demo",
        description="Demo registry",
        annotations='{"team":"platform"}',
    )

    assert fake_client.repository is not None
    assert fake_client.repository.name == "apps/demo"
    assert fake_client.repository.description == "Demo registry"
    assert fake_client.repository.annotations == {"team": "platform"}
    assert printed == ["[green]Created registry:[/] apps/demo (repo-1)"]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_update_fills_unspecified_fields_from_existing_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []
    current = _repository(
        description="Existing description", annotations={"team": "ops"}
    )

    class _FakeClient:
        closed = False
        updated_repository = None

        async def get_repository(
            self, *, project_id: str, repository_id: str
        ) -> ProjectRepository:
            assert project_id == "resolved-project"
            assert repository_id == "repo-1"
            return current

        async def update_repository(
            self, *, project_id: str, repository_id: str, repository
        ) -> ProjectRepository:
            assert project_id == "resolved-project"
            assert repository_id == "repo-1"
            self.updated_repository = repository
            return _repository(
                description=repository.description,
                annotations=repository.annotations,
            )

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    await registry.registry_update(
        project_id="project-1",
        repository_id="repo-1",
        name=None,
        description="Updated description",
        annotations=None,
    )

    assert fake_client.updated_repository is not None
    assert fake_client.updated_repository.name == "apps/demo"
    assert fake_client.updated_repository.description == "Updated description"
    assert fake_client.updated_repository.annotations == {"team": "ops"}
    assert printed == ["[green]Updated registry:[/] apps/demo (repo-1)"]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_list_prints_table_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    class _FakeClient:
        closed = False

        async def list_repositories(
            self, *, project_id: str
        ) -> list[ProjectRepository]:
            assert project_id == "resolved-project"
            return [_repository()]

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print_json_table", fake_print_json_table)

    await registry.registry_list(project_id="project-1", o="table")

    assert printed == [
        (
            [
                {
                    "id": "repo-1",
                    "name": "apps/demo",
                    "description": "Demo registry",
                    "created_at": "2026-04-19T00:00:00+00:00",
                }
            ],
            ("id", "name", "description", "created_at"),
        )
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_delete_accepts_repository_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    class _FakeClient:
        closed = False
        deleted: tuple[str, str] | None = None

        async def list_repositories(
            self, *, project_id: str
        ) -> list[ProjectRepository]:
            assert project_id == "resolved-project"
            return [_repository(repository_id="repo-1", name="website-node")]

        async def delete_repository(
            self, *, project_id: str, repository_id: str
        ) -> None:
            assert project_id == "resolved-project"
            assert repository_id == "repo-1"
            self.deleted = (project_id, repository_id)

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    await registry.registry_delete(project_id="project-1", repository="website-node")

    assert fake_client.deleted == ("resolved-project", "repo-1")
    assert printed == ["[green]Deleted registry:[/] website-node (repo-1)"]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_delete_accepts_name_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    class _FakeClient:
        closed = False
        deleted: tuple[str, str] | None = None

        async def list_repositories(
            self, *, project_id: str
        ) -> list[ProjectRepository]:
            assert project_id == "resolved-project"
            return [_repository(repository_id="repo-1", name="website-node")]

        async def delete_repository(
            self, *, project_id: str, repository_id: str
        ) -> None:
            assert project_id == "resolved-project"
            assert repository_id == "repo-1"
            self.deleted = (project_id, repository_id)

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    await registry.registry_delete(
        project_id="project-1",
        repository=None,
        name="website-node",
    )

    assert fake_client.deleted == ("resolved-project", "repo-1")
    assert printed == ["[green]Deleted registry:[/] website-node (repo-1)"]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_delete_exits_on_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    class _FakeClient:
        closed = False

        async def list_repositories(
            self, *, project_id: str
        ) -> list[ProjectRepository]:
            assert project_id == "resolved-project"
            return []

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    with pytest.raises(typer.Exit) as exc_info:
        await registry.registry_delete(project_id="project-1", repository="repo-1")

    assert exc_info.value.exit_code == 1
    assert printed == ["[red]Registry not found:[/] repo-1"]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_registry_delete_exits_when_repository_is_deleted_after_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    class _FakeClient:
        closed = False

        async def list_repositories(
            self, *, project_id: str
        ) -> list[ProjectRepository]:
            assert project_id == "resolved-project"
            return [_repository(repository_id="repo-1", name="website-node")]

        async def delete_repository(
            self, *, project_id: str, repository_id: str
        ) -> None:
            assert project_id == "resolved-project"
            assert repository_id == "repo-1"
            raise NotFoundError("not found")

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return fake_client

    monkeypatch.setattr(registry, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(registry, "get_client", fake_get_client)
    monkeypatch.setattr(registry, "print", printed.append)

    with pytest.raises(typer.Exit) as exc_info:
        await registry.registry_delete(
            project_id="project-1", repository="website-node"
        )

    assert exc_info.value.exit_code == 1
    assert printed == ["[red]Registry not found:[/] website-node"]
    assert fake_client.closed is True


def test_registry_command_is_available() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["registry", "--help"]
    )

    assert result.exit_code == 0
    assert "Manage registries for your project" in result.output


def test_registry_delete_help_mentions_name_option() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(registry.app), ["delete", "--help"]
    )

    assert result.exit_code == 0
    assert "Repository id or name to delete" in result.output
    assert "--name" in result.output
