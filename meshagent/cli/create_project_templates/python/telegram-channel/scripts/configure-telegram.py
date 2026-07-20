# ruff: noqa: E402

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Literal, Sequence


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None:
        return

    parsed_features = [
        value.strip() for value in raw_features.split(",") if value.strip() != ""
    ]
    if not parsed_features:
        return

    filtered_features = [
        value for value in parsed_features if value.lower() not in ("debug", "devtools")
    ]
    if len(filtered_features) == len(parsed_features):
        return
    if not filtered_features:
        os.environ.pop("TEXTUAL", None)
        return
    os.environ["TEXTUAL"] = ",".join(filtered_features)


_suppress_textual_debug_features()

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Log, Static


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
ENV_KEY_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass(frozen=True, slots=True)
class TelegramSetupField:
    name: str
    title: str
    description: str
    secret: bool = False
    default: str = ""


@dataclass(frozen=True, slots=True)
class TelegramSetupResult:
    status: Literal["completed", "canceled"]
    values: dict[str, str] = field(default_factory=dict)
    message: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramInstallResult:
    status: Literal["completed", "failed", "canceled"]
    message: str | None = None


class TelegramSetupApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #telegram-setup-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #telegram-setup-message {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #telegram-setup-description {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 0 0 1 0;
    }
    #telegram-setup-input {
        width: 100%;
        margin: 1 0 0 0;
    }
    #telegram-setup-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #telegram-setup-error {
        width: 100%;
        content-align: left middle;
        color: #ff7b72;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_setup", "Cancel", priority=True),
        Binding("escape", "cancel_setup", "Cancel", priority=True),
    ]

    def __init__(self, *, fields: Sequence[TelegramSetupField]) -> None:
        super().__init__()
        self._fields = list(fields)
        self._index = 0
        self._values: dict[str, str] = {}
        self._message_view: Static | None = None
        self._description_view: Static | None = None
        self._input_view: Input | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None
        self.result = TelegramSetupResult(
            status="canceled",
            message="Telegram setup canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("Telegram Channel Setup", id="telegram-setup-title")
        yield Static("", id="telegram-setup-message")
        yield Static("", id="telegram-setup-description")
        yield Input(id="telegram-setup-input", placeholder="value")
        yield Static("", id="telegram-setup-help")
        yield Static("", id="telegram-setup-error")

    async def on_mount(self) -> None:
        self._message_view = self.query_one("#telegram-setup-message", Static)
        self._description_view = self.query_one(
            "#telegram-setup-description",
            Static,
        )
        self._input_view = self.query_one("#telegram-setup-input", Input)
        self._help_view = self.query_one("#telegram-setup-help", Static)
        self._error_view = self.query_one("#telegram-setup-error", Static)
        if not self._fields:
            self.result = TelegramSetupResult(status="completed", values={})
            self.exit()
            return
        self._show_current_field()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "telegram-setup-input":
            return
        self._submit_current_value(event.value)

    async def action_cancel_setup(self) -> None:
        self.result = TelegramSetupResult(
            status="canceled",
            message="Telegram setup canceled.",
        )
        self.exit()

    def _show_current_field(self) -> None:
        field = self._fields[self._index]
        if self._message_view is not None:
            self._message_view.update(
                f"Field {self._index + 1} of {len(self._fields)}: {field.title}"
            )
        if self._description_view is not None:
            self._description_view.update(field.description)
        if self._help_view is not None:
            self._help_view.update("Press Enter to continue. Esc or Ctrl+C cancels.")
        if self._error_view is not None:
            self._error_view.update("")
        if self._input_view is None:
            return
        self._input_view.value = field.default
        self._input_view.placeholder = field.name
        self._input_view.password = field.secret
        self._input_view.focus()
        self._input_view.cursor_position = len(self._input_view.value)

    def _submit_current_value(self, value: str) -> None:
        field = self._fields[self._index]
        resolved_value = value.strip()
        if resolved_value == "":
            self._show_error(f"{field.title} is required.")
            return
        if "\n" in resolved_value or "\r" in resolved_value:
            self._show_error(f"{field.name} cannot contain newlines.")
            return
        if field.name == "TELEGRAM_API_ID" and not resolved_value.isdigit():
            self._show_error("Telegram API ID must be numeric.")
            return

        self._values[field.name] = resolved_value
        self._index += 1
        if self._index >= len(self._fields):
            self.result = TelegramSetupResult(
                status="completed",
                values=dict(self._values),
            )
            self.exit()
            return
        self._show_current_field()

    def _show_error(self, message: str) -> None:
        if self._error_view is not None:
            self._error_view.update(message)


async def run_telegram_setup_tui(
    *, fields: Sequence[TelegramSetupField]
) -> TelegramSetupResult:
    app = TelegramSetupApp(fields=fields)
    try:
        await app.run_async()
    except KeyboardInterrupt:
        return TelegramSetupResult(
            status="canceled",
            message="Telegram setup canceled.",
        )
    return app.result


class TelegramInstallProgressApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        align: left top;
        padding: 1 2;
        background: #050911;
    }
    #telegram-install-title {
        width: 100%;
        content-align: left middle;
        color: #f4f7ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #telegram-install-status {
        width: 100%;
        content-align: left middle;
        color: #cad6f4;
        margin: 0 0 1 0;
    }
    #telegram-install-log {
        width: 100%;
        height: 1fr;
        border: round #7ca9ff;
        background: #0a1120;
    }
    #telegram-install-help {
        width: 100%;
        content-align: left middle;
        color: #95a7ce;
        margin: 1 0 0 0;
    }
    #telegram-install-error {
        width: 100%;
        content-align: left middle;
        color: #ff7b72;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_install", "Cancel", priority=True),
        Binding("escape", "cancel_install", "Cancel", priority=True),
        Binding("enter", "exit_if_finished", "Exit", priority=True),
    ]

    def __init__(self, *, command: list[str], log_path: Path) -> None:
        super().__init__()
        self._command = command
        self._log_path = log_path
        self._process: asyncio.subprocess.Process | None = None
        self._status_view: Static | None = None
        self._log_view: Log | None = None
        self._help_view: Static | None = None
        self._error_view: Static | None = None
        self._finished = False
        self.result = TelegramInstallResult(
            status="canceled",
            message="Telegram setup install canceled.",
        )

    def compose(self) -> ComposeResult:
        yield Static("Telegram Channel Setup", id="telegram-install-title")
        yield Static("Preparing Telegram dependencies...", id="telegram-install-status")
        yield Log(id="telegram-install-log", highlight=True)
        yield Static(
            "Installing project dependencies. Ctrl+C cancels.",
            id="telegram-install-help",
        )
        yield Static("", id="telegram-install-error")

    async def on_mount(self) -> None:
        self._status_view = self.query_one("#telegram-install-status", Static)
        self._log_view = self.query_one("#telegram-install-log", Log)
        self._help_view = self.query_one("#telegram-install-help", Static)
        self._error_view = self.query_one("#telegram-install-error", Static)
        asyncio.create_task(self._run_install())

    async def _run_install(self) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("w", encoding="utf-8") as log_file:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert self._process.stdout is not None
            async for raw_line in self._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                log_file.write(line + "\n")
                log_file.flush()
                if self._log_view is not None:
                    self._log_view.write_line(line)
            returncode = await self._process.wait()

        self._finished = True
        self._process = None
        if returncode == 0:
            self.result = TelegramInstallResult(status="completed")
            self.exit()
            return
        self.result = TelegramInstallResult(
            status="failed",
            message=f"Install failed with exit code {returncode}.",
        )
        if self._status_view is not None:
            self._status_view.update("Telegram dependency install failed.")
        if self._error_view is not None:
            self._error_view.update(
                f"Install log: {self._log_path}. Press Enter, Esc, or Ctrl+C to exit."
            )
        if self._help_view is not None:
            self._help_view.update(
                "Fix the install error, then rerun ./scripts/configure-telegram.sh."
            )

    async def action_cancel_install(self) -> None:
        if self._finished:
            self.exit()
            return
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
        self.result = TelegramInstallResult(
            status="canceled",
            message="Telegram setup install canceled.",
        )
        self.exit()

    async def action_exit_if_finished(self) -> None:
        if self._finished:
            self.exit()


