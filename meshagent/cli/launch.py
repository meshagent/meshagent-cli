from typing import Annotated

import typer
from typer import _click as typer_click
from rich import print

from meshagent.cli import async_typer
from meshagent.cli.tool_integrations import (
    launch_claude,
    launch_codex,
    resolve_current_meshagent_executable,
)


def _exit_with_launch_error(message: str) -> None:
    print(f"[red]{message}[/red]")
    raise typer_click.exceptions.Exit(1)


app = async_typer.AsyncTyper(
    help="Launch supported CLI apps through MeshAgent.",
    add_completion=False,
)


@app.command(
    "codex",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Launch Codex through MeshAgent for the active project.",
)
def launch_codex_command(
    ctx: typer.Context,
    project_id: Annotated[
        str | None,
        typer.Option(
            "--project-id",
            help="A MeshAgent project id. If empty, the activated project will be used.",
        ),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            help="Override the MeshAgent API URL for this Codex launch.",
        ),
    ] = None,
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

    raise typer_click.exceptions.Exit(exit_code)


@app.command(
    "claude",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Launch Claude through MeshAgent for the active project.",
)
def launch_claude_command(
    ctx: typer.Context,
    project_id: Annotated[
        str | None,
        typer.Option(
            "--project-id",
            help="A MeshAgent project id. If empty, the activated project will be used.",
        ),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            help="Override the MeshAgent API URL for this Claude launch.",
        ),
    ] = None,
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

    raise typer_click.exceptions.Exit(exit_code)


launch_group = async_typer.get_command(app)
