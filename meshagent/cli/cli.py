import asyncio

from meshagent.cli import async_typer

from meshagent.cli import multi

from meshagent.cli import auth
from meshagent.cli import auth_async
from meshagent.cli import api_keys
from meshagent.cli import projects
from meshagent.cli import sessions
from meshagent.cli import participant_token
from meshagent.cli import webhook
from meshagent.cli import services
from meshagent.cli import mailboxes
from meshagent.cli import routes

from meshagent.cli import call
from meshagent.cli import cli_mcp
from meshagent.cli import chatbot
from meshagent.cli import process
from meshagent.cli import voicebot
from meshagent.cli import mailbot
from meshagent.cli import worker
from meshagent.cli import task_runner
from meshagent.cli import scheduled_tasks
from meshagent.cli import cli_secrets
from meshagent.cli import helpers
from meshagent.cli import meeting_transcriber
from meshagent.cli import rooms
from meshagent.cli import room
from meshagent.cli import image
from meshagent.cli import port
from meshagent.cli import webserver
from meshagent.cli import codex
from meshagent.cli import test
from meshagent.cli.version import __version__
from meshagent.cli.helper import get_active_api_key, get_active_project, get_client
from meshagent.otel import otel_config

import logging

import os
import sys
import warnings
from pathlib import Path

otel_config(service_name="meshagent-cli")

# Turn down OpenAI logs, they are a bit noisy
logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("textual").setLevel(logging.WARNING)
logging.getLogger("textual.events").setLevel(logging.WARNING)
logging.getLogger("textual.message_pump").setLevel(logging.WARNING)
logging.getLogger("textual.screen").setLevel(logging.WARNING)


def _configure_warning_filters() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"Pydantic serializer warnings:.*",
        category=UserWarning,
        module=r"pydantic\.main",
    )


_configure_warning_filters()

app = async_typer.AsyncTyper(no_args_is_help=True, name="meshagent")
app.add_typer(call.app, name="call")
app.add_typer(auth.app, name="auth")
app.add_typer(projects.app, name="project")
app.add_typer(api_keys.app, name="api-key")
app.add_typer(sessions.app, name="session")
app.add_typer(participant_token.app, name="token")
app.add_typer(webhook.app, name="webhook")
app.add_typer(services.app, name="service")
app.add_typer(cli_mcp.app, name="mcp")
app.add_typer(cli_secrets.app, name="secret")
app.add_typer(helpers.app, name="helper")
app.add_typer(helpers.app, name="helpers", hidden=True)
app.add_typer(rooms.app, name="rooms")
app.add_typer(mailboxes.app, name="mailbox")
app.add_typer(routes.app, name="route")
app.add_typer(scheduled_tasks.app, name="scheduled-task")
app.add_typer(meeting_transcriber.app, name="meeting-transcriber")
app.add_typer(port.app, name="port")
app.add_typer(webserver.app, name="webserver")
app.add_typer(codex.app, name="codex")
if not os.getenv("MESHAGENT_CLI_BUILD"):
    app.add_typer(test.app, name="test", hidden=True)

app.add_typer(multi.app, name="multi")
app.add_typer(voicebot.app, name="voicebot")
app.add_typer(chatbot.app, name="chatbot")
app.add_typer(process.app, name="process")
app.add_typer(mailbot.app, name="mailbot")
app.add_typer(task_runner.app, name="task-runner")
app.add_typer(worker.app, name="worker")

app.add_typer(room.app, name="room")
app.add_typer(image.app, name="image")


def _run_async(coro):
    asyncio.run(coro)


