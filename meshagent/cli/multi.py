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

from meshagent.cli.chatbot import service as chatbot_service
from meshagent.cli.worker import service as worker_service
from meshagent.cli.mailbot import service as mailbot_service
from meshagent.cli.voicebot import service as voicebot_service

import yaml


app = async_typer.AsyncTyper(help="Connect agents and tools to a room")

cli = async_typer.AsyncTyper(help="Add agents to a team")

cli.command("chatbot")(chatbot_service)
cli.command("worker")(worker_service)
cli.command("mailbot")(mailbot_service)
cli.command("voicebot")(voicebot_service)


@cli.async_command("python")
async def python(
    *,
    module: str,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str],
        typer.Option(help="A path to add the service at"),
    ] = None,
    identity: Annotated[
        Optional[str],
        typer.Option(help="The desired identity for the service"),
    ] = None,
    name: Annotated[
        str, typer.Option(help="Entry-point name in the Python module")
    ] = "main",
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


def execute_via_root(app, line: str, *, prog_name="meshagent") -> int:
    cmd = get_command(app)
    try:
        cmd.main(args=shlex.split(line), prog_name=prog_name, standalone_mode=False)
        return 0
    except click.ClickException as e:
        e.show()
        return e.exit_code


subcommand_help = """a list of sub commands to run, seperated by semicolons

available sub commands:

chatbot ...;
mailbot ...;
worker ...;
voicebot ...;
python path-to-python-file.py --name=NameOfModule;

chatbot, worker, and mailbot command arguments mirror those of the respective meshagent chatbot service, meshagent mailbot service, meshagent voicebot service, and meshagent worker service commands.
"""


def build_spec(
    *,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
):
    for c in command.split(";"):
        if execute_via_root(cli, c, prog_name="meshagent") != 0:
            print(f"[red]{c} failed[/red]")
            raise typer.Exit(1)

    specs = service_specs()
    if len(specs) == 0:
        print("[red]found no services, specify at least one agent or tool to run[/red]")
        raise typer.Exit(1)

    if len(specs) > 1:
        print(
            "[red]found multiple services leave host and port empty or use the same port for each command[/red]"
        )
        raise typer.Exit(1)

    spec = specs[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    for port in spec.ports:
        port.num = "*"

    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = (
        "us-central1-docker.pkg.dev/meshagent-public/images/cli:{SERVER_VERSION}-esgz"
    )
    spec.container.command = (
        f'meshagent multi service -c "{command.replace('"', '\\"')}"'
    )


@app.async_command("spec")
async def spec(
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
):
    set_deferred(True)

    spec = build_spec(
        command=command,
        service_name=service_name,
        service_description=service_description,
        service_title=service_title,
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy")
async def deploy(
    project_id: ProjectIdOption,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = None,
):
    project_id = await resolve_project_id(project_id)

    client = await get_client()
    try:
        set_deferred(True)

        spec = build_spec(
            command=command,
            service_name=service_name,
            service_description=service_description,
            service_title=service_title,
        )

        spec.container.secrets = []

        id = None
        try:
            if id is None:
                if room is None:
                    services = await client.list_services(project_id=project_id)
                else:
                    services = await client.list_room_services(
                        project_id=project_id, room_name=room
                    )

                for s in services:
                    if s.metadata.name == spec.metadata.name:
                        id = s.id

            if id is None:
                if room is None:
                    id = await client.create_service(
                        project_id=project_id, service=spec
                    )
                else:
                    id = await client.create_room_service(
                        project_id=project_id, service=spec, room_name=room
                    )

            else:
                spec.id = id
                if room is None:
                    await client.update_service(
                        project_id=project_id, service_id=id, service=spec
                    )
                else:
                    await client.update_room_service(
                        project_id=project_id,
                        service_id=id,
                        service=spec,
                        room_name=room,
                    )

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Deployed service:[/] {id}")

    finally:
        await client.close()


@app.async_command("service")
async def host(
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)] = [],
):
    set_deferred(True)

    for c in command.split(";"):
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


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)] = [],
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

    for c in command.split(";"):
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
