import asyncio
from types import SimpleNamespace

from textual.widgets.option_list import Option

from meshagent.cli.tui.setup import (
    SetupClaudeConfiguration,
    SetupProject,
    SetupWizardApp,
)


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

    async def _has_llm_proxy_access_operation(project_id: str) -> bool:
        del project_id
        return True

    async def _list_existing_codex_profiles_operation() -> list[str]:
        return []

    async def _configure_codex_default_profile_operation(
        project_id: str,
        profile_id: str,
    ) -> None:
        del project_id, profile_id
        return None

    async def _remove_codex_profile_operation(profile_id: str) -> None:
        del profile_id
        return None

    async def _configure_claude_operation(project_id: str) -> None:
        del project_id
        return None

    async def _inspect_claude_configuration_operation() -> SetupClaudeConfiguration:
        return SetupClaudeConfiguration(configured=False)

    async def _clear_claude_operation() -> None:
        return None

    return SetupWizardApp(
        login_operation=_login_operation,
        list_projects_operation=_list_projects_operation,
        create_project_operation=_create_project_operation,
        activate_project_operation=_activate_project_operation,
        has_llm_proxy_access_operation=_has_llm_proxy_access_operation,
        list_existing_codex_profiles_operation=_list_existing_codex_profiles_operation,
        remove_codex_profile_operation=_remove_codex_profile_operation,
        configure_codex_default_profile_operation=(
            _configure_codex_default_profile_operation
        ),
        configure_claude_operation=_configure_claude_operation,
        inspect_claude_configuration_operation=(
            _inspect_claude_configuration_operation
        ),
        clear_claude_operation=_clear_claude_operation,
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


def test_show_project_selection_marks_active_project(monkeypatch) -> None:
    app = _new_setup_app()
    app._projects = [
        SetupProject(id="project-123", name="Project One"),
        SetupProject(id="project-456", name="Project Two"),
    ]
    app._active_project_id = "project-123"
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

    app._show_project_selection()

    assert captured == {
        "title": "Activate a Project",
        "message": "Choose a project to activate for CLI commands.",
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Project One (project-123) (active)", "project-123"),
            ("Project Two (project-456)", "project-456"),
            ("Create a new project", "__project_create__"),
            ("Exit setup", "__project_exit__"),
        ],
        "highlighted_id": None,
    }


def test_activate_project_continues_without_api_key_creation(monkeypatch) -> None:
    app = _new_setup_app()
    calls: list[tuple[str, str | None]] = []

    async def activate_project(project_id: str) -> str:
        calls.append(("activate_project", project_id))
        return "project-123"

    async def continue_setup() -> None:
        calls.append(("continue_setup", app._selected_project_id))

    monkeypatch.setattr(app, "_activate_project_operation", activate_project)
    monkeypatch.setattr(app, "_set_busy", lambda **kwargs: None)
    monkeypatch.setattr(app, "_maybe_continue_to_codex_setup", continue_setup)

    asyncio.run(app._activate_project("project-123"))

    assert app._selected_project_id == "project-123"
    assert calls == [
        ("activate_project", "project-123"),
        ("continue_setup", "project-123"),
    ]


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
            "Codex was detected on this machine. Configure Codex to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Yes, make MeshAgent the default for Codex",
                "__codex_create__",
            ),
            (
                'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
                "__codex_skip__",
            ),
        ],
        "highlighted_id": "__codex_create__",
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
            "Codex was detected on this machine. Configure Codex to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions. "
            "Found existing MeshAgent Codex configuration for this project. "
            "Update it for the current project, remove it, or leave Codex "
            "unchanged."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Update Codex MeshAgent configuration", "__codex_update__"),
            ("Remove Codex MeshAgent configuration", "__codex_remove__"),
            (
                'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
                "__codex_skip__",
            ),
        ],
        "highlighted_id": "__codex_update__",
    }