def detect_shell() -> str:
    """
    Best-effort detection of the *current* interactive shell.

    Order of preference
    1. Explicit --shell argument (handled by Typer)
    2. Per-shell env vars set by the running shell
       • BASH_VERSION / ZSH_VERSION / FISH_VERSION
    3. $SHELL on POSIX (user’s login shell – still correct >90 % of the time)
    4. Parent process on Windows (COMSPEC → cmd / powershell)
    5. Safe default: 'bash'
    """
    # Per-shell version variables (works even if login shell ≠ current shell)
    for var, name in (
        ("ZSH_VERSION", "zsh"),
        ("BASH_VERSION", "bash"),
        ("FISH_VERSION", "fish"),
    ):
        if var in os.environ:
            return name

    # POSIX fallback: login shell path
    sh = os.environ.get("SHELL")
    if sh:
        return Path(sh).name.lower()

    # Windows heuristics
    if sys.platform == "win32":
        comspec = Path(os.environ.get("COMSPEC", "")).name.lower()
        if "powershell" in comspec:
            return "powershell"
        if "cmd" in comspec:
            return "cmd"
        return "powershell"  # sensible default on modern Windows

    # Last-ditch default
    return "bash"


def _bash_like(name: str, value: str, unset: bool) -> str:
    return f"unset {name}" if unset else f'export {name}="{value}"'


def _fish(name: str, value: str, unset: bool) -> str:
    return f"set -e {name}" if unset else f'set -gx {name} "{value}"'


def _powershell(name: str, value: str, unset: bool) -> str:
    return f"Remove-Item Env:{name}" if unset else f'$Env:{name}="{value}"'


def _cmd(name: str, value: str, unset: bool) -> str:
    return f"set {name}=" if unset else f"set {name}={value}"


SHELL_RENDERERS = {
    "bash": _bash_like,
    "zsh": _bash_like,
    "fish": _fish,
    "powershell": _powershell,
    "cmd": _cmd,
}


@app.command(
    "version",
    help="Print the version",
)
def version():
    print(__version__)


@app.command("setup")
def setup_command():
    """Perform initial login and project/api key activation."""

    async def runner():
        from meshagent.cli.tui.setup import (
            SetupProject,
            run_setup_wizard_tui,
        )

        async def list_setup_projects() -> list[SetupProject]:
            client = await get_client()
            try:
                response = await client.list_projects()
            finally:
                await client.close()

            if not isinstance(response, dict):
                return []

            project_rows = response.get("projects", [])
            if not isinstance(project_rows, list):
                return []

            setup_projects: list[SetupProject] = []
            for row in project_rows:
                if not isinstance(row, dict):
                    continue
                project_id = row.get("id")
                if not isinstance(project_id, str) or project_id.strip() == "":
                    continue
                project_name = row.get("name")
                resolved_name = (
                    project_name
                    if isinstance(project_name, str) and project_name.strip() != ""
                    else project_id
                )
                setup_projects.append(SetupProject(id=project_id, name=resolved_name))

            return setup_projects

        async def create_project_from_name(project_name: str) -> str:
            client = await get_client()
            try:
                created = await client.create_project(project_name)
            finally:
                await client.close()

            created_project_id = (
                created.get("id") if isinstance(created, dict) else None
            )
            if (
                not isinstance(created_project_id, str)
                or created_project_id.strip() == ""
            ):
                raise RuntimeError("Project creation did not return a valid id.")
            return created_project_id

        async def activate_project(project_id: str) -> str:
            activated_project_id = await projects.activate(
                project_id, interactive=False, return_project_id=True
            )
            if activated_project_id is None:
                raise RuntimeError("Unable to activate selected project.")
            return activated_project_id

        async def has_active_api_key(project_id: str) -> bool:
            return await get_active_api_key(project_id=project_id) is not None

        async def create_and_activate_api_key(
            project_id: str, api_key_name: str
        ) -> None:
            await api_keys.create(
                project_id=project_id,
                activate=True,
                silent=True,
                name=api_key_name,
            )

        result = await run_setup_wizard_tui(
            login_operation=lambda status_handler: auth_async.login(
                status_handler=status_handler,
                print_status=False,
            ),
            list_projects_operation=list_setup_projects,
            create_project_operation=create_project_from_name,
            activate_project_operation=activate_project,
            has_active_api_key_operation=has_active_api_key,
            create_api_key_operation=create_and_activate_api_key,
            active_project_id=await get_active_project(),
        )

        if result.status != "completed" and result.message is not None:
            print(result.message)

    _run_async(runner())


if __name__ == "__main__":
    app()
