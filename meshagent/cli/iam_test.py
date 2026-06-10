import pytest

from meshagent.api.client import AccessResource, AccessSubject, RoleGrant
from meshagent.cli import iam
from meshagent.cli.testing import CliRunner


def test_iam_help_exposes_policy_grant_and_revoke_commands() -> None:
    result = CliRunner().invoke(iam.app, ["--help"])

    assert result.exit_code == 0
    assert "policy" in result.output
    assert "grant" in result.output
    assert "revoke" in result.output


@pytest.mark.asyncio
async def test_iam_grant_calls_generic_policy_api(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def grant_resource_policy(self, **kwargs) -> None:
            calls.append(kwargs)

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(iam, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(iam, "get_client", fake_get_client)

    await iam.grant(
        project_id="project-1",
        resource_type="project",
        resource_id="resolved-project",
        subject_type="service_account",
        subject_id="service-account-1",
        role=["developer", "list"],
        invite_redirect_url=None,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["project_id"] == "resolved-project"
    assert call["resource_type"] == "project"
    assert call["resource_id"] == "resolved-project"
    assert call["roles"] == ["developer", "list"]
    assert call["subject"] == AccessSubject(
        type="service_account",
        id="service-account-1",
    )


@pytest.mark.asyncio
async def test_iam_policy_prints_direct_grants(monkeypatch: pytest.MonkeyPatch):
    printed_tables: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []

    class _FakeClient:
        async def get_resource_policy(self, **kwargs):
            assert kwargs["project_id"] == "resolved-project"
            return [
                RoleGrant(
                    resource=AccessResource(
                        type="project",
                        id="resolved-project",
                        name="Project",
                    ),
                    subject=AccessSubject(
                        type="service_account",
                        id="service-account-1",
                        name="worker",
                    ),
                    direct_roles=["developer", "list"],
                )
            ]

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed_tables.append((records, cols))

    monkeypatch.setattr(iam, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(iam, "get_client", fake_get_client)
    monkeypatch.setattr(iam, "print_json_table", fake_print_json_table)

    await iam.policy(
        project_id="project-1",
        resource_type="project",
        resource_id="resolved-project",
        page_size=50,
        o="table",
    )

    assert printed_tables[0][0] == [
        {
            "resource_type": "project",
            "resource_id": "resolved-project",
            "resource_name": "Project",
            "subject_type": "service_account",
            "subject_id": "service-account-1",
            "subject_name": "worker",
            "subject_email": None,
            "roles": "developer, list",
        }
    ]