def test_show_codex_choice_omits_single_existing_configuration_name(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._existing_codex_profile_ids = ["meshagent"]
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

    assert captured["message"] == (
        "Codex was detected on this machine. Configure Codex to use the "
        "MeshAgent proxy so you can centralize OpenAI and Anthropic "
        "billing, usage analytics, and governance in your MeshAgent "
        "account instead of managing separate provider subscriptions. "
        "Found existing MeshAgent Codex configuration for this project. "
        "Update it for the current project, remove it, or leave Codex "
        "unchanged."
    )
    assert captured["options"] == [
        ("Update Codex MeshAgent configuration", "__codex_update__"),
        ("Remove Codex MeshAgent configuration", "__codex_remove__"),
        (
            'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
            "__codex_skip__",
        ),
    ]


def test_show_codex_choice_hides_single_custom_configuration_name(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._existing_codex_profile_ids = ["tula"]
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

    assert captured["message"] == (
        "Codex was detected on this machine. Configure Codex to use the "
        "MeshAgent proxy so you can centralize OpenAI and Anthropic "
        "billing, usage analytics, and governance in your MeshAgent "
        "account instead of managing separate provider subscriptions. "
        "Found existing MeshAgent Codex configuration for this project. "
        "Update it for the current project, remove it, or leave Codex "
        "unchanged."
    )
    assert captured["options"] == [
        ("Update Codex MeshAgent configuration", "__codex_update__"),
        ("Remove Codex MeshAgent configuration", "__codex_remove__"),
        (
            'No, I will use "meshagent launch codex" if I want to use Codex via MeshAgent.',
            "__codex_skip__",
        ),
    ]


def test_codex_create_choice_configures_default(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._mode = "codex_choice"
    configured_defaults: list[str] = []

    async def _configure_default(profile_id: str) -> None:
        configured_defaults.append(profile_id)

    monkeypatch.setattr(app, "_configure_codex_default_profile", _configure_default)

    event = SimpleNamespace(option=SimpleNamespace(id="__codex_create__"))
    asyncio.run(app.on_option_list_option_selected(event))

    assert configured_defaults == ["meshagent"]


def test_codex_update_choice_removes_existing_then_configures_default(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._mode = "codex_choice"
    app._existing_codex_profile_ids = ["meshagent", "meshagent-work"]
    removed_profiles: list[str] = []
    configured_defaults: list[str] = []

    async def _remove_profile(profile_id: str) -> None:
        removed_profiles.append(profile_id)

    async def _configure_default(profile_id: str) -> None:
        configured_defaults.append(profile_id)

    monkeypatch.setattr(app, "_remove_codex_profile_operation", _remove_profile)
    monkeypatch.setattr(app, "_configure_codex_default_profile", _configure_default)
    monkeypatch.setattr(app, "_set_busy", lambda **kwargs: None)

    event = SimpleNamespace(option=SimpleNamespace(id="__codex_update__"))
    asyncio.run(app.on_option_list_option_selected(event))

    assert removed_profiles == ["meshagent", "meshagent-work"]
    assert configured_defaults == ["meshagent"]


def test_codex_remove_choice_removes_existing_then_continues(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_codex_cli=True)
    app._mode = "codex_choice"
    app._existing_codex_profile_ids = ["meshagent"]
    removed_profiles: list[str] = []
    continued = False

    async def _remove_profile(profile_id: str) -> None:
        removed_profiles.append(profile_id)

    async def _continue_to_claude() -> None:
        nonlocal continued
        continued = True

    monkeypatch.setattr(app, "_remove_codex_profile_operation", _remove_profile)
    monkeypatch.setattr(app, "_maybe_continue_to_claude_setup", _continue_to_claude)
    monkeypatch.setattr(app, "_set_busy", lambda **kwargs: None)

    event = SimpleNamespace(option=SimpleNamespace(id="__codex_remove__"))
    asyncio.run(app.on_option_list_option_selected(event))

    assert removed_profiles == ["meshagent"]
    assert app._removed_codex_profile_ids == ["meshagent"]
    assert continued is True


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
            "Claude was detected on this machine. Configure Claude to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Yes, make MeshAgent the default for Claude",
                "__claude_configure__",
            ),
            (
                'No, I will use "meshagent launch claude" if I want to use Claude via MeshAgent.',
                "__claude_skip__",
            ),
        ],
        "highlighted_id": "__claude_configure__",
    }


def test_show_claude_choice_prefers_existing_configuration(monkeypatch) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    app._has_existing_claude_configuration = True
    app._existing_claude_project_id = "project-old"
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
            "Claude was detected on this machine. Configure Claude to use the "
            "MeshAgent proxy so you can centralize OpenAI and Anthropic "
            "billing, usage analytics, and governance in your MeshAgent "
            "account instead of managing separate provider subscriptions. "
            "Found an existing MeshAgent Claude configuration on this machine. "
            "It is currently configured for project project-old. Update it for "
            "the current project, remove it, or leave Claude unchanged."
        ),
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            (
                "Update Claude MeshAgent configuration",
                "__claude_configure__",
            ),
            (
                "Remove Claude MeshAgent configuration",
                "__claude_remove__",
            ),
            (
                'No, I will use "meshagent launch claude" if I want to use Claude via MeshAgent.',
                "__claude_skip__",
            ),
        ],
        "highlighted_id": "__claude_configure__",
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


def test_show_sample_choice_renders_create_option(monkeypatch) -> None:
    app = _new_setup_app()
    captured: dict[str, object] = {}

    monkeypatch.setattr(app, "_clear_error", lambda: None)
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
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda *, options, highlighted_id=None: captured.update(
            {
                "options": [(option.prompt, option.id) for option in options],
                "highlighted_id": highlighted_id,
            }
        ),
    )

    app._show_sample_choice()

    assert captured == {
        "title": "Create Sample App",
        "message": "Would you like to create a sample MeshAgent application now?",
        "help_text": "Use Up/Down and Enter.",
        "centered": False,
        "options": [
            ("Create a sample application", "__sample_create__"),
            ("Skip for now", "__sample_skip__"),
        ],
        "highlighted_id": "__sample_create__",
    }


def test_finish_success_reports_codex_default_and_claude_configuration(
    monkeypatch,
) -> None:
    app = _new_setup_app(has_claude_code_cli=True)
    app._selected_project_id = "project-123"
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
            "Project activated and setup finished. "
            "Codex is configured to use MeshAgent by default. "
            "Claude is configured to use MeshAgent by default."
        ),
        "help_text": "",
        "centered": False,
        "exited": True,
    }
    assert app.result.status == "completed"
    assert app.result.project_id == "project-123"
    assert app.result.create_sample is False


def test_finish_success_can_request_sample_creation(monkeypatch) -> None:
    app = _new_setup_app()
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
            {"message": message}
        ),
    )
    monkeypatch.setattr(app, "_run_logo_fade", _noop)
    monkeypatch.setattr(app, "exit", lambda: None)

    asyncio.run(app._finish_success(create_sample=True))

    assert "Opening the sample app wizard next." in captured["message"]
    assert app.result.create_sample is True
