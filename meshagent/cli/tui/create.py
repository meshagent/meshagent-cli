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

CREATE_BACK_OPTION_ID = "__init_back__"
CREATE_CANCEL_OPTION_ID = "__init_cancel__"
CREATE_EXISTING_DOCTOR_OPTION_ID = "__init_existing_doctor__"
CREATE_EXISTING_SUBFOLDER_OPTION_ID = "__init_existing_subfolder__"
CREATE_LANGUAGE_OPTION_ID_PREFIX = "__init_language__:"
CREATE_FOCUS_OPTION_ID_PREFIX = "__init_focus__:"

EXISTING_ACTION_DOCTOR = "run-doctor"
EXISTING_ACTION_SUBFOLDER = "create-subfolder"


def _language_option_id(language_id: str) -> str:
    return f"{CREATE_LANGUAGE_OPTION_ID_PREFIX}{language_id}"


def _language_id_from_option_id(option_id: str) -> str | None:
    if not option_id.startswith(CREATE_LANGUAGE_OPTION_ID_PREFIX):
        return None
    return option_id.removeprefix(CREATE_LANGUAGE_OPTION_ID_PREFIX)


def _focus_option_id(focus_id: str) -> str:
    return f"{CREATE_FOCUS_OPTION_ID_PREFIX}{focus_id}"


