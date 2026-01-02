import typer
from meshagent.cli import async_typer
from meshagent.cli.host import run_services, set_deferred, service_specs, get_service
from meshagent.cli.common_options import ProjectIdOption
from typing import Annotated, Optional

import importlib.util
from pathlib import Path
import click
import shlex

from rich import print
from meshagent.cli.call import _make_call
import sys

from typer.main import get_command

from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)
from meshagent.api import (
    ParticipantToken,
    ApiScope,
)
from aiohttp import ClientResponseError
import asyncio

from meshagent.api.keys import parse_api_key

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


@app.async_command("service")
async def host(
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    command: Annotated[
        str, typer.Option("-c", help="a list of commands to run, seperated by pipes")
    ] = [],
):
    set_deferred(True)

    for c in command.split("|"):
        if execute_via_root(cli, c, prog_name="meshagent") != 0:
            print(f"[red]{c} failed[/red]")
            raise typer.Exit(1)

    await run_services()


def import_from_path(path: str, module_name: str | None = None):
    path = Path(path)
    module_name = module_name or path.stem

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@app.async_command("add")
async def add(
    *,
    module: str,
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[
        Optional[str],
        typer.Option(help="A path to add the service at"),
    ] = None,
    identity: Annotated[
        Optional[str],
        typer.Option(help="The desired identity for the service"),
    ] = None,
    name: Annotated[str, typer.Option()] = "main",
):
    service = get_service(host=host, port=port)

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    module = import_from_path(module)
    service.add_path(path=path, identity=identity, cls=getattr(module, name or "main"))


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption = None,
    command: Annotated[
        str, typer.Option("-c", help="a list of commands to run, seperated by pipes")
    ] = [],
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help=(
                "a port number to run the agent on (will set MESHAGENT_PORT environment variable when launching the service)"
            ),
        ),
    ] = None,
    room: Annotated[
        Optional[str],
        typer.Option(
            help="A room name to test the service in (must not be currently running)"
        ),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    set_deferred(True)

    for c in command.split("|"):
        if execute_via_root(cli, c, prog_name="meshagent") != 0:
            print(f"[red]{c} failed[/red]")
            raise typer.Exit(1)

    services_task = asyncio.create_task(run_services())

    key = await resolve_key(project_id=project_id, key=key)

    if port is None:
        import socket

        def find_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", 0))  # Bind to a free port provided by the host.
                s.listen(1)
                return s.getsockname()[1]

        port = find_free_port()

    my_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)

        if room is None:
            print("[bold red]Room was not set[/bold red]")
            raise typer.Exit(1)

        try:
            parsed_key = parse_api_key(key)
            token = ParticipantToken(
                name="cli", project_id=project_id, api_key_id=parsed_key.id
            )
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant("user")
            token.add_room_grant(room)

            print("[bold green]Connecting to room...[/bold green]")

            run_tasks = []

            for spec in service_specs():
                sys.stdout.write("\n")

                for p in spec.ports:
                    print(f"[bold green]Connecting port {p.num}...[/bold green]")

                    for endpoint in p.endpoints:
                        print(
                            f"[bold green]Connecting endpoint {endpoint.path}...[/bold green]"
                        )

                        run_tasks.append(
                            asyncio.create_task(
                                _make_call(
                                    room=room,
                                    project_id=project_id,
                                    participant_name=endpoint.meshagent.identity,
                                    url=f"http://localhost:{p.num}{endpoint.path}",
                                    arguments={},
                                    key=key,
                                    permissions=endpoint.meshagent.api,
                                )
                            )
                        )

                await asyncio.gather(*run_tasks, services_task)

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Room already in use: {room}[/red]")
                raise typer.Exit(code=1)
            raise

        except Exception as e:
            print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)

    finally:
        await my_client.close()


def register_cli(c):
    global cli
    cli = c
