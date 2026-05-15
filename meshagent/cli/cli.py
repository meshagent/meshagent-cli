from meshagent.cli import async_typer

import logging

import os
import sys
import warnings
from pathlib import Path

from meshagent.cli.local_settings import apply_active_profile_api_url_environment


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

    try:
        from meshagent.otel import otel_config
    except ModuleNotFoundError as exc:
        if exc.name != "meshagent.otel":
            raise
        otel_config = None

    if otel_config is not None:
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
    apply_active_profile_api_url_environment()
    _configure_runtime()


app.add_lazy_command(
    name="version",
    module="meshagent.cli.root_commands",
    attribute="version_command",
    help="Print the version",
)
app.add_lazy_command(
    name="setup",
    module="meshagent.cli.root_commands",
    attribute="setup_command",
    help="Perform initial login and project/api key activation.",
)
app.add_lazy_command(
    name="call",
    module="meshagent.cli.call",
    help="Trigger agent/tool calls in a room",
    hidden=True,
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
    name="config",
    module="meshagent.cli.config",
    help="Read MeshAgent deployment configuration",
)
app.add_lazy_command(
    name="doctor",
    module="meshagent.cli.doctor",
    attribute="doctor_command",
    help="Inspect a project for MeshAgent deployment gaps",
    hidden=True,
)
app.add_lazy_command(
    name="create",
    module="meshagent.cli.create",
    attribute="create_command",
    help="Create a minimal deployable hello world project",
    hidden=True,
)
app.add_lazy_command(
    name="session",
    module="meshagent.cli.sessions",
    help="Inspect recent sessions and events",
)
app.add_lazy_command(
    name="ask",
    module="meshagent.cli.ask",
    help="Send a one-shot prompt through the LLM router",
    attribute="ask_command",
)
app.add_lazy_command(
    name="launch",
    module="meshagent.cli.launch",
    attribute="launch_group",
    help="Launch CLI apps through MeshAgent",
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
    name="package",
    module="meshagent.cli.agent_package_cli",
    help="Build, run, and deploy packaged services",
    hidden=True,
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
    hidden=True,
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
    name="feed",
    module="meshagent.cli.feeds",
    help="Manage feeds for your project",
)
app.add_lazy_command(
    name="subscription",
    module="meshagent.cli.subscriptions",
    help="Manage feed subscriptions for your project",
)
app.add_lazy_command(
    name="route",
    module="meshagent.cli.routes",
    help="Manage routes for your project",
)
app.add_lazy_command(
    name="registry",
    module="meshagent.cli.registry",
    help="Manage registries for your project",
)
app.add_lazy_command(
    name="build",
    module="meshagent.cli.image",
    attribute="app",
    command_path=("build",),
    help="Build a container image inside a room",
)
app.add_lazy_command(
    name="deploy",
    module="meshagent.cli.image",
    attribute="app",
    command_path=("deploy",),
    help="Create or update a room service from an image",
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
    hidden=True,
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
    name="llm",
    module="meshagent.cli.llm",
    help="Local LLM proxy utilities",
)


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


if __name__ == "__main__":
    app()