def _focus_id_from_option_id(option_id: str) -> str | None:
    if not option_id.startswith(CREATE_FOCUS_OPTION_ID_PREFIX):
        return None
    return option_id.removeprefix(CREATE_FOCUS_OPTION_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class CreateLanguageChoice:
    id: str
    label: str
    description: str
    focus_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreateFocusChoice:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class CreateWizardResult:
    status: Literal["completed", "canceled"]
    message: str | None = None
    selected_language_id: str | None = None
    selected_focus_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateExistingProjectResult:
    status: Literal["completed", "canceled"]
    message: str | None = None
    action: Literal["run-doctor", "create-subfolder"] | None = None
    subfolder_name: str | None = None


def _validate_subfolder_name(value: str) -> str:
    resolved_value = value.strip()
    if resolved_value == "":
        raise ValueError("Folder name cannot be empty.")
    if resolved_value in {".", ".."} or "/" in resolved_value or "\\" in resolved_value:
        raise ValueError("Enter a folder name, not a path.")
    return resolved_value


class CreateExistingProjectApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #init-existing-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #init-existing-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #init-existing-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #init-existing-input {
        width: 100%;
        margin: 1 0 0 0;
    }
    #init-existing-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #init-existing-error {
        width: 100%;
        content-align: left middle;
        color: #ff7b72;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_init", "Cancel", priority=True),
        Binding("escape", "cancel_or_back", "Cancel", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._mode: Literal["choice", "folder_name"] = "choice"
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._input_view: Input | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None
        self.result = CreateExistingProjectResult(
            status="canceled",
            message="Create canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="init-existing-title")
        yield Static("", id="init-existing-message")
        yield OptionList(id="init-existing-options")
        yield Input(id="init-existing-input", placeholder="")
        yield Static("", id="init-existing-help")
        yield Static("", id="init-existing-error")

    async def on_mount(self) -> None:
        self._title_view = self.query_one("#init-existing-title", Static)
        self._message_view = self.query_one("#init-existing-message", Static)
        self._options_view = self.query_one("#init-existing-options", OptionList)
        self._input_view = self.query_one("#init-existing-input", Input)
        self._help_view = self.query_one("#init-existing-help", Static)
        self._error_view = self.query_one("#init-existing-error", Static)
        self._hide_input()
        self._clear_error()
        self._show_existing_project_choice()

    async def action_cancel_init(self) -> None:
        self.result = CreateExistingProjectResult(
            status="canceled",
            message="Create canceled.",
        )
        self.exit()

    async def action_cancel_or_back(self) -> None:
        if self._mode == "folder_name":
            self._show_existing_project_choice()
            return
        await self.action_cancel_init()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if selected_id == CREATE_EXISTING_DOCTOR_OPTION_ID:
            self.result = CreateExistingProjectResult(
                status="completed",
                action=EXISTING_ACTION_DOCTOR,
            )
            self.exit()
            return

        if selected_id == CREATE_EXISTING_SUBFOLDER_OPTION_ID:
            self._show_subfolder_prompt()
            return

        if selected_id == CREATE_CANCEL_OPTION_ID:
            self.result = CreateExistingProjectResult(
                status="canceled",
                message="Create canceled.",
            )
            self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._mode != "folder_name":
            return
        self._submit_subfolder_name(event.value)

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

    def _set_options(self, options: Sequence[Option]) -> None:
        if self._options_view is None:
            return
        option_list = list(options)
        self._options_view.clear_options()
        self._options_view.add_options(option_list)
        self._options_view.highlighted = self._first_enabled_option_index(option_list)
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

    def _show_existing_project_choice(self) -> None:
        self._mode = "choice"
        self._clear_error()
        self._hide_input()
        self._set_text(
            title="MeshAgent Create",
            message="This directory already contains project files.",
            help_text="Choose an option. Esc or Ctrl+C cancels.",
        )
        self._set_options(
            [
                Option(
                    "Run meshagent doctor here.",
                    id=CREATE_EXISTING_DOCTOR_OPTION_ID,
                ),
                Option(
                    "Create a new project in a new subfolder.",
                    id=CREATE_EXISTING_SUBFOLDER_OPTION_ID,
                ),
                Option("Cancel", id=CREATE_CANCEL_OPTION_ID),
            ]
        )

    def _show_subfolder_prompt(self) -> None:
        self._mode = "folder_name"
        self._clear_error()
        self._hide_options()
        self._set_text(
            title="MeshAgent Create",
            message="Enter a folder name for the new project.",
            help_text="Type a folder name and press Enter. Esc goes back.",
        )
        self._show_input(placeholder="Folder name")

    def _submit_subfolder_name(self, subfolder_name: str) -> bool:
        try:
            resolved_subfolder_name = _validate_subfolder_name(subfolder_name)
        except ValueError as error:
            self._set_error_text(str(error))
            return False

        self.result = CreateExistingProjectResult(
            status="completed",
            action=EXISTING_ACTION_SUBFOLDER,
            subfolder_name=resolved_subfolder_name,
        )
        self.exit()
        return True


class CreateWizardApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #init-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #init-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #init-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #init-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_init", "Cancel", priority=True),
        Binding("escape", "cancel_or_back", "Cancel", priority=True),
    ]

    def __init__(
        self,
        *,
        languages: Sequence[CreateLanguageChoice],
        focuses: Sequence[CreateFocusChoice],
    ) -> None:
        super().__init__()
        self._languages = list(languages)
        self._focuses = list(focuses)
        self._mode: Literal["language", "focus"] = "language"
        self._selected_language_id: str | None = None
        self._selected_language_label: str | None = None
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._help_view: Static | None = None
        self.result = CreateWizardResult(
            status="canceled",
            message="Create canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="init-title")
        yield Static("", id="init-message")
        yield OptionList(id="init-options")
        yield Static("", id="init-help")

    async def on_mount(self) -> None:
        self._title_view = self.query_one("#init-title", Static)
        self._message_view = self.query_one("#init-message", Static)
        self._options_view = self.query_one("#init-options", OptionList)
        self._help_view = self.query_one("#init-help", Static)
        self._show_language_selection()

    async def action_cancel_init(self) -> None:
        self.result = CreateWizardResult(status="canceled", message="Create canceled.")
        self.exit()

    async def action_cancel_or_back(self) -> None:
        if self._mode == "focus":
            self._show_language_selection()
            return
        await self.action_cancel_init()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if selected_id == CREATE_CANCEL_OPTION_ID:
            self.result = CreateWizardResult(
                status="canceled", message="Create canceled."
            )
            self.exit()
            return

        if selected_id == CREATE_BACK_OPTION_ID:
            self._show_language_selection()
            return

        language_id = _language_id_from_option_id(selected_id)
        if language_id is not None:
            self._selected_language_id = language_id
            self._selected_language_label = self._language_label(language_id)
            self._show_focus_selection()
            return

        focus_id = _focus_id_from_option_id(selected_id)
        if focus_id is None or self._selected_language_id is None:
            return

        self.result = CreateWizardResult(
            status="completed",
            selected_language_id=self._selected_language_id,
            selected_focus_id=focus_id,
        )
        self.exit()

    @staticmethod
    def _first_enabled_option_index(options: Sequence[Option]) -> int | None:
        for index, option in enumerate(options):
            if not option.disabled:
                return index
        return None

    def _language_label(self, language_id: str) -> str:
        for language in self._languages:
            if language.id == language_id:
                return language.label
        return language_id

    def _language_choice(self, language_id: str) -> CreateLanguageChoice | None:
        for language in self._languages:
            if language.id == language_id:
                return language
        return None

    def _focuses_for_selected_language(self) -> list[CreateFocusChoice]:
        if self._selected_language_id is None:
            return list(self._focuses)

        language = self._language_choice(self._selected_language_id)
        if language is None or not language.focus_ids:
            return list(self._focuses)

        allowed_focus_ids = set(language.focus_ids)
        return [focus for focus in self._focuses if focus.id in allowed_focus_ids]

    def _set_text(self, *, title: str, message: str, help_text: str) -> None:
        if self._title_view is not None:
            self._title_view.update(title)
        if self._message_view is not None:
            self._message_view.update(message)
        if self._help_view is not None:
            self._help_view.update(help_text)

    def _set_options(self, options: Sequence[Option]) -> None:
        if self._options_view is None:
            return
        option_list = list(options)
        self._options_view.clear_options()
        self._options_view.add_options(option_list)
        self._options_view.highlighted = self._first_enabled_option_index(option_list)
        self._options_view.focus()

    def _show_language_selection(self) -> None:
        self._mode = "language"
        self._selected_language_id = None
        self._selected_language_label = None
        self._set_text(
            title="MeshAgent Create",
            message="Choose the language for the project.",
            help_text="Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        )
        options = [
            Option(
                language.label,
                id=_language_option_id(language.id),
            )
            for language in self._languages
        ]
        options.append(Option("Cancel", id=CREATE_CANCEL_OPTION_ID))
        self._set_options(options)

    def _show_focus_selection(self) -> None:
        self._mode = "focus"
        language_label = self._selected_language_label or "the selected language"
        self._set_text(
            title="MeshAgent Create",
            message=f"Choose what you want to build for {language_label}.",
            help_text=(
                "Web server creates an HTTP app. Backend agent creates a "
                "RoomClient SDK service. Esc goes back."
            ),
        )
        options = [
            Option(
                f"{focus.label} - {focus.description}",
                id=_focus_option_id(focus.id),
            )
            for focus in self._focuses_for_selected_language()
        ]
        options.append(Option("Back", id=CREATE_BACK_OPTION_ID))
        options.append(Option("Cancel", id=CREATE_CANCEL_OPTION_ID))
        self._set_options(options)


async def _run_app(app: App[None]) -> None:
    app_token = active_app.set(app)
    try:
        await app.run_async()
    finally:
        active_app.reset(app_token)


async def run_create_wizard_tui(
    *,
    languages: Sequence[CreateLanguageChoice],
    focuses: Sequence[CreateFocusChoice],
) -> CreateWizardResult:
    app = CreateWizardApp(languages=languages, focuses=focuses)

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return CreateWizardResult(status="canceled", message="Create canceled.")

    return app.result


async def run_existing_project_create_tui() -> CreateExistingProjectResult:
    app = CreateExistingProjectApp()

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return CreateExistingProjectResult(
            status="canceled", message="Create canceled."
        )

    return app.result
