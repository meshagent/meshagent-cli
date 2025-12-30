import typer
import asyncio

from meshagent.cli import async_typer

from meshagent.cli import queue
from meshagent.cli import auth
from meshagent.cli import api_keys
from meshagent.cli import projects
from meshagent.cli import sessions
from meshagent.cli import database
from meshagent.cli import participant_token
from meshagent.cli import agent
from meshagent.cli import messaging
from meshagent.cli import storage
from meshagent.cli import developer
from meshagent.cli import webhook
from meshagent.cli import services
from meshagent.cli import mailboxes
from meshagent.cli import cli_secrets
from meshagent.cli import call
from meshagent.cli import cli_mcp
from meshagent.cli import chatbot
from meshagent.cli import voicebot
from meshagent.cli import mailbot
from meshagent.cli import worker
from meshagent.cli import task_runner
from meshagent.cli import containers
from meshagent.cli import oauth2
from meshagent.cli import helpers
from meshagent.cli import meeting_transcriber
from meshagent.cli import room
from meshagent.cli.exec import register as register_exec
from meshagent.cli.version import __version__
from meshagent.cli.helper import get_active_api_key
from meshagent.otel import otel_config


from art import tprint

import logging

import os
import sys
from pathlib import Path

otel_config(service_name="meshagent-cli")


# Turn down OpenAI logs, they are a bit noisy
logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

app = async_typer.AsyncTyper(no_args_is_help=True)
app.add_typer(call.app, name="call")
app.add_typer(auth.app, name="auth")
app.add_typer(projects.app, name="project")
app.add_typer(api_keys.app, name="api-key")
app.add_typer(sessions.app, name="session")
app.add_typer(participant_token.app, name="participant-token")
app.add_typer(agent.app, name="agents")
app.add_typer(messaging.app, name="messaging")
app.add_typer(storage.app, name="storage")
app.add_typer(developer.app, name="developer")
app.add_typer(webhook.app, name="webhook")
app.add_typer(services.app, name="service")
app.add_typer(cli_secrets.app, name="secret")
app.add_typer(queue.app, name="queue")
app.add_typer(cli_mcp.app, name="mcp")
app.add_typer(chatbot.app, name="chatbot")
app.add_typer(task_runner.app, name="task-runner")
app.add_typer(voicebot.app, name="voicebot")
app.add_typer(mailbot.app, name="mailbot")
app.add_typer(worker.app, name="worker")
app.add_typer(containers.app, name="container")
app.add_typer(oauth2.app, name="oauth2")
app.add_typer(helpers.app, name="helpers")
app.add_typer(room.app, name="room")
app.add_typer(mailboxes.app, name="mailbox")
app.add_typer(database.app, name="database")
app.add_typer(meeting_transcriber.app, name="meeting-transcriber")

register_exec(app)


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
        print("\n", flush=True)
        tprint("MeshAgent", "tarty10")
        print("\n", flush=True)
        await auth.login()
        print("Activate a project...")
        project_id = await projects.activate(None, interactive=True)
        if project_id is None:
            print("You have choosen to not activate a project. Exiting.")
        if (
            project_id is not None
            and await get_active_api_key(project_id=project_id) is None
        ):
            if typer.confirm(
                "You do not have an active api key for this project. Would you like to create and activate a new api key?",
                default=True,
            ):
                name = typer.prompt(
                    "Enter a name for your API Key (must be a unique name):"
                )
                await api_keys.create(
                    project_id=None, activate=True, silent=True, name=name
                )

    _run_async(runner())


if __name__ == "__main__":
    app()
