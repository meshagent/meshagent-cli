# ---------------------------------------------------------------------------
#  Imports
# ---------------------------------------------------------------------------
import typer
from rich import print
from typing import Annotated, List, Optional, Dict
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption
from aiohttp import ClientResponseError
from datetime import datetime, timezone
from pydantic import PositiveInt
import pathlib
import pydantic
from typing import Literal
from meshagent.cli import async_typer
from meshagent.api.specs.service import (
    ServiceSpec,
    ServiceStorageMounts,
    RoomStorageMount,
)
import asyncio
import shlex


from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_api_key,
    resolve_room,
)
from meshagent.api import (
    ParticipantToken,
    RoomClient,
    WebSocketClientProtocol,
    websocket_room_url,
    meshagent_base_url,
    ApiScope,
)
from meshagent.cli.common_options import OutputFormatOption

from pydantic_yaml import parse_yaml_raw_as

# Pydantic basemodels
from meshagent.api.accounts_client import Service, Port, Services

from meshagent.cli.call import _make_call

app = async_typer.AsyncTyper(help="Manage services for your project")

# ---------------------------------------------------------------------------
#  Utilities
# ---------------------------------------------------------------------------


def _kv_to_dict(pairs: List[str]) -> Dict[str, str]:
    """Convert ["A=1","B=2"] → {"A":"1","B":"2"}."""
    out: Dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"'{p}' must be KEY=VALUE")
        k, v = p.split("=", 1)
        out[k] = v
    return out


class PortSpec(pydantic.BaseModel):
    """
    CLI schema for --port.
    Example:
        --port num=8080 type=webserver liveness=/health path=/agent participant_name=myname
    """

    num: PositiveInt | Literal["*"]
    type: Literal["mcp.sse", "meshagent.callable", "http", "tcp"]
    liveness: str | None = None
    participant_name: str | None = None
    path: str | None = None


def _parse_port_spec(spec: str) -> PortSpec:
    """
    Convert "num=8080 type=webserver liveness=/health" → PortSpec.
    The user should quote the whole string if it contains spaces.
    """
    tokens = spec.strip().split()
    kv: Dict[str, str] = {}
    for t in tokens:
        if "=" not in t:
            raise typer.BadParameter(
                f"expected num=PORT_NUMBER type=meshagent.callable|mcp.sse liveness=OPTIONAL_PATH, got '{t}'"
            )
        k, v = t.split("=", 1)
        kv[k] = v
    try:
        return PortSpec(**kv)
    except pydantic.ValidationError as exc:
        raise typer.BadParameter(str(exc))


# ---------------------------------------------------------------------------
#  Commands
# ---------------------------------------------------------------------------


