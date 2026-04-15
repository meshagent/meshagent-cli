from rich import print
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from meshagent.api.room_server_client import ServicesClient, ListServicesResult
from meshagent.api.specs.service import ServiceSpec
from datetime import datetime
from typing import Annotated, Optional
import typer


app = async_typer.AsyncTyper(help="Manage services inside a room")


@app.async_command("list", help="List services running in a room")
async def room_services_list_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    output: OutputFormatOption = "table",
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            print("[bold green]Fetching services...[/bold green]")
            services_client = ServicesClient(room=client)
            result: ListServicesResult = await services_client.list_with_state()
            services: list[ServiceSpec] = result.services

            def format_unix_timestamp(value: float | None) -> str | None:
                if value is None:
                    return None
                return datetime.fromtimestamp(value).isoformat()

            rows = []
            for svc in services:
                state = result.service_states.get(svc.id or "")
                rows.append(
                    {
                        "id": svc.id,
                        "name": svc.metadata.name,
                        "image": svc.container.image
                        if svc.container is not None
                        else None,
                        "state": state.state if state is not None else None,
                        "container_id": state.container_id
                        if state is not None
                        else None,
                        "restart_scheduled_at": format_unix_timestamp(
                            state.restart_scheduled_at if state is not None else None
                        ),
                        "started_at": format_unix_timestamp(
                            state.started_at if state is not None else None
                        ),
                        "restart_count": (
                            state.restart_count if state is not None else None
                        ),
                        "last_exit_code": (
                            state.last_exit_code if state is not None else None
                        ),
                    }
                )

            if output == "json":
                print(
                    {
                        "services": [svc.model_dump(mode="json") for svc in services],
                        "service_states": {
                            sid: state.model_dump(mode="json")
                            for sid, state in result.service_states.items()
                        },
                    }
                )
            else:
                print_json_table(
                    rows,
                    "id",
                    "name",
                    "image",
                    "state",
                    "container_id",
                    "started_at",
                    "restart_scheduled_at",
                    "restart_count",
                    "last_exit_code",
                )
    finally:
        await account_client.close()


@app.async_command(
    "restart",
    help="Restart a running room service by stopping its current container.",
)
async def room_services_restart_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    service_id: Annotated[
        Optional[str], typer.Option("--id", help="Service ID to restart")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option("--name", help="Service name to restart")
    ] = None,
):
    if (service_id is None and name is None) or (service_id and name):
        raise typer.BadParameter("Provide exactly one of --id or --name")

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        connection = await account_client.connect_room(project_id=project_id, room=room)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory()
        ) as client:
            services_client = ServicesClient(room=client)
            result: ListServicesResult = await services_client.list_with_state()
            services: list[ServiceSpec] = result.services

            service: ServiceSpec | None = None
            if service_id is not None:
                service = next((svc for svc in services if svc.id == service_id), None)
            elif name is not None:
                service = next(
                    (svc for svc in services if svc.metadata.name == name),
                    None,
                )

            if service is None:
                target = service_id if service_id is not None else name
                print(f"[red]Service not found:[/] {target}")
                raise typer.Exit(code=1)

            if service.id is None:
                print("[red]Service does not have an id and cannot be restarted.[/red]")
                raise typer.Exit(code=1)

            await services_client.restart(service_id=service.id)
            print(
                f"[green]Restart requested:[/] {service.metadata.name} ({service.id})"
            )
    finally:
        await account_client.close()
