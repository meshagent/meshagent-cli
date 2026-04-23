import asyncio

from meshagent.cli import root_commands
from meshagent.cli.local_settings import StoredUserProfile
from meshagent.cli.tui.setup import SetupWizardResult


def test_setup_command_launches_ask_after_success(monkeypatch) -> None:
    launched: list[dict[str, object]] = []
    existing_codex_profile_requests: list[str] = []
    fake_profile = {
        "id": "user-123",
        "first_name": "Jesse",
        "last_name": "Ezell",
        "email": "jesse@example.com",
    }

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["active_project_id"] is None
        assert kwargs["has_authenticated_session"] is True
        assert kwargs["authenticated_user_name"] == "Jesse Ezell (jesse@example.com)"
        assert kwargs["has_codex_cli"] is True
        assert kwargs["default_codex_profile_name"] == "meshagent"
        assert callable(kwargs["list_existing_codex_profiles_operation"])
        assert callable(kwargs["configure_codex_profile_operation"])
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
            return fake_profile

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


def test_setup_command_does_not_launch_ask_when_not_completed(monkeypatch) -> None:
    launched = False

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["has_authenticated_session"] is False
        assert kwargs["authenticated_user_name"] is None
        assert kwargs["has_codex_cli"] is False
        assert kwargs["list_existing_codex_profiles_operation"] is None
        assert kwargs["configure_codex_profile_operation"] is None
        del kwargs
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
        assert kwargs["list_existing_codex_profiles_operation"] is None
        assert kwargs["configure_codex_profile_operation"] is None
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
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback(api_url="https://override.meshagent.test")

    assert captured == {"api_url": "https://override.meshagent.test"}
