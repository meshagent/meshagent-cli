import pytest
import typer

from meshagent.cli import api_keys


@pytest.mark.asyncio
async def test_show_prints_activated_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_active_api_key(*, project_id: str) -> str | None:
        assert project_id == "resolved-project"
        return "ma-key-1"

    monkeypatch.setattr(api_keys, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(api_keys, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(api_keys.typer, "echo", printed.append)

    await api_keys.show(project_id="project-1")

    assert printed == ["ma-key-1"]


@pytest.mark.asyncio
async def test_env_prints_shell_export_for_activated_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_active_api_key(*, project_id: str) -> str | None:
        assert project_id == "resolved-project"
        return "ma-key-1"

    monkeypatch.setattr(api_keys, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(api_keys, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(api_keys.typer, "echo", printed.append)

    await api_keys.env(project_id="project-1")

    assert printed == ["export MESHAGENT_API_KEY=ma-key-1"]


@pytest.mark.asyncio
async def test_show_exits_when_no_activated_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed: list[str] = []

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_active_api_key(*, project_id: str) -> str | None:
        assert project_id == "resolved-project"
        return None

    monkeypatch.setattr(api_keys, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(api_keys, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(api_keys, "print", printed.append)

    with pytest.raises(typer.Exit) as exc_info:
        await api_keys.show(project_id="project-1")

    assert exc_info.value.exit_code == 1
    assert printed == [
        "[red]No activated API key found for project resolved-project. "
        "Use meshagent api-key activate or meshagent api-key create "
        "--activate to store one locally.[/red]"
    ]
