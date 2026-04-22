import click

from meshagent.cli.tool_integrations import launch_claude_code


@click.command(
    "claude-code",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help="Launch Claude Code through MeshAgent for the active project.",
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
    help="Override the MeshAgent API URL for this Claude Code launch.",
)
@click.pass_context
def claude_code_command(
    ctx: click.Context,
    project_id: str | None,
    api_url: str | None,
) -> None:
    exit_code = launch_claude_code(
        project_id=project_id,
        api_url=api_url,
        extra_args=ctx.args,
    )
    raise click.exceptions.Exit(exit_code)
