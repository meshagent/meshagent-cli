import asyncio
from types import SimpleNamespace

from meshagent.api.client import ProjectInfo, ProjectsPage
from meshagent.cli import root_commands
from meshagent.cli.local_settings import StoredUserProfile
from meshagent.cli.tui.setup import (
    SetupClaudeConfiguration,
    SetupProject,
    SetupWizardResult,
)


def test_version_command_prints_client_and_server_versions(
    monkeypatch,
    capsys,
) -> None:
    async def _fake_get_server_version_best_effort() -> str | None:
        return "0.42.0"

    monkeypatch.setattr(
        root_commands,
        "get_server_version_best_effort",
        _fake_get_server_version_best_effort,
    )
    monkeypatch.setattr(root_commands, "__version__", "0.41.5")

    callback = root_commands.version_command.callback
    assert callback is not None
    callback()

    captured = capsys.readouterr()
    assert captured.out == "client: 0.41.5\nserver: 0.42.0\n"
    assert captured.err == ""


def test_version_command_prints_unavailable_server_version(
    monkeypatch,
    capsys,
) -> None:
    async def _fake_get_server_version_best_effort() -> str | None:
        return None

    monkeypatch.setattr(
        root_commands,
        "get_server_version_best_effort",
        _fake_get_server_version_best_effort,
    )
    monkeypatch.setattr(root_commands, "__version__", "0.41.5")

    callback = root_commands.version_command.callback
    assert callback is not None
    callback()

    captured = capsys.readouterr()
    assert captured.out == "client: 0.41.5\nserver: unavailable\n"
    assert captured.err == ""


