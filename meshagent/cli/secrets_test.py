import pytest

from meshagent.cli import cli, secrets
from meshagent.cli.testing import CliRunner


def test_root_help_exposes_secret_command() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "secret" in result.output


def test_root_cli_rejects_legacy_plural_secrets_alias() -> None:
    result = CliRunner().invoke(cli.app, ["secrets", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_secret_help_exposes_subject_based_commands() -> None:
    result = CliRunner().invoke(secrets.app, ["--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "search" in result.output
    assert "create" in result.output
    assert "access" in result.output
    assert "versions" in result.output
    assert "delete-version" in result.output
    assert "grants" in result.output
    assert "grant-proxy" in result.output
    assert "revoke-proxy" in result.output
    assert "pull-secrets" in result.output
    assert "add-pull-secret" in result.output
    assert "remove-pull-secret" in result.output
    assert "service-account" not in result.output
    assert "project" not in result.output
    assert "room" not in result.output
    assert "agent" not in result.output


def test_secret_command_rejects_removed_legacy_scopes() -> None:
    for legacy_scope in ("project", "room", "agent"):
        result = CliRunner().invoke(secrets.app, [legacy_scope, "--help"])

        assert result.exit_code != 0
        assert "No such command" in result.output


@pytest.mark.asyncio
async def test_create_user_secret_creates_initial_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def create_user_secret(self, **kwargs):
            calls.append({"method": "create_user_secret", **kwargs})
            return {
                "id": "secret-1",
                "name": kwargs["name"],
                "type": kwargs["type"],
                "http_only": kwargs["http_only"],
                "current_version_id": None,
            }

        async def create_user_secret_version(self, **kwargs):
            calls.append({"method": "create_user_secret_version", **kwargs})
            return {"id": "version-1", "secret_id": kwargs["secret_id"], "version": 1}

        async def get_user_secret(self, **kwargs):
            calls.append({"method": "get_user_secret", **kwargs})
            return {
                "id": kwargs["secret_id"],
                "name": "github",
                "type": "opaque",
                "http_only": True,
                "current_version_id": "version-1",
            }

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(secrets, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(secrets, "get_client", fake_get_client)
    monkeypatch.setattr(secrets, "print_json_table", lambda *args: None)

    await secrets.create_secret(
        project_id="project-1",
        subject="me",
        name="github",
        type="opaque",
        http_only=True,
        metadata='{"service":"github"}',
        annotations='{"meshagent.io/secret.service":"github"}',
        value="token-value",
        value_file=None,
        o="table",
    )

    assert calls == [
        {
            "method": "create_user_secret",
            "project_id": "resolved-project",
            "name": "github",
            "type": "opaque",
            "http_only": True,
            "metadata": {"service": "github"},
            "annotations": {"meshagent.io/secret.service": "github"},
        },
        {
            "method": "create_user_secret_version",
            "secret_id": "secret-1",
            "value": b"token-value",
        },
        {
            "method": "get_user_secret",
            "secret_id": "secret-1",
        },
    ]


@pytest.mark.asyncio
async def test_search_user_secret_dispatches_standard_annotation_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def search_user_secrets(self, **kwargs):
            calls.append({"method": "search_user_secrets", **kwargs})
            return {"secrets": []}

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(secrets, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(secrets, "get_client", fake_get_client)
    monkeypatch.setattr(secrets, "print_json_table", lambda *args: None)

    await secrets.search_secrets(
        project_id="project-1",
        subject="me",
        filter="token",
        name="github",
        type="oauth_credentials",
        http_only=True,
        metadata='{"service":"github"}',
        annotations='{"custom":"value"}',
        provider="github",
        service="git",
        account="alice",
        username="alice-login",
        email="alice@example.com",
        url="https://github.com",
        oauth_provider="github-oauth",
        oauth_scopes="repo user",
        page_size=25,
        o="table",
    )

    assert calls == [
        {
            "method": "search_user_secrets",
            "filter": "token",
            "name": "github",
            "type": "oauth_credentials",
            "http_only": True,
            "metadata": {"service": "github"},
            "annotations": {"custom": "value"},
            "provider": "github",
            "service": "git",
            "account": "alice",
            "username": "alice-login",
            "email": "alice@example.com",
            "url": "https://github.com",
            "oauth_provider": "github-oauth",
            "oauth_scopes": "repo user",
            "page_size": 25,
        }
    ]


@pytest.mark.asyncio
async def test_service_account_secret_create_and_pull_secret_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        async def list_service_accounts(self, project_id, **kwargs):
            calls.append(
                {
                    "method": "list_service_accounts",
                    "project_id": project_id,
                    **kwargs,
                }
            )
            return {
                "service_accounts": [
                    {
                        "id": "service-account-1",
                        "project_id": project_id,
                        "key": "puller",
                        "name": "puller",
                    }
                ]
            }

        async def create_service_account_secret(self, **kwargs):
            calls.append({"method": "create_service_account_secret", **kwargs})
            return {
                "id": "secret-1",
                "name": kwargs["name"],
                "type": kwargs["type"],
                "http_only": kwargs["http_only"],
                "current_version_id": None,
            }

        async def create_service_account_secret_version(self, **kwargs):
            calls.append({"method": "create_service_account_secret_version", **kwargs})
            return {"id": "version-1", "secret_id": kwargs["secret_id"], "version": 1}

        async def get_service_account_secret(self, **kwargs):
            calls.append({"method": "get_service_account_secret", **kwargs})
            return {
                "id": kwargs["secret_id"],
                "name": "pull",
                "type": "docker_auth",
                "http_only": True,
                "current_version_id": "version-1",
            }

        async def add_service_account_pull_secret(self, **kwargs):
            calls.append({"method": "add_service_account_pull_secret", **kwargs})

        async def grant_user_secret_proxy_access(self, **kwargs):
            calls.append({"method": "grant_user_secret_proxy_access", **kwargs})

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(secrets, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(secrets, "get_client", fake_get_client)
    monkeypatch.setattr(secrets, "print_json_table", lambda *args: None)

    await secrets.create_secret(
        project_id="project-1",
        subject="puller@service.project.example.test",
        name="pull",
        type="docker_auth",
        http_only=True,
        metadata=None,
        annotations=None,
        value='{"registry":"private.example.com"}',
        value_file=None,
        o="table",
    )
    await secrets.add_service_account_pull_secret(
        project_id="project-1",
        subject="puller@service.project.example.test",
        secret_id="secret-1",
    )
    await secrets.grant_user_secret_proxy_access(
        project_id="project-1",
        subject="puller@service.project.example.test",
        secret_id="user-secret-1",
    )

    assert calls == [
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "puller@service.project.example.test",
        },
        {
            "method": "create_service_account_secret",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "name": "pull",
            "type": "docker_auth",
            "http_only": True,
            "metadata": None,
            "annotations": None,
        },
        {
            "method": "create_service_account_secret_version",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "secret_id": "secret-1",
            "value": b'{"registry":"private.example.com"}',
        },
        {
            "method": "get_service_account_secret",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "secret_id": "secret-1",
        },
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "puller@service.project.example.test",
        },
        {
            "method": "add_service_account_pull_secret",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "secret_id": "secret-1",
        },
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "puller@service.project.example.test",
        },
        {
            "method": "grant_user_secret_proxy_access",
            "secret_id": "user-secret-1",
            "service_account_id": "service-account-1",
        },
    ]


@pytest.mark.asyncio
async def test_secret_version_access_and_delete_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    written_values: list[bytes] = []

    class _FakeClient:
        async def get_user_secret(self, **kwargs):
            calls.append({"method": "get_user_secret", **kwargs})
            return {"id": kwargs["secret_id"], "current_version_id": "version-1"}

        async def access_user_secret_version(self, **kwargs):
            calls.append({"method": "access_user_secret_version", **kwargs})
            return b"user-secret-value"

        async def delete_user_secret_version(self, **kwargs):
            calls.append({"method": "delete_user_secret_version", **kwargs})

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(secrets, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(secrets, "get_client", fake_get_client)
    monkeypatch.setattr(
        secrets,
        "_write_secret_value",
        lambda value, *, o: written_values.append(value),
    )

    await secrets.access_secret_version(
        project_id="project-1",
        subject="me",
        secret_id="secret-1",
        version_id=None,
        o="table",
    )
    await secrets.delete_secret_version(
        project_id="project-1",
        subject="me",
        secret_id="secret-1",
        version_id="version-1",
    )

    assert written_values == [b"user-secret-value"]
    assert calls == [
        {"method": "get_user_secret", "secret_id": "secret-1"},
        {
            "method": "access_user_secret_version",
            "secret_id": "secret-1",
            "version_id": "version-1",
        },
        {
            "method": "delete_user_secret_version",
            "secret_id": "secret-1",
            "version_id": "version-1",
        },
    ]


@pytest.mark.asyncio
async def test_service_account_secret_version_access_and_delete_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    written_values: list[bytes] = []

    class _FakeClient:
        async def list_service_accounts(self, project_id, **kwargs):
            calls.append(
                {
                    "method": "list_service_accounts",
                    "project_id": project_id,
                    **kwargs,
                }
            )
            return {
                "service_accounts": [
                    {
                        "id": "service-account-1",
                        "project_id": project_id,
                        "key": "builder",
                        "name": "builder",
                    }
                ]
            }

        async def access_service_account_secret_version(self, **kwargs):
            calls.append({"method": "access_service_account_secret_version", **kwargs})
            return b"service-secret-value"

        async def delete_service_account_secret_version(self, **kwargs):
            calls.append({"method": "delete_service_account_secret_version", **kwargs})

        async def close(self) -> None:
            pass

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    async def fake_get_client() -> _FakeClient:
        return _FakeClient()

    monkeypatch.setattr(secrets, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(secrets, "get_client", fake_get_client)
    monkeypatch.setattr(
        secrets,
        "_write_secret_value",
        lambda value, *, o: written_values.append(value),
    )

    await secrets.access_secret_version(
        project_id="project-1",
        subject="builder@service.project.example.test",
        secret_id="secret-1",
        version_id="version-1",
        o="table",
    )
    await secrets.delete_secret_version(
        project_id="project-1",
        subject="builder@service.project.example.test",
        secret_id="secret-1",
        version_id="version-1",
    )

    assert written_values == [b"service-secret-value"]
    assert calls == [
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "builder@service.project.example.test",
        },
        {
            "method": "access_service_account_secret_version",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "secret_id": "secret-1",
            "version_id": "version-1",
        },
        {
            "method": "list_service_accounts",
            "project_id": "resolved-project",
            "page_size": 100,
            "filter": "builder@service.project.example.test",
        },
        {
            "method": "delete_service_account_secret_version",
            "project_id": "resolved-project",
            "service_account_id": "service-account-1",
            "secret_id": "secret-1",
            "version_id": "version-1",
        },
    ]
