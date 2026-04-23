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

    async def _has_llm_proxy_access_operation(project_id: str) -> bool:
        del project_id
        return True

    async def _list_existing_codex_profiles_operation(project_id: str) -> list[str]:
        del project_id
        return []

    async def _get_current_codex_default_profile_operation(
        project_id: str,
    ) -> str | None:
        del project_id
        return None

    async def _configure_codex_default_profile_operation(
        project_id: str,
        profile_id: str | None,
    ) -> None:
        del project_id, profile_id
        return None

    async def _configure_claude_operation(project_id: str) -> None:
        del project_id
        return None

    return SetupWizardApp(
        login_operation=_login_operation,
        list_projects_operation=_list_projects_operation,
        create_project_operation=_create_project_operation,
        activate_project_operation=_activate_project_operation,
        has_active_api_key_operation=_has_active_api_key_operation,
        create_api_key_operation=_create_api_key_operation,
        has_llm_proxy_access_operation=_has_llm_proxy_access_operation,
        list_existing_codex_profiles_operation=_list_existing_codex_profiles_operation,
        get_current_codex_default_profile_operation=(
            _get_current_codex_default_profile_operation
        ),
        configure_codex_default_profile_operation=(
            _configure_codex_default_profile_operation
        ),
        configure_claude_operation=_configure_claude_operation,
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
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
        "highlighted_id": None,
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_codex_choice()

    assert captured == {
        "title": "Codex Setup",
        "message": (
            "Codex was detected on this machine. Update Codex to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Yes, update Codex to use the MeshAgent proxy",
                "__codex_create__",
            ),
            (
                'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
                "__codex_skip__",
            ),
        ],
        "highlighted_id": None,
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_codex_choice()

    assert captured == {
        "title": "Codex Setup",
        "message": (
            "Codex was detected on this machine. MeshAgent proxy profiles "
            "centralize OpenAI and Anthropic billing, usage analytics, and "
            "governance in your MeshAgent account instead of managing separate "
            "provider subscriptions. Found existing MeshAgent Codex profiles "
            "for this project: meshagent, meshagent-work. Continue with them or "
            "create another profile."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Yes, update Codex to use the MeshAgent proxy",
                "__codex_continue__",
            ),
            ("Create another Codex profile", "__codex_create__"),
            (
                'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
                "__codex_skip__",
            ),
        ],
        "highlighted_id": None,
    }


def test_show_codex_default_choice_highlights_current_default(monkeypatch) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._current_codex_default_profile_id = "meshagent-work"
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_codex_default_choice(profile_ids=["meshagent", "meshagent-work"])

    assert captured == {
        "title": "Codex Default",
        "message": (
            "Choose which MeshAgent Codex profile should be the default profile."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Make meshagent the default profile",
                "__codex_default_profile__:meshagent",
            ),
            (
                "Make meshagent-work the default profile",
                "__codex_default_profile__:meshagent-work",
            ),
            (
                'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
                "__codex_default_none__",
            ),
        ],
        "highlighted_id": "__codex_default_profile__:meshagent-work",
    }


def test_show_codex_choice_requires_llm_proxy_access(monkeypatch) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._can_use_llm_proxy = False
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_codex_choice()

    assert captured == {
        "title": "Codex Setup",
        "message": (
            "Codex was detected on this machine. The MeshAgent proxy lets your "
            "team centralize OpenAI and Anthropic billing, usage analytics, "
            "and governance in MeshAgent instead of managing separate provider "
            "subscriptions. Your MeshAgent account is not currently configured "
            "for LLM access for this project. Talk to your account "
            "administrator to turn it on, then run setup again."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [("Continue setup", "__codex_skip__")],
        "highlighted_id": None,
    }


def test_show_claude_choice_renders_options(monkeypatch) -> None:
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_claude_choice()

    assert captured == {
        "title": "Claude Setup",
        "message": (
            "Claude was detected on this machine. Update Claude to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Yes, update Claude to use the MeshAgent proxy",
                "__claude_configure__",
            ),
            (
                'No, I will use "meshagent launch claude" if I want to use Claude via MeshAgent.',
                "__claude_skip__",
            ),
        ],
        "highlighted_id": None,
    }


def test_show_claude_choice_requires_llm_proxy_access(monkeypatch) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    app._can_use_llm_proxy = False
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
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(str(option.prompt), option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_claude_choice()

    assert captured == {
        "title": "Claude Setup",
        "message": (
            "Claude was detected on this machine. The MeshAgent proxy lets "
            "your team centralize OpenAI and Anthropic billing, usage "
            "analytics, and governance in MeshAgent instead of managing "
            "separate provider subscriptions. Your MeshAgent account is not "
            "currently configured for LLM access for this project. Talk to "
            "your account administrator to turn it on, then run setup again."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [("Finish setup", "__claude_skip__")],
        "highlighted_id": None,
    }


def test_finish_success_reports_codex_default_and_claude_configuration(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    app._selected_project_id = "project-123"
    app._configured_codex_profile_id = "meshagent"
    app._configured_codex_default_profile_id = "meshagent"
    app._configured_claude = True
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
            "Codex default profile meshagent is selected. "
            "Claude is configured to use MeshAgent."
        ),
        "help_text": "",
        "centered": False,
        "exited": True,
    }
    assert app.result.status == "completed"
    assert app.result.project_id == "project-123"
