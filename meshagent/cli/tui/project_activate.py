# ruff: noqa: E402

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Sequence


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None:
        return

    parsed_features = [
        value.strip() for value in raw_features.split(",") if value.strip() != ""
    ]
    if len(parsed_features) == 0:
        return

    filtered_features = [
        value for value in parsed_features if value.lower() not in ("debug", "devtools")
    ]
    if len(filtered_features) == len(parsed_features):
        return

    if len(filtered_features) == 0:
        os.environ.pop("TEXTUAL", None)
        return

    os.environ["TEXTUAL"] = ",".join(filtered_features)


_suppress_textual_debug_features()

from textual._context import active_app
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

PROJECT_CREATE_OPTION_ID = "__project_activate_create__"
PROJECT_EXIT_OPTION_ID = "__project_activate_exit__"
PROJECT_OPTION_ID_PREFIX = "__project_activate_project__:"


def _project_option_id(project_id: str) -> str:
    return f"{PROJECT_OPTION_ID_PREFIX}{project_id}"


def _project_id_from_option_id(option_id: str) -> str | None:
    if not option_id.startswith(PROJECT_OPTION_ID_PREFIX):
        return None

    return option_id.removeprefix(PROJECT_OPTION_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class ProjectActivateProject:
    id: str
    name: str
    is_active: bool


def _project_option_label(project: ProjectActivateProject) -> str:
    label = f"{project.name} ({project.id})"
    if project.is_active:
        return f"{label} (active)"
    return label


@dataclass(frozen=True, slots=True)
class ProjectActivateResult:
    status: Literal["completed", "canceled"]
    message: str | None = None
    selected_project_id: str | None = None
    new_project_name: str | None = None


class ProjectActivateApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #project-activate-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #project-activate-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #project-activate-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #project-activate-input {
        width: 100%;
        border: round #7ca9ff;
        background: #0a1120;
        color: #f4f7ff;
    }
    #project-activate-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #project-activate-error {
        width: 100%;
        color: #ffb4ab;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_activate", "Cancel", priority=True),
        Binding("escape", "cancel_activate", "Cancel", priority=True),
    ]

    def __init__(self, *, projects: Sequence[ProjectActivateProject]) -> None:
        super().__init__()
        self._projects = list(projects)
        self._mode: Literal["projects", "project_name"] = "projects"
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._input_view: Input | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None

        self.result = ProjectActivateResult(
            status="canceled",
            message="Project activation canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="project-activate-title")
        yield Static("", id="project-activate-message")
        yield OptionList(id="project-activate-options")
        yield Input(id="project-activate-input", placeholder="")
        yield Static("", id="project-activate-help")
        yield Static("", id="project-activate-error")

    async def on_mount(self) -> None:
        self._title_view = self.query_one("#project-activate-title", Static)
        self._message_view = self.query_one("#project-activate-message", Static)
        self._options_view = self.query_one("#project-activate-options", OptionList)
        self._input_view = self.query_one("#project-activate-input", Input)
        self._help_view = self.query_one("#project-activate-help", Static)
        self._error_view = self.query_one("#project-activate-error", Static)
        self._hide_input()
        self._clear_error()
        self._show_project_selection()

    async def action_cancel_activate(self) -> None:
        self.result = ProjectActivateResult(
            status="canceled",
            message="Project activation canceled.",
        )
        self.exit()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if selected_id == PROJECT_EXIT_OPTION_ID:
            self.result = ProjectActivateResult(
                status="canceled",
                message="Project activation canceled.",
            )
            self.exit()
            return

        if selected_id == PROJECT_CREATE_OPTION_ID:
            self._set_mode_project_name()
            return

        project_id = _project_id_from_option_id(selected_id)
        if project_id is None:
            return

        self.result = ProjectActivateResult(
            status="completed",
            selected_project_id=project_id,
        )
        self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._mode != "project_name":
            return

        self._submit_project_name(event.value)

    @staticmethod
    def _highlighted_project_id(
        projects: Sequence[ProjectActivateProject],
    ) -> str | None:
        for project in projects:
            if not project.is_active:
                return project.id

        if len(projects) == 0:
            return None

        return projects[0].id

    @staticmethod
    def _first_enabled_option_index(options: Sequence[Option]) -> int | None:
        for index, option in enumerate(options):
            if not option.disabled:
                return index
        return None

    def _set_text(self, *, title: str, message: str, help_text: str) -> None:
        if self._title_view is not None:
            self._title_view.update(title)
        if self._message_view is not None:
            self._message_view.update(message)
        if self._help_view is not None:
            self._help_view.update(help_text)

    def _set_options(
        self,
        *,
        options: Sequence[Option],
        highlighted_id: str | None = None,
    ) -> None:
        if self._options_view is None:
            return

        option_list = list(options)
        self._options_view.clear_options()
        self._options_view.add_options(option_list)
        highlighted_index = self._first_enabled_option_index(option_list)
        if highlighted_id is not None:
            for index, option in enumerate(option_list):
                if option.id == highlighted_id and not option.disabled:
                    highlighted_index = index
                    break

        self._options_view.highlighted = highlighted_index
        self._options_view.display = True
        self._options_view.focus()

    def _hide_options(self) -> None:
        if self._options_view is not None:
            self._options_view.display = False

    def _show_input(self, *, placeholder: str, value: str = "") -> None:
        if self._input_view is None:
            return

        self._input_view.value = value
        self._input_view.placeholder = placeholder
        self._input_view.display = True
        self._input_view.focus()

    def _hide_input(self) -> None:
        if self._input_view is not None:
            self._input_view.display = False

    def _set_error_text(self, message: str) -> None:
        if self._error_view is not None:
            self._error_view.display = True
            self._error_view.update(message)

    def _clear_error(self) -> None:
        if self._error_view is not None:
            self._error_view.display = False
            self._error_view.update("")

    def _show_project_selection(self) -> None:
        self._mode = "projects"
        self._clear_error()
        self._hide_input()

        options: list[Option] = []
        if len(self._projects) == 0:
            options.append(Option("No projects available yet.", disabled=True))
        else:
            options.extend(
                Option(
                    _project_option_label(project),
                    id=_project_option_id(project.id),
                )
                for project in self._projects
            )

        options.append(Option("Create a new project", id=PROJECT_CREATE_OPTION_ID))
        options.append(Option("Exit without activating", id=PROJECT_EXIT_OPTION_ID))

        message = "Choose a project to activate for CLI commands."
        if len(self._projects) == 0:
            message = "No projects found yet. Choose Create to continue."

        highlighted_project_id = self._highlighted_project_id(self._projects)
        highlighted_id = (
            _project_option_id(highlighted_project_id)
            if highlighted_project_id is not None
            else PROJECT_CREATE_OPTION_ID
        )

        self._set_text(
            title="Activate a Project",
            message=message,
            help_text="Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        )
        self._set_options(options=options, highlighted_id=highlighted_id)

    def _set_mode_project_name(self) -> None:
        self._mode = "project_name"
        self._clear_error()
        self._hide_options()
        self._set_text(
            title="Create a Project",
            message="Enter a name for the new project.",
            help_text="Type a project name and press Enter. Esc or Ctrl+C cancels.",
        )
        self._show_input(placeholder="Project name")

    def _submit_project_name(self, project_name: str) -> bool:
        resolved_project_name = project_name.strip()
        if resolved_project_name == "":
            self._set_error_text("Project name cannot be empty.")
            return False

        self.result = ProjectActivateResult(
            status="completed",
            new_project_name=resolved_project_name,
        )
        self.exit()
        return True


async def _run_app(app: App[None]) -> None:
    app_token = active_app.set(app)
    try:
        await app.run_async()
    finally:
        active_app.reset(app_token)


async def run_project_activate_tui(
    *,
    projects: Sequence[ProjectActivateProject],
) -> ProjectActivateResult:
    app = ProjectActivateApp(projects=projects)

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return ProjectActivateResult(
            status="canceled",
            message="Project activation canceled.",
        )

    return app.result
