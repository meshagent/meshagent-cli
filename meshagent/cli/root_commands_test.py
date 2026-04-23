import asyncio
from types import SimpleNamespace

from meshagent.cli import root_commands
from meshagent.cli.local_settings import StoredUserProfile
from meshagent.cli.tui.setup import SetupClaudeConfiguration, SetupWizardResult


def test_setup_command_launches_ask_after_success(monkeypatch) -> None:
    launched: list[dict[str, object]] = []
    existing_codex_profile_requests: list[str] = []

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
        assert callable(kwargs["configure_codex_profile_operation"])
        assert callable(kwargs["replace_codex_profile_operation"])
        assert callable(kwargs["remove_codex_profile_operation"])
        assert callable(kwargs["get_current_codex_default_profile_operation"])
        assert callable(kwargs["configure_codex_default_profile_operation"])
        assert callable(kwargs["configure_claude_operation"])
        assert callable(kwargs["inspect_claude_configuration_operation"])
        assert callable(kwargs["clear_claude_operation"])
        assert await kwargs["list_existing_codex_profiles_operation"](
            "project-123"
        ) == ["meshagent"]
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

    async def _fake_ask(*, project_id, message, model="gpt-5.4") -> None:
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
        "meshagent.cli.tool_integrations.has_codex_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.has_claude_code_cli",
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.find_existing_codex_profiles",
        lambda *, project_id=None, api_url=None, config_path=None: (
            existing_codex_profile_requests.append(project_id or "") or ["meshagent"]
        ),
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
            "model": "gpt-5.4",
        }
    ]
    assert existing_codex_profile_requests == ["project-123"]


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
        assert kwargs["configure_codex_profile_operation"] is None
        assert kwargs["replace_codex_profile_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["get_current_codex_default_profile_operation"] is None
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
    current_default_requests: list[dict[str, str | None]] = []
    configured_defaults: list[dict[str, str | None]] = []
    cleared_defaults: list[dict[str, str | None]] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is True
        assert kwargs["has_claude_code_cli"] is False
        assert callable(kwargs["replace_codex_profile_operation"])
        assert callable(kwargs["remove_codex_profile_operation"])
        assert callable(kwargs["get_current_codex_default_profile_operation"])
        assert callable(kwargs["configure_codex_default_profile_operation"])
        assert (
            await kwargs["get_current_codex_default_profile_operation"]("project-123")
            == "meshagent"
        )
        await kwargs["configure_codex_default_profile_operation"](
            "project-123",
            "meshagent-work",
        )
        await kwargs["configure_codex_default_profile_operation"]("project-123", None)
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_find_current_codex_default_profile(
        *,
        project_id: str | None = None,
        api_url: str | None = None,
        config_path=None,
    ) -> str | None:
        current_default_requests.append(
            {
                "project_id": project_id,
                "api_url": api_url,
                "config_path": None if config_path is None else str(config_path),
            }
        )
        return "meshagent"

    def _fake_set_codex_default_profile(
        *,
        profile_id: str | None,
        config_path=None,
    ) -> bool:
        configured_defaults.append(
            {
                "profile_id": profile_id,
                "config_path": None if config_path is None else str(config_path),
            }
        )
        return True

    def _fake_clear_codex_default_profile_if_meshagent_project(
        *,
        project_id: str | None = None,
        api_url: str | None = None,
        config_path=None,
    ) -> bool:
        cleared_defaults.append(
            {
                "project_id": project_id,
                "api_url": api_url,
                "config_path": None if config_path is None else str(config_path),
            }
        )
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
        "meshagent.cli.tool_integrations.find_current_codex_default_profile",
        _fake_find_current_codex_default_profile,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.set_codex_default_profile",
        _fake_set_codex_default_profile,
    )
    monkeypatch.setattr(
        (
            "meshagent.cli.tool_integrations."
            "clear_codex_default_profile_if_meshagent_project"
        ),
        _fake_clear_codex_default_profile_if_meshagent_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert current_default_requests == [
        {
            "project_id": "project-123",
            "api_url": "https://api.meshagent.com",
            "config_path": None,
        }
    ]
    assert configured_defaults == [
        {
            "profile_id": "meshagent-work",
            "config_path": None,
        }
    ]
    assert cleared_defaults == [
        {
            "project_id": "project-123",
            "api_url": "https://api.meshagent.com",
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
        assert kwargs["configure_codex_profile_operation"] is None
        assert kwargs["replace_codex_profile_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["get_current_codex_default_profile_operation"] is None
        assert kwargs["configure_codex_default_profile_operation"] is None
        assert kwargs["configure_claude_operation"] is None
        assert kwargs["inspect_claude_configuration_operation"] is None
        assert kwargs["clear_claude_operation"] is None
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_ask(*, project_id, message, model="gpt-5.4") -> None:
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


def test_setup_command_passes_current_cli_path_to_codex_configuration(
    monkeypatch,
) -> None:
    captured: dict[str, str | None] = {}

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is True
        assert callable(kwargs["configure_codex_profile_operation"])
        await kwargs["configure_codex_profile_operation"]("meshagent-work")
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_configure_codex_integration(
        *,
        profile_id: str,
        api_url: str | None = None,
        meshagent_executable: str | None = None,
        **kwargs,
    ) -> None:
        captured["profile_id"] = profile_id
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
        lambda: True,
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.configure_codex_integration",
        _fake_configure_codex_integration,
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
        "profile_id": "meshagent-work",
        "api_url": "https://api.meshagent.com",
        "meshagent_executable": "/tmp/current/bin/meshagent",
    }


def test_setup_command_passes_codex_replace_and_remove_operations(
    monkeypatch,
) -> None:
    replaced: list[dict[str, str | None]] = []
    removed: list[str] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_codex_cli"] is True
        assert callable(kwargs["replace_codex_profile_operation"])
        assert callable(kwargs["remove_codex_profile_operation"])
        await kwargs["replace_codex_profile_operation"]("meshagent")
        await kwargs["remove_codex_profile_operation"]("meshagent-work")
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    def _fake_replace_codex_integration(
        *,
        profile_id: str,
        api_url: str | None = None,
        meshagent_executable: str | None = None,
        **kwargs,
    ) -> None:
        replaced.append(
            {
                "profile_id": profile_id,
                "api_url": api_url,
                "meshagent_executable": meshagent_executable,
            }
        )
        assert kwargs == {}

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
        "meshagent.cli.tool_integrations.replace_codex_integration",
        _fake_replace_codex_integration,
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

    assert replaced == [
        {
            "profile_id": "meshagent",
            "api_url": "https://api.meshagent.com",
            "meshagent_executable": "/tmp/current/bin/meshagent",
        }
    ]
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
        assert kwargs["configure_codex_profile_operation"] is None
        assert kwargs["replace_codex_profile_operation"] is None
        assert kwargs["remove_codex_profile_operation"] is None
        assert kwargs["get_current_codex_default_profile_operation"] is None
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
