import pytest
import typer

from meshagent.api.keys import ApiKey, encode_api_key
from meshagent.cli import api_keys


@pytest.mark.asyncio
async def test_list_marks_activated_api_key_in_table_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_project_id = "project-1"
    resolved_project_id = "11111111-1111-1111-1111-111111111111"
    active_key_id = "22222222-2222-2222-2222-222222222222"
    inactive_key_id = "33333333-3333-3333-3333-333333333333"
    active_key = encode_api_key(
        ApiKey(
            id=active_key_id,
            project_id=resolved_project_id,
            secret="secret-value",
        )
    )
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    class _FakeClient:
        closed = False

        async def list_api_keys(self, *, project_id: str) -> dict[str, object]:
            assert project_id == resolved_project_id
            return {
                "keys": [
                    {
                        "id": active_key_id,
                        "name": "active-key",
                        "description": "Current CLI key",
                    },
                    {
                        "id": inactive_key_id,
                        "name": "inactive-key",
                        "description": "Another key",
                    },
                ]
            }

        async def close(self) -> None:
            self.closed = True

    fake_client = _FakeClient()

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == input_project_id
        return resolved_project_id

    async def fake_get_active_api_key(*, project_id: str) -> str | None:
        assert project_id == resolved_project_id
        return active_key

    async def fake_get_client() -> _FakeClient:
        return fake_client

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(api_keys, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(api_keys, "get_active_api_key", fake_get_active_api_key)
    monkeypatch.setattr(api_keys, "get_client", fake_get_client)
    monkeypatch.setattr(api_keys, "print_json_table", fake_print_json_table)

    await api_keys.list(project_id=input_project_id, o="table")

    assert printed == [
        (
            [
                {
                    "active": "*",
                    "id": active_key_id,
                    "name": "active-key",
                    "description": "Current CLI key",
                },
                {
                    "active": "",
                    "id": inactive_key_id,
                    "name": "inactive-key",
                    "description": "Another key",
                },
            ],
            ("active", "id", "name", "description"),
        )
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
async def test_get_prints_activated_api_key(
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

    await api_keys.get(project_id="project-1")

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
async def test_get_exits_when_no_activated_api_key(
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
        await api_keys.get(project_id="project-1")

    assert exc_info.value.exit_code == 1
    assert printed == [
        "[red]No activated API key found for project resolved-project. "
        "Use meshagent api-key activate or meshagent api-key create "
        "--activate to store one locally.[/red]"
    ]
