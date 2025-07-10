# ---------------------------------------------------------------------------
#  Imports
# ---------------------------------------------------------------------------
import typer
from rich import print
from typing import Annotated, List, Optional, Dict
from aiohttp import ClientResponseError
from datetime import datetime, timezone
from pydantic import PositiveInt
import pydantic
from typing import Literal
from meshagent.cli import async_typer
from pydantic import BaseModel
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_api_key,
)
from meshagent.api import (
    ParticipantToken,
    RoomClient,
    WebSocketClientProtocol,
    websocket_room_url,
    meshagent_base_url,
)

from pydantic_yaml import parse_yaml_raw_as

# Pydantic basemodels
from meshagent.api.accounts_client import Service, Port, Services, Endpoint


app = async_typer.AsyncTyper()

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

    num: PositiveInt
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
    project_id: str = None,
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
            with open(file, "rb") as f:
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

            service_obj = Service(
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                role=role,
                image=image,
                command=command,
                pull_secret=pull_secret,
                room_storage_path=room_storage_path,
                room_storage_subpath=room_storage_subpath,
                environment=_kv_to_dict(env),
                environment_secrets=env_secret or None,
                runtime_secrets=_kv_to_dict(runtime_secret),
                ports=ports_dict,
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
    project_id: str = None,
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
            with open(file, "rb") as f:
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

            service_obj = Service(
                created_at=datetime.now(timezone.utc).isoformat(),
                name=name,
                role=role,
                image=image,
                command=command,
                pull_secret=pull_secret,
                room_storage_path=room_storage_path,
                room_storage_subpath=room_storage_subpath,
                environment=_kv_to_dict(env),
                environment_secrets=env_secret or None,
                runtime_secrets=_kv_to_dict(runtime_secret),
                ports=ports_dict,
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


class ServicePortEndpointSpec(pydantic.BaseModel):
    path: str
    identity: str
    type: Optional[Literal["mcp.sse", "meshagent.callable", "http", "tcp"]] = None


class ServicePortSpec(pydantic.BaseModel):
    num: PositiveInt
    type: Literal["mcp.sse", "meshagent.callable", "http", "tcp"]
    endpoints: list[ServicePortEndpointSpec] = []
    liveness: Optional[str] = None


class ServiceSpec(BaseModel):
    version: Literal["v1"]
    kind: Literal["Service"]
    id: Optional[str] = None
    name: str
    command: Optional[str] = None
    image: str
    ports: Optional[list[ServicePortSpec]] = []
    role: Optional[Literal["user", "tool", "agent"]] = None
    environment: Optional[dict[str, str]] = {}
    secrets: list[str] = []
    pull_secret: Optional[str] = None
    room_storage_path: Optional[str] = None
    room_storage_subpath: Optional[str] = None

    def to_service(self):
        ports = {}
        for p in self.ports:
            port = Port(liveness_path=p.liveness, type=p.type, endpoints=[])
            for endpoint in p.endpoints:
                type = port.type
                if endpoint.type is not None:
                    type = endpoint.type

                port.endpoints.append(
                    Endpoint(
                        type=type,
                        participant_name=endpoint.identity,
                        path=endpoint.path,
                    )
                )
            ports[p.num] = port
        return Service(
            id="",
            created_at=datetime.now(timezone.utc).isoformat(),
            name=self.name,
            command=self.command,
            image=self.image,
            ports=ports,
            role=self.role,
            environment=self.environment,
            environment_secrets=self.secrets,
            pull_secret=self.pull_secret,
            room_storage_path=self.room_storage_path,
            room_storage_subpath=self.room_storage_subpath,
        )


@app.async_command("test")
async def service_test(
    *,
    project_id: str = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
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


@app.async_command("show")
async def service_show(
    *,
    project_id: str = None,
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
    project_id: str = None,
    o: Annotated[
        str, typer.Option("--output", "-o", help="output format [json|table]")
    ] = "table",
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
    project_id: Optional[str] = None,
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
