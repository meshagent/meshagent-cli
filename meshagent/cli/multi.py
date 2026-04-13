import typer
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from typing import Annotated, Optional
import os
import importlib.util
from pathlib import Path
import click
import shlex

from rich import print

from meshagent.cli.common_options import RoomOption
import asyncio

import yaml


app = async_typer.LazyTyper(help="Connect agents and tools to a room")

cli_service = async_typer.LazyTyper(help="Add agents to a team")
cli_service.add_lazy_command(
    name="chatbot",
    module="meshagent.cli.chatbot",
    command_path=("service",),
    help="Deploy a chatbot-backed service.",
)
cli_service.add_lazy_command(
    name="worker",
    module="meshagent.cli.worker",
    command_path=("service",),
    help="Deploy a worker-backed service.",
)
cli_service.add_lazy_command(
    name="mailbot",
    module="meshagent.cli.mailbot",
    command_path=("service",),
    help="Deploy a mailbot-backed service.",
)
cli_service.add_lazy_command(
    name="voicebot",
    module="meshagent.cli.voicebot",
    command_path=("service",),
    help="Deploy a voicebot-backed service.",
)

cli_join = async_typer.LazyTyper(help="Add agents to a team")
cli_join.add_lazy_command(
    name="chatbot",
    module="meshagent.cli.chatbot",
    command_path=("join",),
    help="Join a room and run a chatbot agent.",
)
cli_join.add_lazy_command(
    name="worker",
    module="meshagent.cli.worker",
    command_path=("join",),
    help="Join a room and run a worker agent.",
)
cli_join.add_lazy_command(
    name="mailbot",
    module="meshagent.cli.mailbot",
    command_path=("join",),
    help="Join a room and run a mailbot agent.",
)
cli_join.add_lazy_command(
    name="voicebot",
    module="meshagent.cli.voicebot",
    command_path=("join",),
    help="Join a room and run a voicebot agent.",
)
cli_join.add_lazy_command(
    name="webserver",
    module="meshagent.cli.webserver",
    command_path=("join",),
    help="Join a room and run a webserver agent.",
)


@cli_service.async_command("python")
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
    from meshagent.cli.host import get_service

    service = get_service(host=host, port=port)

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    imported_module = import_from_path(module)
    export_name = name or "main"
    try:
        entrypoint = imported_module.__dict__[export_name]
    except KeyError as exc:
        raise ImportError(f"{module} does not define {export_name}") from exc

    service.add_path(path=path, identity=identity, cls=entrypoint)


def execute_via_root(app, line: str, *, prog_name="meshagent") -> int:
    cmd = async_typer.get_command(app)
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
webserver ...;
python path-to-python-file.py --name=NameOfModule;

chatbot, worker, mailbot, voicebot, and webserver command arguments mirror those of the respective meshagent chatbot service, meshagent worker service, meshagent mailbot service, meshagent voicebot service, and meshagent webserver service commands.
"""


def build_spec(
    *,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    agent_name: str,
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
):
    from meshagent.cli.host import service_specs

    for c in command.split(";"):
        if execute_via_root(cli_service, c, prog_name="meshagent") != 0:
            print(f"[red]{c} failed[/red]")
            raise typer.Exit(1)

    specs = service_specs(token_identity=agent_name)
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
    spec.container.image = "meshagent/cli:default"
    spec.container.command = (
        f'meshagent multi service -c "{command.replace('"', '\\"')}"'
    )


@app.async_command(
    "spec", help="Generate a combined service spec from multiple subcommands."
)
async def spec(
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    agent_name: Annotated[
        str,
        typer.Option("--agent-name", help="identity for injected MESHAGENT_TOKEN"),
    ],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
):
    from meshagent.cli.host import set_deferred

    set_deferred(True)

    spec = build_spec(
        command=command,
        service_name=service_name,
        agent_name=agent_name,
        service_description=service_description,
        service_title=service_title,
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command(
    "deploy", help="Deploy a combined service from multiple subcommands."
)
async def deploy(
    project_id: ProjectIdOption,
    command: Annotated[str, typer.Option("-c", help=subcommand_help)],
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    agent_name: Annotated[
        str,
        typer.Option("--agent-name", help="identity for injected MESHAGENT_TOKEN"),
    ],
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
    ] = os.getenv("MESHAGENT_ROOM"),
):
    from aiohttp import ClientResponseError

    from meshagent.cli.helper import get_client, resolve_project_id
    from meshagent.cli.host import set_deferred

    project_id = await resolve_project_id(project_id)

    client = await get_client()
    try:
        set_deferred(True)

        spec = build_spec(
            command=command,
            service_name=service_name,
            agent_name=agent_name,
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
    from meshagent.cli.host import run_services, set_deferred

    set_deferred(True)

    for c in command.split(";"):
        if execute_via_root(cli_service, c, prog_name="meshagent") != 0:
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


@app.async_command("join", help="Run multiple join commands together in one process.")
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
    room: RoomOption,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
):
    from meshagent.agents import SingleRoomAgent
    from meshagent.api import RoomClient
    from meshagent.api.helpers import websocket_room_url
    from meshagent.api.websocket_protocol import WebSocketClientProtocol
    from meshagent.cli.host import agents, set_deferred

    set_deferred(True)

    if room is None:
        print("[bold red]--room is required[/bold red]")
        raise typer.Exit(-1)

    for c in command.split(";"):
        print(c, flush=True)
        command_args = c + f" --room={room}"
        if agent_name and "--agent-name" not in c:
            command_args += f" --agent-name={agent_name}"
        execute_via_root(cli_join, command_args, prog_name="meshagent")

    try:

        async def run_agent(agent: SingleRoomAgent, jwt: str):
            nonlocal room

            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
                    token=jwt,
                )
            ) as room:
                await agent.start(room=room)
                await room.protocol.wait_for_close()
                await agent.stop()

        await asyncio.gather(
            *([asyncio.create_task(run_agent(agent, jwt)) for agent, jwt in agents])
        )

    except KeyboardInterrupt:
        pass
