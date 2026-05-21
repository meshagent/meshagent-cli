# ruff: noqa: E402

from __future__ import annotations

import os
import re
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

DEPLOY_ROOM_CANCEL_OPTION_ID = "__deploy_room_cancel__"
DEPLOY_ROOM_CREATE_OPTION_ID = "__deploy_room_create__"
DEPLOY_ROOM_OPTION_ID_PREFIX = "__deploy_room__:"
SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _room_option_id(room_id: str) -> str:
    return f"{DEPLOY_ROOM_OPTION_ID_PREFIX}{room_id}"


@dataclass(frozen=True, slots=True)
class DeployRoomChoice:
    id: str
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class DeployRoomPickerResult:
    status: Literal["completed", "create", "canceled"]
    selected_room_name: str | None = None
    create_room_name: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class DeployDomainPromptResult:
    status: Literal["completed", "skipped", "canceled"]
    domain: str | None = None
    message: str | None = None


class DeployRoomPickerApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #deploy-room-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #deploy-room-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #deploy-room-options {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #deploy-room-input {
        width: 100%;
        margin: 1 0 0 0;
    }
    #deploy-room-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #deploy-room-error {
        width: 100%;
        content-align: left middle;
        color: #ff7b72;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_deploy", "Cancel", priority=True),
        Binding("escape", "cancel_deploy", "Cancel", priority=True),
    ]

    def __init__(
        self,
        *,
        rooms: Sequence[DeployRoomChoice],
        can_create_room: bool,
    ) -> None:
        super().__init__()
        self._rooms = list(rooms)
        self._can_create_room = can_create_room
        self._mode: Literal["select", "create"] = "select"
        self._rooms_by_option_id = {
            _room_option_id(room.id): room for room in self._rooms
        }
        self._title_view: Static | None = None
        self._message_view: Static | None = None
        self._options_view: OptionList | None = None
        self._input_view: Input | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None
        self.result = DeployRoomPickerResult(
            status="canceled",
            message="Deploy canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="deploy-room-title")
        yield Static("", id="deploy-room-message")
        yield OptionList(id="deploy-room-options")
        yield Input(id="deploy-room-input", placeholder="")
        yield Static("", id="deploy-room-help")
        yield Static("", id="deploy-room-error")

    async def on_mount(self) -> None:
        self._title_view = self.query_one("#deploy-room-title", Static)
        self._message_view = self.query_one("#deploy-room-message", Static)
        self._options_view = self.query_one("#deploy-room-options", OptionList)
        self._input_view = self.query_one("#deploy-room-input", Input)
        self._help_view = self.query_one("#deploy-room-help", Static)
        self._error_view = self.query_one("#deploy-room-error", Static)
        self._show_room_selection()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "deploy-room-input":
            return
        self._submit_create_room_name(event.value)

    async def action_cancel_deploy(self) -> None:
        if self._mode == "create":
            self._show_room_selection()
            return
        self.result = DeployRoomPickerResult(
            status="canceled",
            message="Deploy canceled.",
        )
        self.exit()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        selected_id = event.option.id
        if not isinstance(selected_id, str):
            return

        if selected_id == DEPLOY_ROOM_CANCEL_OPTION_ID:
            self.result = DeployRoomPickerResult(
                status="canceled",
                message="Deploy canceled.",
            )
            self.exit()
            return

        if selected_id == DEPLOY_ROOM_CREATE_OPTION_ID:
            self._show_create_room_input()
            return

        room = self._rooms_by_option_id.get(selected_id)
        if room is None:
            return

        self.result = DeployRoomPickerResult(
            status="completed",
            selected_room_name=room.name,
        )
        self.exit()

    def _show_room_selection(self) -> None:
        self._mode = "select"
        self._clear_error()
        self._set_text(
            title="MeshAgent Deploy",
            message="Choose the room to deploy to.",
            help_text=(
                "Only rooms where you are an owner are shown. "
                "Use Up/Down and Enter. Esc or Ctrl+C cancels."
            ),
        )
        options = [
            Option(
                self._room_option_label(room),
                id=_room_option_id(room.id),
            )
            for room in self._rooms
        ]
        if self._can_create_room:
            options.append(Option("Create new room", id=DEPLOY_ROOM_CREATE_OPTION_ID))
        options.append(Option("Cancel", id=DEPLOY_ROOM_CANCEL_OPTION_ID))

        if self._input_view is not None:
            self._input_view.display = False
        if self._options_view is not None:
            self._options_view.display = True
            self._options_view.clear_options()
            self._options_view.add_options(options)
            self._options_view.highlighted = 0
            self._options_view.focus()

    def _show_create_room_input(self) -> None:
        self._mode = "create"
        self._clear_error()
        self._set_text(
            title="MeshAgent Deploy",
            message="Enter a name for the new room.",
            help_text="Press Enter to create the room. Esc or Ctrl+C returns to rooms.",
        )
        if self._options_view is not None:
            self._options_view.display = False
        if self._input_view is None:
            return
        self._input_view.display = True
        self._input_view.placeholder = "Room name"
        self._input_view.value = ""
        self._input_view.focus()

    def _submit_create_room_name(self, value: str) -> None:
        try:
            room_name = self._validate_room_name(value)
        except ValueError as exc:
            self._show_error(str(exc))
            return

        self.result = DeployRoomPickerResult(
            status="create",
            create_room_name=room_name,
        )
        self.exit()

    @staticmethod
    def _validate_room_name(value: str) -> str:
        resolved_value = value.strip()
        if resolved_value == "":
            raise ValueError("Room name cannot be empty.")
        return resolved_value

    def _set_text(self, *, title: str, message: str, help_text: str) -> None:
        if self._title_view is None:
            return
        if self._message_view is None:
            return
        if self._help_view is None:
            return
        self._title_view.update(title)
        self._message_view.update(message)
        self._help_view.update(help_text)

    def _show_error(self, message: str) -> None:
        if self._error_view is None:
            return
        self._error_view.update(message)

    def _clear_error(self) -> None:
        if self._error_view is None:
            return
        self._error_view.update("")

    @staticmethod
    def _room_option_label(room: DeployRoomChoice) -> str:
        description = room.description.strip()
        if description == "":
            return room.name
        return f"{room.name} - {description}"


async def _run_app(app: App[None]) -> None:
    app_token = active_app.set(app)
    try:
        await app.run_async()
    finally:
        active_app.reset(app_token)


async def run_deploy_room_picker_tui(
    *,
    rooms: Sequence[DeployRoomChoice],
    can_create_room: bool,
) -> DeployRoomPickerResult:
    app = DeployRoomPickerApp(
        rooms=rooms,
        can_create_room=can_create_room,
    )

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return DeployRoomPickerResult(
            status="canceled",
            message="Deploy canceled.",
        )

    return app.result


class DeployDomainPromptApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #deploy-domain-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #deploy-domain-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #deploy-domain-input {
        width: 100%;
        margin: 1 0 0 0;
    }
    #deploy-domain-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #deploy-domain-error {
        width: 100%;
        content-align: left middle;
        color: #ff7b72;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_deploy", "Cancel", priority=True),
        Binding("escape", "skip_domain", "Skip", priority=True),
    ]

    def __init__(
        self,
        *,
        service_name: str,
        port: str,
        room_name: str,
        pages_domain: str,
    ) -> None:
        super().__init__()
        self._service_name = service_name
        self._port = port
        self._pages_domain = pages_domain.strip().lower().removeprefix(".")
        self._default_subdomain = self._subdomain_from_room_name(room_name)
        self._help_view: Static | None = None
        self._error_view: Static | None = None
        self.result = DeployDomainPromptResult(
            status="canceled",
            message="Deploy canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("MeshAgent Deploy", id="deploy-domain-title")
        yield Static(
            (
                f"Service {self._service_name} exposes port {self._port}. "
                f"The domain suffix is already selected: .{self._pages_domain}."
            ),
            id="deploy-domain-message",
        )
        yield Input(id="deploy-domain-input", placeholder="subdomain")
        yield Static(
            "",
            id="deploy-domain-help",
        )
        yield Static("", id="deploy-domain-error")

    async def on_mount(self) -> None:
        self._help_view = self.query_one("#deploy-domain-help", Static)
        self._error_view = self.query_one("#deploy-domain-error", Static)
        input_view = self.query_one("#deploy-domain-input", Input)
        input_view.value = self._default_subdomain
        self._update_domain_preview(input_view.value)
        input_view.focus()
        input_view.cursor_position = len(input_view.value)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "deploy-domain-input":
            return
        self._clear_error()
        self._update_domain_preview(event.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "deploy-domain-input":
            return
        subdomain = event.value.strip().lower()
        if subdomain == "":
            self.result = DeployDomainPromptResult(status="skipped")
            self.exit()
            return
        if not self._is_valid_subdomain(subdomain):
            self._set_error(
                "Enter only the subdomain. The domain suffix "
                f".{self._pages_domain} is already selected."
            )
            return
        self.result = DeployDomainPromptResult(
            status="completed",
            domain=f"{subdomain}.{self._pages_domain}",
        )
        self.exit()

    async def action_skip_domain(self) -> None:
        self.result = DeployDomainPromptResult(status="skipped")
        self.exit()

    async def action_cancel_deploy(self) -> None:
        self.result = DeployDomainPromptResult(
            status="canceled",
            message="Deploy canceled.",
        )
        self.exit()

    @staticmethod
    def _subdomain_from_room_name(room_name: str) -> str:
        normalized = room_name.strip().lower()
        normalized = re.sub(r"[^a-z0-9-]+", "-", normalized)
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        if normalized == "":
            normalized = "app"
        if len(normalized) > 63:
            normalized = normalized[:63].rstrip("-")
        return normalized or "app"

    @staticmethod
    def _is_valid_subdomain(value: str) -> bool:
        return SUBDOMAIN_PATTERN.fullmatch(value) is not None

    def _update_domain_preview(self, subdomain: str) -> None:
        preview_subdomain = subdomain.strip().lower() or "<subdomain>"
        if self._help_view is not None:
            self._help_view.update(
                f"Public domain: {preview_subdomain}.{self._pages_domain}. "
                "Press Enter to use it. Leave empty or press Esc to skip. Ctrl+C cancels."
            )

    def _clear_error(self) -> None:
        if self._error_view is not None:
            self._error_view.update("")

    def _set_error(self, message: str) -> None:
        if self._error_view is not None:
            self._error_view.update(message)


async def run_deploy_domain_prompt_tui(
    *,
    service_name: str,
    port: str,
    room_name: str,
    pages_domain: str,
) -> DeployDomainPromptResult:
    app = DeployDomainPromptApp(
        service_name=service_name,
        port=port,
        room_name=room_name,
        pages_domain=pages_domain,
    )

    try:
        await _run_app(app)
    except KeyboardInterrupt:
        return DeployDomainPromptResult(
            status="canceled",
            message="Deploy canceled.",
        )

    return app.result
