from meshagent.cli.tui.project_activate import (
    PROJECT_CREATE_OPTION_ID,
    PROJECT_EXIT_OPTION_ID,
    ProjectActivateApp,
    ProjectActivateProject,
)
from textual.widgets.option_list import Option


def _project(
    *,
    project_id: str,
    name: str,
    is_active: bool,
) -> ProjectActivateProject:
    return ProjectActivateProject(
        id=project_id,
        name=name,
        is_active=is_active,
    )


def test_first_enabled_option_index_returns_first_enabled() -> None:
    options = [
        Option("Disabled", disabled=True),
        Option("Enabled", id="enabled"),
        Option("Exit", id="exit"),
    ]

    assert ProjectActivateApp._first_enabled_option_index(options) == 1


def test_highlighted_project_id_prefers_first_non_active() -> None:
    projects = [
        _project(project_id="project-1", name="Alpha", is_active=True),
        _project(project_id="project-2", name="Beta", is_active=False),
    ]

    assert ProjectActivateApp._highlighted_project_id(projects) == "project-2"


def test_show_project_selection_renders_projects(monkeypatch) -> None:
    app = ProjectActivateApp(
        projects=[
            _project(project_id="project-1", name="Alpha", is_active=True),
            _project(project_id="project-2", name="Beta", is_active=False),
        ]
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
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
        "help_text": "Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        "options": [
            (
                "Alpha (project-1) (active)",
                "__project_activate_project__:project-1",
            ),
            ("Beta (project-2)", "__project_activate_project__:project-2"),
            ("Create a new project", PROJECT_CREATE_OPTION_ID),
            ("Exit without activating", PROJECT_EXIT_OPTION_ID),
        ],
        "highlighted_id": "__project_activate_project__:project-2",
    }


def test_show_project_selection_renders_empty_state(monkeypatch) -> None:
    app = ProjectActivateApp(projects=[])
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
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
        "message": "No projects found yet. Choose Create to continue.",
        "help_text": "Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        "options": [
            ("No projects available yet.", None),
            ("Create a new project", PROJECT_CREATE_OPTION_ID),
            ("Exit without activating", PROJECT_EXIT_OPTION_ID),
        ],
        "highlighted_id": PROJECT_CREATE_OPTION_ID,
    }


def test_submit_project_name_accepts_non_empty_values(monkeypatch) -> None:
    app = ProjectActivateApp(projects=[])
    exited = False

    def _fake_exit() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "exit", _fake_exit)

    submitted = app._submit_project_name("  New Project  ")

    assert submitted is True
    assert exited is True
    assert app.result.status == "completed"
    assert app.result.new_project_name == "New Project"
    assert app.result.selected_project_id is None
