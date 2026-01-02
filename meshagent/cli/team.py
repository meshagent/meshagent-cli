import typer
from meshagent.cli import async_typer
from meshagent.cli.host import run_services, set_deferred

from typing import Annotated, Optional

import click
import shlex

from typer.main import get_command

app = async_typer.AsyncTyper(help="Run a team of agents")

cli = None


def execute_via_root(app, line: str, *, prog_name="meshagent") -> int:
    cmd = get_command(app)
    try:
        cmd.main(args=shlex.split(line), prog_name=prog_name, standalone_mode=False)
        return 0
    except click.ClickException as e:
        e.show()
        return e.exit_code


@app.async_command("host")
async def host(
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    command: Annotated[
        str, typer.Option("-c", help="a list of commands to run, seperated by pipes")
    ] = [],
):
    set_deferred(True)

    for c in command.split("|"):
        execute_via_root(cli, c, prog_name="meshagent")

    await run_services()


def register_cli(c):
    global cli
    cli = c
