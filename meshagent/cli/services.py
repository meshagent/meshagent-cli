# ---------------------------------------------------------------------------
#  Imports
# ---------------------------------------------------------------------------
import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption
from aiohttp import ClientResponseError
import pathlib
from meshagent.cli import async_typer
from meshagent.api.services import well_known_service_path
from meshagent.api.specs.service import ServiceSpec
from meshagent.api.keys import parse_api_key

import asyncio
import shlex

import os
import signal
import atexit
import ctypes
import sys


from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
    resolve_key,
)
from meshagent.api import (
    ParticipantToken,
    ApiScope,
)
from meshagent.cli.common_options import OutputFormatOption

from pydantic_yaml import parse_yaml_raw_as


from meshagent.cli.call import _make_call

app = async_typer.AsyncTyper(help="Manage services for your project")


@app.async_command("create")
async def service_create(
    *,
    project_id: ProjectIdOption = None,
    file: Annotated[
        str,
        typer.Option("--file", "-f", help="File path to a service definition"),
    ],
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room", "-r", help="The name of a room to create the service for"
        ),
    ] = None,
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ServiceSpec, f.read())

            if spec.id is not None:
                print("[red]id cannot be set when creating a service[/red]")
                raise typer.Exit(code=1)

        try:
            if room is None:
                new_id = await client.create_service(
                    project_id=project_id, service=spec
                )
            else:
                new_id = await client.create_room_service(
                    project_id=project_id, service=spec, room_name=room
                )
        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created service:[/] {new_id}")

    finally:
        await client.close()


@app.async_command("update")
async def service_update(
    *,
    project_id: ProjectIdOption = None,
    id: Optional[str] = None,
    file: Annotated[
        str,
        typer.Option("--file", "-f", help="File path to a service definition"),
    ],
    create: Annotated[
        Optional[bool],
        typer.Option(
            help="create the service if it does not exist",
        ),
    ] = False,
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room", "-r", help="The name of a room to update the service for"
        ),
    ] = None,
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ServiceSpec, f.read())
            if spec.id is not None:
                id = spec.id

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

            if id is None and not create:
                print("[red]pass a service id or specify --create[/red]")
                raise typer.Exit(code=1)

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
            print(f"[green]Updated service:[/] {id}")

    finally:
        await client.close()


@app.async_command("run")
async def service_run(
    *,
    project_id: ProjectIdOption = None,
    command: str,
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

            async def run_service(port: int):
                if command.endswith(".py"):
                    code, output = await _run_process(
                        cmd=shlex.split("python3 " + command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                elif command.endswith(".dart"):
                    code, output = await _run_process(
                        cmd=shlex.split("dart run " + command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                else:
                    code, output = await _run_process(
                        cmd=shlex.split(command),
                        log=True,
                        env={**os.environ, "MESHAGENT_PORT": str(port)},
                    )

                if code != 0:
                    print(f"[red]{output}[/red]")

            run_tasks.append(asyncio.create_task(run_service(port)))

            async def get_spec(port: int, attempt=0) -> ServiceSpec:
                import aiohttp

                max_attempts = 10

                url = f"http://localhost:{port}{well_known_service_path}"

                async with aiohttp.ClientSession() as session:
                    try:
                        res = await session.get(url=url)
                        res.raise_for_status()

                        spec_json = await res.json()

                        return ServiceSpec.model_validate(spec_json)

                    except Exception:
                        if attempt < max_attempts:
                            backoff = 0.1 * pow(2, attempt)
                            await asyncio.sleep(backoff)
                            return await get_spec(port, attempt + 1)
                        else:
                            print("[red]unable to read service spec[/red]")
                            raise typer.Exit(-1)

            print(f"getting spec {port}", flush=True)
            spec = await get_spec(port)

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

            await asyncio.gather(*run_tasks)

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


@app.async_command("show")
async def service_show(
    *,
    project_id: ProjectIdOption = None,
    service_id: Annotated[str, typer.Argument(help="ID of the service to show")],
):
    """Show a services for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        service = await client.get_service(
            project_id=project_id, service_id=service_id
        )  # → List[Service]
        print(service.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("list")
async def service_list(
    *,
    project_id: ProjectIdOption = None,
    o: OutputFormatOption = "table",
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room", "-r", help="The name of a room to list the services for"
        ),
    ] = None,
):
    """List all services for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        services: list[ServiceSpec] = (
            (await client.list_services(project_id=project_id))
            if room is None
            else (
                await client.list_room_services(project_id=project_id, room_name=room)
            )
        )

        if o == "json":
            print(
                {"services": [svc.model_dump(mode="json") for svc in services]}
            ).model_dump_json(indent=2)
        else:
            print_json_table(
                [
                    {
                        "id": svc.id,
                        "name": svc.metadata.name,
                        "image": svc.container.image
                        if svc.container is not None
                        else None,
                    }
                    for svc in services
                ],
                "id",
                "name",
                "image",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def service_delete(
    *,
    project_id: ProjectIdOption = None,
    service_id: Annotated[str, typer.Argument(help="ID of the service to delete")],
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room", "-r", help="The name of a room to delete the service for"
        ),
    ] = None,
):
    """Delete a service."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        if room is None:
            await client.delete_service(project_id=project_id, service_id=service_id)
        else:
            await client.delete_service(
                project_id=project_id, service_id=service_id, room_name=room
            )
        print(f"[green]Service {service_id} deleted.[/]")
    finally:
        await client.close()


async def _run_process(
    cmd: list[str], cwd=None, env=None, timeout: float | None = None, log: bool = False
) -> tuple[int, str]:
    """
    Spawn a process, stream its output line-by-line as it runs, and return its exit code.
    stdout+stderr are merged to preserve ordering.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_preexec_fn,
    )

    _spawned.append(proc)

    output = []
    try:
        # Stream lines as they appear
        assert proc.stdout is not None
        while True:
            line = (
                await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if timeout
                else await proc.stdout.readline()
            )
            if not line:
                break
            ln = line.decode(errors="replace").rstrip()
            if log:
                print(ln, flush=True)
            output.append(ln)  # or send to a logger/queue

        return await proc.wait(), "".join(output)
    except asyncio.TimeoutError:
        # Graceful shutdown on timeout
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise


# Linux-only: send SIGTERM to child if parent dies
_PRCTL_AVAILABLE = sys.platform.startswith("linux")
if _PRCTL_AVAILABLE:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    PR_SET_PDEATHSIG = 1


def _preexec_fn():
    # Make child the leader of a new session/process group
    os.setsid()
    # On Linux, ensure child gets SIGTERM if parent dies unexpectedly
    if _PRCTL_AVAILABLE:
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
            err = ctypes.get_errno()
            raise OSError(err, "prctl(PR_SET_PDEATHSIG) failed")


_spawned = []


def _cleanup():
    # Kill each child's process group (created by setsid)
    for p in _spawned:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass


atexit.register(_cleanup)
