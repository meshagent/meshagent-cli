import typer
import asyncio
from typing import Optional

from meshagent.cli import async_typer

from meshagent.cli import auth
from meshagent.cli import api_keys
from meshagent.cli import projects
from meshagent.cli import sessions
from meshagent.cli import participant_token
from meshagent.cli import agent
from meshagent.cli import messaging
from meshagent.cli import storage
from meshagent.cli import developer
from meshagent.cli import webhook
from meshagent.cli import services
from meshagent.cli import cli_secrets
from meshagent.cli import call
from meshagent.cli import cli_mcp
from meshagent.cli import chatbot
from meshagent.cli import voicebot
from meshagent.cli.exec import register as register_exec

from meshagent.cli import otel

from art import tprint

import logging

import os
import sys
from pathlib import Path
from meshagent.cli.helper import get_client, resolve_project_id, resolve_api_key


otel.init(level=logging.INFO)

# Turn down OpenAI logs, they are a bit noisy
logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

app = async_typer.AsyncTyper()
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
app.add_typer(cli_mcp.app, name="mcp")
app.add_typer(chatbot.app, name="chatbot")
app.add_typer(voicebot.app, name="voicebot")

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
    "env",
    help="Generate commands to set meshagent environment variables.",
)
def env(
    shell: Optional[str] = typer.Option(
        None,
        "--shell",
        case_sensitive=False,
        help="bash | zsh | fish | powershell | cmd",
    ),
    unset: bool = typer.Option(
        False, "--unset", help="Output commands to unset the variables."
    ),
):
    """Print shell-specific exports/unsets for Docker environment variables."""

    async def command():
        nonlocal shell, unset
        shell = (shell or detect_shell()).lower()
        if shell not in SHELL_RENDERERS:
            typer.echo(f"Unsupported shell '{shell}'.", err=True)
            raise typer.Exit(code=1)

        client = await get_client()
        try:
            project_id = await resolve_project_id(project_id=None)
            api_key_id = await resolve_api_key(project_id=project_id, api_key_id=None)

            token = (
                await client.decrypt_project_api_key(
                    project_id=project_id, id=api_key_id
                )
            )["token"]
        finally:
            await client.close()

        vars = {
            "MESHAGENT_PROJECT_ID": project_id,
            "MESHAGENT_KEY_ID": api_key_id,
            "MESHAGENT_SECRET": token,
        }
        if shell not in SHELL_RENDERERS:
            typer.echo(f"Unsupported shell '{shell}'.", err=True)
            raise typer.Exit(code=1)

        render = SHELL_RENDERERS[shell]

        for name, value in vars.items():
            typer.echo(render(name, value, unset))

        if not unset and shell in ("bash", "zsh"):
            typer.echo(
                "\n# Run this command to configure your current shell:\n"
                '# eval "$(meshagent env)"'
            )

    _run_async(command())


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
        if project_id is not None:
            print("Activate an api-key...")
            api_key_id = await api_keys.activate(None, interactive=True)
            if api_key_id is None:
                print("You have choosen to not activate an api-key. Exiting.")

    _run_async(runner())


if __name__ == "__main__":
    app()