def test_setup_command_launches_ask_after_success(monkeypatch) -> None:
    launched: list[dict[str, object]] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["active_project_id"] is None
        assert kwargs["has_authenticated_session"] is True
        assert kwargs["authenticated_user_name"] == "Jesse Ezell (jesse@example.com)"
        assert kwargs["has_codex_cli"] is True
        assert kwargs["has_claude_code_cli"] is True
        assert kwargs["default_codex_profile_name"] == "meshagent"
        assert callable(kwargs["list_existing_codex_profiles_operation"])
        assert callable(kwargs["remove_codex_profile_operation"])
        assert callable(kwargs["configure_codex_default_profile_operation"])
        assert callable(kwargs["configure_claude_operation"])
        assert callable(kwargs["inspect_claude_configuration_operation"])
        assert callable(kwargs["clear_claude_operation"])
        assert await kwargs["list_existing_codex_profiles_operation"]() == ["meshagent"]
        return SetupWizardResult(
            status="completed",
            message="done",
            project_id="project-123",
        )

    class _FakeClient:
        async def get_user_profile(self, user_id: str):
            assert user_id == "me"
            return {
                "id": "user-123",
                "first_name": "Jesse",
                "last_name": "Ezell",
                "email": "jesse@example.com",
            }

        async def close(self) -> None:
            return None

    async def _fake_ask(*, project_id, message, model="gpt-5.5") -> None:
        launched.append(
            {
                "project_id": project_id,
                "message": message,
                "model": model,
            }
        )

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.get_active_profile",
        lambda: StoredUserProfile(
            id="user-123",
            first_name="Jesse",
            last_name="Ezell",
            email="jesse@example.com",
        ),
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        root_commands,
        "_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.find_existing_codex_profiles",
        lambda *, config_path=None: ["meshagent"],
    )
    monkeypatch.setattr(
        "meshagent.cli.helper.CustomMeshagentClient",
        lambda *, base_url, token: _FakeClient(),
    )
    monkeypatch.setattr(
        "meshagent.cli.ask.ask",
        _fake_ask,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert launched == [
        {
            "project_id": None,
            "message": root_commands._setup_welcome_prompt(user_name="Jesse Ezell"),
            "model": "gpt-5.5",
        }
    ]


def test_setup_command_launches_create_after_sample_selection(monkeypatch) -> None:
    created_args: list[list[str]] = []
    asked: list[str] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        del kwargs
        return SetupWizardResult(
            status="completed", project_id="project-123", create_sample=True
        )

    class _FakeClient:
        async def get_user_profile(self, user_id: str):
            assert user_id == "me"
            return {
                "id": "user-123",
                "first_name": "Jesse",
                "last_name": "Ezell",
                "email": "jesse@example.com",
            }

        async def close(self) -> None:
            return None

    async def _fake_ask(*, project_id, message, model="gpt-5.5") -> None:
        del project_id, model
        asked.append(message)

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project", _fake_get_active_project
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui", _fake_run_setup_wizard_tui
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr("meshagent.cli.tool_integrations.has_codex_cli", lambda: False)
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli", lambda: False
    )
    monkeypatch.setattr(
        "meshagent.cli.helper.CustomMeshagentClient",
        lambda *, base_url, token: _FakeClient(),
    )
    monkeypatch.setattr("meshagent.cli.ask.ask", _fake_ask)
    monkeypatch.setattr(root_commands, "_run_async", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(
        "meshagent.cli.create.create_command.main",
        lambda *, args, standalone_mode: created_args.append(list(args)),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert len(asked) == 1
    assert created_args == [["--interactive"]]


def test_setup_command_passes_current_cli_path_to_claude_configuration(
    monkeypatch,
) -> None:
    captured: dict[str, str | None] = {}

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is False
        assert kwargs["has_claude_code_cli"] is True
        assert kwargs["list_existing_codex_profiles_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["configure_codex_default_profile_operation"] is None
        assert callable(kwargs["configure_claude_operation"])
        assert callable(kwargs["inspect_claude_configuration_operation"])
        assert callable(kwargs["clear_claude_operation"])
        await kwargs["configure_claude_operation"]("project-123")
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_configure_claude_code_integration(
        *,
        project_id: str,
        api_url: str | None = None,
        meshagent_executable: str | None = None,
        **kwargs,
    ) -> None:
        captured["project_id"] = project_id
        captured["api_url"] = api_url
        captured["meshagent_executable"] = meshagent_executable
        assert kwargs == {}

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.configure_claude_code_integration",
        _fake_configure_claude_code_integration,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        root_commands,
        "_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert captured == {
        "project_id": "project-123",
        "api_url": "https://api.meshagent.com",
        "meshagent_executable": "/tmp/current/bin/meshagent",
    }


def test_setup_command_passes_codex_default_profile_operations(
    monkeypatch,
) -> None:
    configured_defaults: list[dict[str, str | None]] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is True
        assert kwargs["has_claude_code_cli"] is False
        assert callable(kwargs["remove_codex_profile_operation"])
        assert callable(kwargs["configure_codex_default_profile_operation"])
        await kwargs["configure_codex_default_profile_operation"](
            "project-123",
            "meshagent",
        )
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_configure_codex_default_integration(
        *,
        project_id: str | None = None,
        provider_id: str,
        api_url: str | None = None,
        meshagent_executable: str | None = None,
        config_path=None,
        **kwargs,
    ) -> None:
        configured_defaults.append(
            {
                "project_id": project_id,
                "provider_id": provider_id,
                "api_url": api_url,
                "meshagent_executable": meshagent_executable,
                "config_path": None if config_path is None else str(config_path),
            }
        )
        assert kwargs == {}

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.configure_codex_default_integration",
        _fake_configure_codex_default_integration,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        root_commands,
        "_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert configured_defaults == [
        {
            "project_id": "project-123",
            "provider_id": "meshagent",
            "api_url": "https://api.meshagent.com",
            "meshagent_executable": "/tmp/current/bin/meshagent",
            "config_path": None,
        }
    ]


def test_setup_command_does_not_launch_ask_when_not_completed(monkeypatch) -> None:
    launched = False

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_authenticated_session"] is False
        assert kwargs["authenticated_user_name"] is None
        assert kwargs["has_codex_cli"] is False
        assert kwargs["has_claude_code_cli"] is False
        assert kwargs["list_existing_codex_profiles_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["configure_codex_default_profile_operation"] is None
        assert kwargs["configure_claude_operation"] is None
        assert kwargs["inspect_claude_configuration_operation"] is None
        assert kwargs["clear_claude_operation"] is None
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_ask(*, project_id, message, model="gpt-5.5") -> None:
        del project_id, message, model
        nonlocal launched
        launched = True

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.ask.ask",
        _fake_ask,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert launched is False


def test_setup_command_passes_codex_remove_operation(
    monkeypatch,
) -> None:
    removed: list[str] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is True
        assert callable(kwargs["remove_codex_profile_operation"])
        await kwargs["remove_codex_profile_operation"]("meshagent-work")
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_remove_codex_integration(*, profile_id: str, **kwargs) -> bool:
        removed.append(profile_id)
        assert kwargs == {}
        return True

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.remove_codex_integration",
        _fake_remove_codex_integration,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        root_commands,
        "_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert removed == ["meshagent-work"]


def test_setup_command_passes_claude_inspect_and_clear_operations(
    monkeypatch,
) -> None:
    cleared = False

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is False
        assert kwargs["has_claude_code_cli"] is True
        assert callable(kwargs["inspect_claude_configuration_operation"])
        assert callable(kwargs["clear_claude_operation"])
        assert await kwargs["inspect_claude_configuration_operation"]() == (
            SetupClaudeConfiguration(
                configured=True,
                project_id="project-old",
            )
        )
        await kwargs["clear_claude_operation"]()
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_inspect_claude_code_integration():
        return SimpleNamespace(configured=True, project_id="project-old")

    def _fake_clear_claude_code_integration() -> bool:
        nonlocal cleared
        cleared = True
        return True

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.inspect_claude_code_integration",
        _fake_inspect_claude_code_integration,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.clear_claude_code_integration",
        _fake_clear_claude_code_integration,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert cleared is True


def test_setup_command_passes_api_url_to_login_operation(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    async def _fake_login(
        *,
        status_handler=None,
        print_status: bool = True,
        api_url: str | None = None,
    ) -> None:
        del status_handler, print_status
        captured["api_url"] = api_url

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_authenticated_session"] is False
        assert kwargs["authenticated_user_name"] is None
        assert kwargs["has_codex_cli"] is False
        assert kwargs["has_claude_code_cli"] is False
        assert kwargs["list_existing_codex_profiles_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["configure_codex_default_profile_operation"] is None
        assert kwargs["configure_claude_operation"] is None
        assert kwargs["inspect_claude_configuration_operation"] is None
        assert kwargs["clear_claude_operation"] is None
        await kwargs["login_operation"](lambda _message: None)
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.login",
        _fake_login,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback(api_url="https://override.meshagent.test")

    assert captured == {"api_url": "https://override.meshagent.test"}


def test_setup_command_project_operations_use_setup_api_url(monkeypatch) -> None:
    client_api_urls: list[str | None] = []
    active_project_ids: list[str | None] = []

    class _FakeClient:
        async def list_projects(self):
            return ProjectsPage(
                projects=[
                    ProjectInfo.model_validate(
                        {
                            "id": "project-1",
                            "name": "Project One",
                        }
                    )
                ]
            )

        async def create_project(self, project_name: str):
            assert project_name == "New Project"
            return ProjectInfo.model_validate(
                {
                    "id": "project-created",
                    "name": project_name,
                }
            )

        async def can_use_llm_proxy(self, project_id: str) -> bool:
            assert project_id == "project-1"
            return True

        async def close(self) -> None:
            return None

    async def _fake_get_client(*, api_url=None):
        client_api_urls.append(api_url)
        return _FakeClient()

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_set_active_project(project_id: str | None) -> None:
        active_project_ids.append(project_id)

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert await kwargs["list_projects_operation"]() == [
            SetupProject(id="project-1", name="Project One")
        ]
        assert (
            await kwargs["create_project_operation"]("New Project") == "project-created"
        )
        assert await kwargs["activate_project_operation"]("project-1") == "project-1"
        assert await kwargs["has_llm_proxy_access_operation"]("project-1") is True
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_resolve_api_url(*, api_url=None):
        if api_url is None:
            return "https://ambient.meshagent.test"
        return api_url.rstrip("/")

    monkeypatch.setattr(
        "meshagent.cli.helper.get_client",
        _fake_get_client,
    )
    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.helper.set_active_project",
        _fake_set_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        _fake_resolve_api_url,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.get_active_api_url",
        lambda: "https://override.meshagent.test",
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback(api_url="https://override.meshagent.test/")

    assert client_api_urls == [
        "https://override.meshagent.test",
        "https://override.meshagent.test",
        "https://override.meshagent.test",
    ]
    assert active_project_ids == ["project-1"]


def test_setup_command_skips_current_account_for_unmatched_override_api_url(
    monkeypatch,
) -> None:
    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_authenticated_session"] is False
        assert kwargs["authenticated_user_name"] is None
        assert kwargs["has_codex_cli"] is False
        assert kwargs["has_claude_code_cli"] is False
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.get_active_api_url",
        lambda: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.get_active_profile",
        lambda: StoredUserProfile(
            id="user-123",
            first_name="Jesse",
            last_name="Ezell",
            email="jesse@example.com",
        ),
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: False,
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback(api_url="https://override.meshagent.test")
