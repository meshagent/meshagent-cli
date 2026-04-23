import asyncio

from textual.widgets.option_list import Option

from meshagent.cli.tui.setup import SetupWizardApp


def _new_setup_app(**kwargs) -> SetupWizardApp:
    async def _login_operation(status_handler):
        del status_handler
        return None

    async def _list_projects_operation():
        return []

    async def _create_project_operation(project_name: str) -> str:
        return project_name

    async def _activate_project_operation(project_id: str) -> str:
        return project_id

    async def _has_active_api_key_operation(project_id: str) -> bool:
        del project_id
        return True

    async def _create_api_key_operation(project_id: str, api_key_name: str) -> None:
        del project_id, api_key_name
        return None

    async def _list_existing_codex_profiles_operation(project_id: str) -> list[str]:
        del project_id
        return []

    return SetupWizardApp(
        login_operation=_login_operation,
        list_projects_operation=_list_projects_operation,
        create_project_operation=_create_project_operation,
        activate_project_operation=_activate_project_operation,
        has_active_api_key_operation=_has_active_api_key_operation,
        create_api_key_operation=_create_api_key_operation,
        list_existing_codex_profiles_operation=_list_existing_codex_profiles_operation,
        **kwargs,
    )


def test_first_enabled_option_index_returns_first_enabled() -> None:
    options = [
        Option("No projects available yet.", disabled=True),
        Option("Launch browser to sign in", id="launch"),
        Option("Exit setup", id="exit"),
    ]

    assert SetupWizardApp._first_enabled_option_index(options) == 1


def test_first_enabled_option_index_returns_none_when_all_disabled() -> None:
    options = [
        Option("Unavailable option 1", disabled=True),
        Option("Unavailable option 2", disabled=True),
    ]

    assert SetupWizardApp._first_enabled_option_index(options) is None


def test_show_account_choice_uses_authenticated_user_name(monkeypatch) -> None:
    app = _new_setup_app(
        has_authenticated_session=True,
        authenticated_user_name="Jesse Ezell (jesse@example.com)",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text, centered=False: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
                "centered": centered,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(app, "_hide_status", lambda: None)
    monkeypatch.setattr(app, "_hide_url", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options: captured.update(
            {"options": [(str(option.prompt), option.id) for option in options]}
        ),
    )

    app._show_account_choice()

    assert captured == {
        "title": "Use Current Account",
        "message": (
            "You're already signed in. Continue as Jesse Ezell "
            "(jesse@example.com) or switch accounts."
        ),
        "help_text": "Choose an option. Esc or Ctrl+C cancels.",
        "centered": True,
        "options": [
            (
                "Continue as Jesse Ezell (jesse@example.com)",
                "__account_continue__",
            ),
            ("Switch accounts", "__account_switch__"),
            ("Exit setup", "__account_exit__"),
        ],
    }


def test_show_codex_choice_renders_options(monkeypatch) -> None:
    app = _new_setup_app(has_codex_cli=True)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text, centered=False: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
                "centered": centered,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(app, "_hide_status", lambda: None)
    monkeypatch.setattr(app, "_hide_url", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options: captured.update(
            {"options": [(str(option.prompt), option.id) for option in options]}
        ),
    )

    app._show_codex_choice()

    assert captured == {
        "title": "Codex Setup",
        "message": (
            "Codex was detected on this machine. Add a profile so Codex can use "
            "your MeshAgent account?"
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Add Codex profile", "__codex_create__"),
            ("Skip for now", "__codex_skip__"),
        ],
    }


def test_show_codex_choice_prefers_existing_profiles(monkeypatch) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._existing_codex_profile_ids = ["meshagent", "meshagent-work"]
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text, centered=False: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
                "centered": centered,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(app, "_hide_status", lambda: None)
    monkeypatch.setattr(app, "_hide_url", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options: captured.update(
            {"options": [(str(option.prompt), option.id) for option in options]}
        ),
    )

    app._show_codex_choice()

    assert captured == {
        "title": "Codex Setup",
        "message": (
            "Codex was detected on this machine. Found existing MeshAgent Codex "
            "profiles for this project: meshagent, meshagent-work. Continue with "
            "them or create another profile."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Continue", "__codex_continue__"),
            ("Create another Codex profile", "__codex_create__"),
        ],
    }


def test_show_claude_code_choice_renders_options(monkeypatch) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text, centered=False: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
                "centered": centered,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(app, "_hide_status", lambda: None)
    monkeypatch.setattr(app, "_hide_url", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options: captured.update(
            {"options": [(str(option.prompt), option.id) for option in options]}
        ),
    )

    app._show_claude_code_choice()

    assert captured == {
        "title": "Claude Code",
        "message": (
            "Claude Code was detected on this machine. Launch Claude Code "
            "through MeshAgent for this project now?"
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Launch Claude Code", "__claude_code_launch__"),
            ("Skip for now", "__claude_code_skip__"),
        ],
    }


def test_finish_success_reports_claude_code_launch(monkeypatch) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    app._selected_project_id = "project-123"
    app._configured_codex_profile_id = "meshagent"
    app._launch_claude_code = True
    captured: dict[str, object] = {}

    async def _noop() -> None:
        return None

    monkeypatch.setattr(app, "_stop_logo_dissolve", _noop)
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_options", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(app, "_hide_status", lambda: None)
    monkeypatch.setattr(app, "_hide_url", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text, centered=False: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
                "centered": centered,
            }
        ),
    )
    monkeypatch.setattr(app, "_run_logo_fade", _noop)
    monkeypatch.setattr(app, "exit", lambda: captured.update({"exited": True}))

    asyncio.run(app._finish_success())

    assert captured == {
        "title": "Setup Complete",
        "message": (
            "Project activated and Codex profile meshagent created. "
            "Claude Code will launch next."
        ),
        "help_text": "",
        "centered": False,
        "exited": True,
    }
    assert app.result.status == "completed"
    assert app.result.project_id == "project-123"
    assert app.result.launch_claude_code is True
