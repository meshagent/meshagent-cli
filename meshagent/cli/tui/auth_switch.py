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

from textual.app import App, ComposeResult
from textual._context import active_app
from textual.binding import Binding
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from meshagent.cli.local_settings import DEFAULT_API_URL, SavedProfileRecord

EXIT_OPTION_ID = "__auth_switch_exit__"
PROFILE_OPTION_ID_PREFIX = "__auth_switch_profile__:"


def _profile_option_id(user_id: str) -> str:
    return f"{PROFILE_OPTION_ID_PREFIX}{user_id}"


def _profile_user_id_from_option_id(option_id: str) -> str | None:
    if not option_id.startswith(PROFILE_OPTION_ID_PREFIX):
        return None

    return option_id.removeprefix(PROFILE_OPTION_ID_PREFIX)


def _profile_option_label(profile: SavedProfileRecord) -> str:
    api_url = profile.api_url or DEFAULT_API_URL
    label = f"{profile.profile.display_name()} ({profile.user_id}) @ {api_url}"
    if profile.is_active:
        return f"{label} (active)"
    return label


@dataclass(frozen=True, slots=True)
class AuthSwitchResult:
    status: Literal["completed", "canceled"]
    message: str | None = None
    selected_profile: SavedProfileRecord | None = None


class AuthSwitchApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #auth-switch-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #auth-switch-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #auth-switch-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #auth-switch-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_switch", "Cancel", priority=True),
        Binding("escape", "cancel_switch", "Cancel", priority=True),
    ]

    def __init__(self, *, saved_profiles: Sequence[SavedProfileRecord]) -> None:
        super().__init__()
        self._saved_profiles = list(saved_profiles)
        self._profiles_by_user_id = {
            profile.user_id: profile for profile in self._saved_profiles
        }
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._help_view: Static | None = None

        self.result = AuthSwitchResult(
            status="canceled",
            message="Profile switch canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="auth-switch-title")
        yield Static("", id="auth-switch-message")
        yield OptionList(id="auth-switch-options")
        yield Static("", id="auth-switch-help")

    async def on_mount(self) -> None:
        self._title_view = self.query_one("#auth-switch-title", Static)
        self._message_view = self.query_one("#auth-switch-message", Static)
        self._options_view = self.query_one("#auth-switch-options", OptionList)
        self._help_view = self.query_one("#auth-switch-help", Static)
        self._show_profile_selection()

    async def action_cancel_switch(self) -> None:
        self.result = AuthSwitchResult(
            status="canceled",
            message="Profile switch canceled.",
        )
        self.exit()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if selected_id == EXIT_OPTION_ID:
            self.result = AuthSwitchResult(
                status="canceled",
                message="Profile switch canceled.",
            )
            self.exit()
            return

        user_id = _profile_user_id_from_option_id(selected_id)
        if user_id is None:
            return

        selected_profile = self._profiles_by_user_id.get(user_id)
        if selected_profile is None:
            return

        self.result = AuthSwitchResult(
            status="completed",
            selected_profile=selected_profile,
        )
        self.exit()

    @staticmethod
    def _highlighted_profile_user_id(
        saved_profiles: Sequence[SavedProfileRecord],
    ) -> str | None:
        for profile in saved_profiles:
            if profile.is_active:
                return profile.user_id

        if len(saved_profiles) == 0:
            return None

        return saved_profiles[0].user_id

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
                if option.id == highlighted_id:
                    highlighted_index = index
                    break

        if highlighted_index is not None:
            self._options_view.highlighted = highlighted_index

    def _show_profile_selection(self) -> None:
        self._set_text(
            title="Switch MeshAgent Account",
            message="Choose which saved local profile should become active.",
            help_text="Use Up/Down and Enter. Esc or Ctrl+C cancels.",
        )

        options = [
            Option(
                _profile_option_label(profile),
                id=_profile_option_id(profile.user_id),
            )
            for profile in self._saved_profiles
        ]
        options.append(Option("Exit without switching", id=EXIT_OPTION_ID))

        highlighted_user_id = self._highlighted_profile_user_id(self._saved_profiles)
        highlighted_id = (
            _profile_option_id(highlighted_user_id)
            if highlighted_user_id is not None
            else EXIT_OPTION_ID
        )
        self._set_options(options=options, highlighted_id=highlighted_id)


async def _run_app(app: App[None]) -> None:
    app_token = active_app.set(app)
    try:
        await app.run_async()
    finally:
        active_app.reset(app_token)


async def run_auth_switch_tui(
    *,
    saved_profiles: Sequence[SavedProfileRecord],
) -> AuthSwitchResult:
    app = AuthSwitchApp(saved_profiles=saved_profiles)

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return AuthSwitchResult(
            status="canceled",
            message="Profile switch canceled.",
        )

    return app.result