async def run_telegram_install_tui(
    *, command: list[str], log_path: Path
) -> TelegramInstallResult:
    app = TelegramInstallProgressApp(command=command, log_path=log_path)
    try:
        await app.run_async()
    except KeyboardInterrupt:
        return TelegramInstallResult(
            status="canceled",
            message="Telegram setup install canceled.",
        )
    return app.result


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def decode_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_KEY_RE.match(line.strip())
        if match is None:
            continue
        values[match.group(1)] = decode_env_value(match.group(2))
    return values


def merged_env_values() -> dict[str, str]:
    values = load_env_file(ENV_PATH)
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN"):
        env_value = os.getenv(name, "").strip()
        if env_value:
            values[name] = env_value
    return values


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    if ENV_EXAMPLE_PATH.exists():
        shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
    else:
        ENV_PATH.write_text("", encoding="utf-8")
    print("Created .env")


def set_env_values(updates: dict[str, str]) -> None:
    if not updates:
        return
    ensure_env_file()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in lines:
        match = ENV_KEY_RE.match(line.strip())
        if match is None or match.group(1) not in remaining:
            next_lines.append(line)
            continue
        name = match.group(1)
        next_lines.append(f"{name}={remaining.pop(name)}")
    if remaining:
        if next_lines and next_lines[-1] != "":
            next_lines.append("")
        for name, value in remaining.items():
            next_lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def runtime_dependencies_ready() -> bool:
    return (
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import meshagent.telegram, textual",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def ensure_runtime_dependencies() -> bool:
    if runtime_dependencies_ready():
        return True
    setup_log = Path(
        os.getenv(
            "MESHAGENT_TELEGRAM_SETUP_LOG",
            str(ROOT / ".meshagent/telegram-setup-install.log"),
        )
    )
    if not setup_log.is_absolute():
        setup_log = ROOT / setup_log
    result = asyncio.run(
        run_telegram_install_tui(
            command=["./scripts/install.sh"],
            log_path=setup_log,
        )
    )
    if result.status == "completed":
        return True
    print(result.message or "Telegram dependency install failed.", file=sys.stderr)
    print(f"Install log: {setup_log}", file=sys.stderr)
    return False


def configure_api_credentials(values: dict[str, str]) -> bool:
    file_values = load_env_file(ENV_PATH)
    updates: dict[str, str] = {}
    fields: list[TelegramSetupField] = []
    api_id = values.get("TELEGRAM_API_ID", "").strip()
    api_hash = values.get("TELEGRAM_API_HASH", "").strip()
    bot_token = values.get("TELEGRAM_BOT_TOKEN", "").strip()

    if api_id and not api_id.isdigit():
        api_id = ""

    if not api_id:
        fields.append(
            TelegramSetupField(
                name="TELEGRAM_API_ID",
                title="Telegram API ID",
                description=(
                    "Create or reuse a Telegram API app at https://my.telegram.org. "
                    "The API ID is numeric."
                ),
            )
        )
    elif file_values.get("TELEGRAM_API_ID", "").strip() != api_id:
        updates["TELEGRAM_API_ID"] = api_id

    if not api_hash:
        fields.append(
            TelegramSetupField(
                name="TELEGRAM_API_HASH",
                title="Telegram API hash",
                description=(
                    "Paste the API hash from https://my.telegram.org. "
                    "The value is hidden while you type."
                ),
                secret=True,
            )
        )
    elif file_values.get("TELEGRAM_API_HASH", "").strip() != api_hash:
        updates["TELEGRAM_API_HASH"] = api_hash

    if not bot_token:
        fields.append(
            TelegramSetupField(
                name="TELEGRAM_BOT_TOKEN",
                title="Telegram bot token",
                description=(
                    "Run ./scripts/create-bot-token.sh to create one with "
                    "BotFather, or paste an existing bot token. The value is "
                    "hidden while you type."
                ),
                secret=True,
            )
        )
    elif file_values.get("TELEGRAM_BOT_TOKEN", "").strip() != bot_token:
        updates["TELEGRAM_BOT_TOKEN"] = bot_token

    if fields:
        result = asyncio.run(run_telegram_setup_tui(fields=fields))
        if result.status != "completed":
            print(result.message or "Telegram setup canceled.", file=sys.stderr)
            return False
        updates.update(result.values)

    set_env_values(updates)
    return True


def main() -> int:
    values = merged_env_values()
    missing = [
        name
        for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN")
        if not values.get(name, "").strip()
    ]
    if not missing:
        print("Telegram credentials are already configured.")
        return 0
    if not is_interactive():
        print(
            "Telegram credentials are missing and this terminal is not interactive.",
            file=sys.stderr,
        )
        print("Run ./scripts/configure-telegram.sh in a terminal.", file=sys.stderr)
        return 1

    if not ensure_runtime_dependencies():
        return 1
    if not configure_api_credentials(values):
        return 1
    values = merged_env_values()
    missing = [
        name
        for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN")
        if not values.get(name, "").strip()
    ]
    if missing:
        print(
            "Telegram credentials were not saved to .env: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    print("")
    print("Telegram environment is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