@app.async_command("create")
async def service_create(
    *,
    project_id: ProjectIdOption = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    name: Annotated[Optional[str], typer.Option(help="Friendly service name")] = None,
    image: Annotated[
        Optional[str], typer.Option(help="Container image reference")
    ] = None,
    role: Annotated[
        Optional[str], typer.Option(help="Service role (agent|tool)")
    ] = None,
    pull_secret: Annotated[
        Optional[str],
        typer.Option("--pull-secret", help="Secret ID for registry"),
    ] = None,
    command: Annotated[
        Optional[str],
        typer.Option("--command", help="Override ENTRYPOINT/CMD"),
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    env_secret: Annotated[List[str], typer.Option("--env-secret")] = [],
    runtime_secret: Annotated[List[str], typer.Option("--runtime-secret")] = [],
    room_storage_path: Annotated[
        Optional[str],
        typer.Option("--mount", help="Path inside container to mount room storage"),
    ] = None,
    room_storage_subpath: Annotated[
        Optional[str],
        typer.Option(
            "--mount-subpath",
            help="Restrict the container's mount to a subpath within the room storage",
        ),
    ] = None,
    port: Annotated[
        List[str],
        typer.Option(
            "--port",
            "-p",
            help=(
                "Repeatable. Example:\n"
                '  -p "num=8080 type=[mcp.sse | meshagent.callable | http | tcp] liveness=/health path=/agent participant_name=myname"'
            ),
        ),
    ] = [],
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        if file is not None:
            with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
                spec = parse_yaml_raw_as(ServiceSpec, f.read())

                if spec.id is not None:
                    print("[red]id cannot be set when creating a service[/red]")
                    raise typer.Exit(code=1)

                service_obj = spec.to_service()

        else:
            # ✅ validate / coerce port specs
            port_specs: List[PortSpec] = [_parse_port_spec(s) for s in port]

            ports_dict = {
                ps.num: Port(
                    type=ps.type,
                    liveness_path=ps.liveness,
                    participant_name=ps.participant_name,
                    path=ps.path,
                )
                for ps in port_specs
            } or None

            storage = ServiceStorageMounts(room=[])

            if room_storage_path is not None:
                storage.room.append(
                    RoomStorageMount(
                        path=room_storage_path, subpath=room_storage_subpath
                    )
                )

            service_obj = Service(
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                role=role,
                image=image,
                command=command,
                pull_secret=pull_secret,
                environment=_kv_to_dict(env),
                environment_secrets=env_secret or None,
                runtime_secrets=_kv_to_dict(runtime_secret),
                ports=ports_dict,
                storage=storage,
            )

        try:
            new_id = (
                await client.create_service(project_id=project_id, service=service_obj)
            )["id"]
        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {service_obj.name}[/red]")
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
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ] = None,
    name: Annotated[Optional[str], typer.Option(help="Friendly service name")] = None,
    image: Annotated[
        Optional[str], typer.Option(help="Container image reference")
    ] = None,
    role: Annotated[
        Optional[str], typer.Option(help="Service role (agent|tool)")
    ] = None,
    pull_secret: Annotated[
        Optional[str],
        typer.Option("--pull-secret", help="Secret ID for registry"),
    ] = None,
    command: Annotated[
        Optional[str],
        typer.Option("--command", help="Override ENTRYPOINT/CMD"),
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    env_secret: Annotated[List[str], typer.Option("--env-secret")] = [],
    runtime_secret: Annotated[List[str], typer.Option("--runtime-secret")] = [],
    room_storage_path: Annotated[
        Optional[str],
        typer.Option("--mount", help="Path inside container to mount room storage"),
    ] = None,
    room_storage_subpath: Annotated[
        Optional[str],
        typer.Option(
            "--mount-subpath",
            help="Restrict the container's mount to a subpath within the room storage",
        ),
    ] = None,
    port: Annotated[
        List[str],
        typer.Option(
            "--port",
            "-p",
            help=(
                "Repeatable. Example:\n"
                '  -p "num=8080 type=[mcp.sse | meshagent.callable | http | tcp] liveness=/health path=/agent participant_name=myname"'
            ),
        ),
    ] = [],
    create: Annotated[
        Optional[bool],
        typer.Option(
            help="create the service if it does not exist",
        ),
    ] = False,
):
    """Create a service attached to the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)

        if file is not None:
            with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
                spec = parse_yaml_raw_as(ServiceSpec, f.read())
                if spec.id is not None:
                    id = spec.id
                service_obj = spec.to_service()

        else:
            # ✅ validate / coerce port specs
            port_specs: List[PortSpec] = [_parse_port_spec(s) for s in port]

            ports_dict = {
                ps.num: Port(
                    type=ps.type,
                    liveness_path=ps.liveness,
                    participant_name=ps.participant_name,
                    path=ps.path,
                )
                for ps in port_specs
            } or None

            storage = ServiceStorageMounts(room=[])

            if room_storage_path is not None:
                storage.room.append(
                    RoomStorageMount(
                        path=room_storage_path, subpath=room_storage_subpath
                    )
                )

            service_obj = Service(
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                role=role,
                image=image,
                command=command,
                pull_secret=pull_secret,
                environment=_kv_to_dict(env),
                environment_secrets=env_secret or None,
                runtime_secrets=_kv_to_dict(runtime_secret),
                ports=ports_dict,
                storage=storage,
            )

        try:
            if id is None:
                services = await client.list_services(project_id=project_id)
                for s in services:
                    if s.name == service_obj.name:
                        id = s.id

            if id is None and not create:
                print("[red]pass a service id or specify --create[/red]")
                raise typer.Exit(code=1)

            if id is None:
                id = (
                    await client.create_service(
                        project_id=project_id, service=service_obj
                    )
                )["id"]

            else:
                await client.update_service(
                    project_id=project_id, service_id=id, service=service_obj
                )

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Service name already in use: {service_obj.name}[/red]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated service:[/] {id}")

    finally:
        await client.close()


@app.async_command("test")
async def service_test(
    *,
    project_id: ProjectIdOption = None,
    api_key_id: ApiKeyIdOption = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="File path to a service definition"),
    ],
    room: Annotated[
        Optional[str],
        typer.Option(
            help="A room name to test the service in (must not be currently running)"
        ),
    ] = None,
    name: Annotated[Optional[str], typer.Option(help="Friendly service name")] = None,
    role: Annotated[
        Optional[str], typer.Option(help="Service role (agent|tool)")
    ] = None,
    image: Annotated[
        Optional[str], typer.Option(help="Container image reference")
    ] = None,
    pull_secret: Annotated[
        Optional[str],
        typer.Option("--pull-secret", help="Secret ID for registry"),
    ] = None,
    command: Annotated[
        Optional[str],
        typer.Option("--command", help="Override ENTRYPOINT/CMD"),
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    env_secret: Annotated[List[str], typer.Option("--env-secret")] = [],
    runtime_secret: Annotated[List[str], typer.Option("--runtime-secret")] = [],
    room_storage_path: Annotated[
        Optional[str],
        typer.Option("--mount", help="Path inside container to mount room storage"),
    ] = None,
    port: Annotated[
        List[str],
        typer.Option(
            "--port",
            "-p",
            help=(
                "Repeatable. Example:\n"
                '  -p "num=8080 type=[mcp.sse | meshagent.callable | http | tcp] liveness=/health path=/agent participant_name=myname"'
            ),
        ),
    ] = [],
    timeout: Annotated[
        Optional[int],
        typer.Option(
            "--timeout", help="The maximum time that this room should run (default 1hr)"
        ),
    ] = None,
):
    """Create a service attached to the project."""
    my_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)

        if room is None:
            print("[bold red]Room was not set[/bold red]")
            raise typer.Exit(1)

        if file is not None:
            with open(file, "rb") as f:
                service_obj = parse_yaml_raw_as(ServiceSpec, f.read()).to_service()

        else:
            # ✅ validate / coerce port specs
            port_specs: List[PortSpec] = [_parse_port_spec(s) for s in port]

            ports_dict = {
                str(ps.num): Port(
                    type=ps.type,
                    liveness_path=ps.liveness,
                    participant_name=ps.participant_name,
                    path=ps.path,
                )
                for ps in port_specs
            } or None

            service_obj = Service(
                created_at=datetime.now(timezone.utc).isoformat(),
                role=role,
                name=name,
                image=image,
                command=command,
                pull_secret=pull_secret,
                room_storage_path=room_storage_path,
                environment=_kv_to_dict(env),
                environment_secrets=env_secret or None,
                runtime_secrets=_kv_to_dict(runtime_secret),
                ports=ports_dict,
            )

        try:
            token = ParticipantToken(
                name=name, project_id=project_id, api_key_id=api_key_id
            )
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant("user")
            token.add_room_grant(room)
            token.extra_payload = {
                "max_runtime_seconds": timeout,  # run for 1 hr max
                "meshagent_dev_services": [service_obj.model_dump(mode="json")],
            }

            print("[bold green]Connecting to room...[/bold green]")

            key = (
                await my_client.decrypt_project_api_key(
                    project_id=project_id, id=api_key_id
                )
            )["token"]

            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(
                        room_name=room, base_url=meshagent_base_url()
                    ),
                    token=token.to_jwt(token=key),
                )
            ) as client:
                print(
                    f"[green]Your test room '{client.room_name}' has been started. It will time out after a few minutes if you do not join it.[/green]"
                )

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Room already in use: {room}[/red]")
                raise typer.Exit(code=1)
            raise

    finally:
        await my_client.close()


@app.async_command("run")
async def service_run(
    *,
    project_id: ProjectIdOption = None,
    api_key_id: ApiKeyIdOption = None,
    command: str,
    room: Annotated[
        Optional[str],
        typer.Option(
            help="A room name to test the service in (must not be currently running)"
        ),
    ] = None,
):
    """Create a service attached to the project."""
    my_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)
        room = resolve_room(room)

        if room is None:
            print("[bold red]Room was not set[/bold red]")
            raise typer.Exit(1)

        try:
            token = ParticipantToken(
                name="cli", project_id=project_id, api_key_id=api_key_id
            )
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant("user")
            token.add_room_grant(room)

            print("[bold green]Connecting to room...[/bold green]")

            status, output = await _run_process(
                cmd=shlex.split("python3 " + command + " --describe")
            )

            if status != 0:
                print(f"[red]{output}[/red]")
                exit(1)

            run_tasks = []

            async def run_service():
                code, output = await _run_process(
                    cmd=shlex.split("python3 " + command), log=True
                )

                if code != 0:
                    print(f"[red]{output}[/red]")

            run_tasks.append(asyncio.create_task(run_service()))

            spec = ServiceSpec.model_validate_json(output)

            for port in spec.ports:
                print(f"[bold green]Connecting port {port.num}...[/bold green]")

                for endpoint in port.endpoints:
                    print(
                        f"[bold green]Connecting endpoint {endpoint.path} as {endpoint.identity}...[/bold green]"
                    )

                    token = ParticipantToken(
                        name=endpoint.identity,
                        project_id=project_id,
                        api_key_id=api_key_id,
                    )
                    if endpoint.role is not None:
                        token.add_role_grant(role=endpoint.role)

                    token.add_room_grant(room)
                    if endpoint.api is not None:
                        token.add_api_grant(endpoint.api)
                    else:
                        token.add_api_grant(ApiScope.agent_default())

                    run_tasks.append(
                        asyncio.create_task(
                            _make_call(
                                room=room,
                                api_key_id=api_key_id,
                                project_id=project_id,
                                participant_name=endpoint.identity,
                                url=f"http://localhost:{port.num}{endpoint.path}",
                                arguments={},
                                token=endpoint.api,
                            )
                        )
                    )

            await asyncio.gather(*run_tasks)

        except ClientResponseError as exc:
            if exc.status == 409:
                print(f"[red]Room already in use: {room}[/red]")
                raise typer.Exit(code=1)
            raise

    finally:
        await my_client.close()


@app.async_command("show")
async def service_show(
    *,
    project_id: ProjectIdOption = None,
    service_id: Annotated[str, typer.Argument(help="ID of the service to delete")],
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
):
    """List all services for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        services: list[Service] = await client.list_services(
            project_id=project_id
        )  # → List[Service]

        if o == "json":
            print(Services(services=services).model_dump_json(indent=2))
        else:
            print_json_table(
                [svc.model_dump(mode="json") for svc in services], "id", "name", "image"
            )
    finally:
        await client.close()


@app.async_command("delete")
async def service_delete(
    *,
    project_id: ProjectIdOption = None,
    service_id: Annotated[str, typer.Argument(help="ID of the service to delete")],
):
    """Delete a service."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        await client.delete_service(project_id=project_id, service_id=service_id)
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
    )

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
