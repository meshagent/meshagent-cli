import click
from rich import print

from meshagent.cli.tool_integrations import (
    launch_claude,
    launch_codex,
    resolve_current_meshagent_executable,
)


def _exit_with_launch_error(message: str) -> None:
    print(f"[red]{message}[/red]")
    raise click.exceptions.Exit(1)


@click.group("launch")
def launch_group() -> None:
    """Launch supported CLI apps through MeshAgent."""


@launch_group.command(
    "codex",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Launch Codex through MeshAgent for the active project.",
)
@click.option(
    "--project-id",
    type=str,
    default=None,
    help="A MeshAgent project id. If empty, the activated project will be used.",
)
@click.option(
    "--api-url",
    type=str,
    default=None,
    help="Override the MeshAgent API URL for this Codex launch.",
)
@click.pass_context
def launch_codex_command(
    ctx: click.Context,
    project_id: str | None,
    api_url: str | None,
) -> None:
    try:
        exit_code = launch_codex(
            project_id=project_id,
            api_url=api_url,
            extra_args=ctx.args,
            meshagent_executable=resolve_current_meshagent_executable(),
        )
    except RuntimeError as exc:
        _exit_with_launch_error(str(exc))
        return

    raise click.exceptions.Exit(exit_code)


@launch_group.command(
    "claude",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Launch Claude through MeshAgent for the active project.",
)
@click.option(
    "--project-id",
    type=str,
    default=None,
    help="A MeshAgent project id. If empty, the activated project will be used.",
)
@click.option(
    "--api-url",
    type=str,
    default=None,
    help="Override the MeshAgent API URL for this Claude launch.",
)
@click.pass_context
def launch_claude_command(
    ctx: click.Context,
    project_id: str | None,
    api_url: str | None,
) -> None:
    try:
        exit_code = launch_claude(
            project_id=project_id,
            api_url=api_url,
            extra_args=ctx.args,
            meshagent_executable=resolve_current_meshagent_executable(),
        )
    except RuntimeError as exc:
        _exit_with_launch_error(str(exc))
        return

    raise click.exceptions.Exit(exit_code)
