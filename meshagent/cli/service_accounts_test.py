import pytest

from meshagent.cli import service_accounts
from meshagent.cli.testing import CliRunner


def test_service_account_help_exposes_management_and_api_key_commands() -> None:
    result = CliRunner().invoke(service_accounts.app, ["--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "get" in result.output
    assert "create" in result.output
    assert "update" in result.output
    assert "delete" in result.output
    assert "api-key" in result.output


def test_service_account_api_key_help_exposes_scoped_key_commands() -> None:
    result = CliRunner().invoke(service_accounts.app, ["api-key", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "delete" in result.output
    assert "get" in result.output
    assert "env" in result.output
    assert "activate" in result.output


@pytest.mark.asyncio
async def test_list_service_accounts_prints_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printed_tables: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    class _FakeClient:
        async def list_service_accounts(self, *, project_id: str) -> dict[str, object]:
            assert project_id == "resolved-project"
            return {
                "service_accounts": [
                    {
                        "id": "service-account-1",
                        "name": "worker",
                        "display_name": "Worker",
                        "description": "background jobs",
                    }
                ]
            }

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed_tables.append((records, cols))

    monkeypatch.setattr(service_accounts, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(service_accounts, "get_client", fake_get_client)
    monkeypatch.setattr(service_accounts, "print_json_table", fake_print_json_table)

    await service_accounts.list(project_id="project-1", o="table")

    assert printed_tables == [
        (
            [
                {
                    "id": "service-account-1",
                    "name": "worker",
                    "display_name": "Worker",
                    "description": "background jobs",
                }
            ],
            ("id", "name", "display_name", "description"),
        )
    ]


@pytest.mark.asyncio
async def test_create_service_account_calls_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def create_service_account(
            self,
            *,
            project_id: str,
            name: str,
            display_name: str | None,
            description: str,
            metadata: dict[str, object] | None,
            annotations: dict[str, str] | None,
        ) -> dict[str, object]:
            calls.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "display_name": display_name,
                    "description": description,
                    "metadata": metadata,
                    "annotations": annotations,
                }
            )
            return {
                "id": "service-account-1",
                "name": name,
                "display_name": display_name,
                "description": description,
            }

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(service_accounts, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(service_accounts, "get_client", fake_get_client)
    monkeypatch.setattr(service_accounts, "print_json_table", lambda *args: None)

    await service_accounts.create(
        project_id="project-1",
        name="worker",
        display_name="Worker",
        description="background jobs",
        metadata='{"tier":"batch"}',
        annotations='{"owner":"ops"}',
        o="table",
    )

    assert calls == [
        {
            "project_id": "resolved-project",
            "name": "worker",
            "display_name": "Worker",
            "description": "background jobs",
            "metadata": {"tier": "batch"},
            "annotations": {"owner": "ops"},
        }
    ]


@pytest.mark.asyncio
async def test_create_api_key_resolves_service_account_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def list_service_accounts(self, **kwargs):
            calls.append({"method": "list_service_accounts", **kwargs})
            return {"service_accounts": [{"id": "service-account-1", "name": "worker"}]}

        async def create_api_key(self, **kwargs):
            calls.append({"method": "create_api_key", **kwargs})
            return {"id": "key-1", "name": kwargs["name"], "value": "ma-token"}

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(service_accounts, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(service_accounts, "get_client", fake_get_client)

    await service_accounts.create_api_key(
        project_id="project-1",
        name="deploy",
        service_account="worker",
        description="deploy key",
        activate=False,
        silent=True,
    )

    assert calls == [
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "worker",
        },
        {
            "method": "create_api_key",
            "project_id": "resolved-project",
            "name": "deploy",
            "description": "deploy key",
            "service_account_id": "service-account-1",
        },
    ]
