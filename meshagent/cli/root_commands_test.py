import asyncio

from meshagent.cli import root_commands
from meshagent.cli.tui.setup import SetupWizardResult


def test_setup_command_launches_ask_after_success(monkeypatch) -> None:
    launched: list[dict[str, object]] = []
    integrations_called: list[tuple[str | None, str | None, str | None]] = []
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
        return SetupWizardResult(
            status="completed",
            message="done",
            project_id="project-123",
        )

    class _FakeClient:
        async def list_projects(self):
            return {"projects": [{"id": "project-123", "name": "Life"}]}

        async def create_project(self, project_name: str):
            return {"id": project_name}

        async def get_user_profile(self, user_id: str):
            assert user_id == "me"
            return fake_profile

        async def close(self) -> None:
            return None

    async def _fake_get_client():
        return _FakeClient()

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
        "meshagent.cli.helper.get_client",
        _fake_get_client,
    )
    monkeypatch.setattr(
        "meshagent.cli.auth_async.get_access_token",
        _fake_get_access_token,
    )
    monkeypatch.setattr(
        "meshagent.cli.local_settings.resolve_api_url",
        lambda *, api_url=None: "https://api.meshagent.com",
    )
    monkeypatch.setattr(
        "meshagent.cli.helper.CustomMeshagentClient",
        lambda *, base_url, token: _FakeClient(),
    )
    monkeypatch.setattr(
        "meshagent.cli.tool_integrations.maybe_configure_local_tool_integrations",
        lambda *, api_url=None, project_id=None, project_name=None: integrations_called.append(
            (api_url, project_id, project_name)
        ),
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
    assert integrations_called == [
        ("https://api.meshagent.com", "project-123", "Life")
    ]


def test_setup_command_does_not_launch_ask_when_not_completed(monkeypatch) -> None:
    launched = False
    integrations_called = False

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        del kwargs
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    async def _fake_get_access_token() -> str | None:
        raise AssertionError("oauth token should not be requested")

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
        "meshagent.cli.tool_integrations.maybe_configure_local_tool_integrations",
        lambda *, api_url=None, project_id=None, project_name=None: None,
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
    assert integrations_called is False


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

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
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
        "meshagent.cli.tool_integrations.maybe_configure_local_tool_integrations",
        lambda *, api_url=None, project_id=None, project_name=None: None,
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
