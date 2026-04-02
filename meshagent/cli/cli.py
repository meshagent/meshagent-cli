import asyncio

from meshagent.cli import async_typer

from meshagent.cli.version import __version__

import logging

import os
import sys
import warnings
from pathlib import Path


def _configure_warning_filters() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"Pydantic serializer warnings:.*",
        category=UserWarning,
        module=r"pydantic\.main",
    )


_configure_warning_filters()

_runtime_configured = False


def _configure_runtime() -> None:
    global _runtime_configured
    if _runtime_configured:
        return

    from meshagent.otel import otel_config

    otel_config(service_name="meshagent-cli")

    # Turn down noisy dependencies after OTEL installs the root handler.
    logging.getLogger("openai").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.ERROR)
    logging.getLogger("textual").setLevel(logging.WARNING)
    logging.getLogger("textual.events").setLevel(logging.WARNING)
    logging.getLogger("textual.message_pump").setLevel(logging.WARNING)
    logging.getLogger("textual.screen").setLevel(logging.WARNING)

    _runtime_configured = True


app = async_typer.LazyTyper(no_args_is_help=True, name="meshagent")


@app.callback()
def _root_callback() -> None:
    _configure_runtime()


app.add_lazy_command(
    name="call",
    module="meshagent.cli.call",
    help="Trigger agent/tool calls via URL",
)
app.add_lazy_command(
    name="auth",
    module="meshagent.cli.auth",
    help="Authenticate to meshagent",
)
app.add_lazy_command(
    name="project",
    module="meshagent.cli.projects",
    help="Manage or activate your meshagent projects",
)
app.add_lazy_command(
    name="api-key",
    module="meshagent.cli.api_keys",
    help="Manage or activate api-keys for your project",
)
app.add_lazy_command(
    name="session",
    module="meshagent.cli.sessions",
    help="Inspect recent sessions and events",
)
app.add_lazy_command(
    name="token",
    module="meshagent.cli.participant_token",
    help="Generate participant tokens (JWTs)",
)
app.add_lazy_command(
    name="webhook",
    module="meshagent.cli.webhook",
    help="Manage project webhooks",
)
app.add_lazy_command(
    name="service",
    module="meshagent.cli.services",
    help="Manage services for your project",
)
app.add_lazy_command(
    name="mcp",
    module="meshagent.cli.cli_mcp",
    help="Bridge MCP servers into MeshAgent rooms",
)
app.add_lazy_command(
    name="secret",
    module="meshagent.cli.cli_secrets",
    help="Manage secrets for your project.",
)
app.add_lazy_command(
    name="helper",
    module="meshagent.cli.helpers",
    help="Developer helper services",
)
app.add_lazy_command(
    name="helpers",
    module="meshagent.cli.helpers",
    help="Developer helper services",
    hidden=True,
)
app.add_lazy_command(
    name="rooms",
    module="meshagent.cli.rooms",
    help="Create, list, and manage rooms in a project",
)
app.add_lazy_command(
    name="mailbox",
    module="meshagent.cli.mailboxes",
    help="Manage mailboxes for your project",
)
app.add_lazy_command(
    name="route",
    module="meshagent.cli.routes",
    help="Manage routes for your project",
)
app.add_lazy_command(
    name="scheduled-task",
    module="meshagent.cli.scheduled_tasks",
    help="Manage scheduled tasks for your project",
)
app.add_lazy_command(
    name="meeting-transcriber",
    module="meshagent.cli.meeting_transcriber",
    help="Join a meeting transcriber to a room",
)
app.add_lazy_command(
    name="port",
    module="meshagent.cli.port",
    help="Port forwarding into room containers",
)
app.add_lazy_command(
    name="webserver",
    module="meshagent.cli.webserver",
    help="Run a webserver agent in a room",
    hidden=True,
)
app.add_lazy_command(
    name="codex",
    module="meshagent.cli.codex",
    help="Codex-backed agents",
    hidden=True,
)
if not os.getenv("MESHAGENT_CLI_BUILD"):
    app.add_lazy_command(
        name="test",
        module="meshagent.cli.test",
        help="Hidden test tools",
        hidden=True,
    )

app.add_lazy_command(
    name="multi",
    module="meshagent.cli.multi",
    help="Connect agents and tools to a room",
)
app.add_lazy_command(
    name="voicebot",
    module="meshagent.cli.voicebot",
    help="Join a voicebot to a room",
)
app.add_lazy_command(
    name="chatbot",
    module="meshagent.cli.chatbot",
    help="Join a chatbot to a room",
    hidden=True,
)
app.add_lazy_command(
    name="process",
    module="meshagent.cli.process",
    help="Join a process-backed agent to a room",
)
app.add_lazy_command(
    name="mailbot",
    module="meshagent.cli.mailbot",
    help="Join a mailbot to a room",
    hidden=True,
)
app.add_lazy_command(
    name="task-runner",
    module="meshagent.cli.task_runner",
    help="Join a taskrunner to a room",
    hidden=True,
)
app.add_lazy_command(
    name="worker",
    module="meshagent.cli.worker",
    help="Join a worker agent to a room",
    hidden=True,
)

app.add_lazy_command(
    name="room",
    module="meshagent.cli.room",
    help="Operate within a room",
)
app.add_lazy_command(
    name="image",
    module="meshagent.cli.image",
    help="Build and pack OCI images",
)


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
        from meshagent.cli import api_keys, auth_async, projects
        from meshagent.cli.helper import (
            get_active_api_key,
            get_active_project,
            get_client,
        )
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
