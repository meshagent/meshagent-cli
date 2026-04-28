# meshagent/cli/routes.py

from __future__ import annotations

from typing import Annotated, Optional

import json
import os

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.api.client import ValidationErrorResponse
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, OutputFormatOption
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    resolve_room,
)

app = async_typer.AsyncTyper(help="Manage routes for your project")
MESHAGENT_APP_DOMAIN_SUFFIX = os.getenv("MESAHGENT_APP_DOMAIN_SUFFIX", ".meshagent.app")


def _validation_error_message(
    exc: ClientResponseError | ValidationErrorResponse,
) -> str:
    message = exc.message if isinstance(exc, ClientResponseError) else str(exc)
    for marker in ("body=", "body: "):
        if marker in message:
            message = message.split(marker, 1)[1]
            break
    if message.startswith("400: "):
        message = message.removeprefix("400: ")
    return message.strip()


def _parse_annotations(annotations: Optional[str]) -> Optional[dict[str, str]]:
    if annotations is None:
        return None
    if annotations.strip() == "":
        return {}
    try:
        return json.loads(annotations)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("Invalid JSON for --annotations") from exc


def _warn_if_non_meshagent_app_domain(domain: str) -> None:
    if domain.strip().lower().endswith(MESHAGENT_APP_DOMAIN_SUFFIX):
        return
    print(
        f"[yellow]Warning:[/] domain does not end with {MESHAGENT_APP_DOMAIN_SUFFIX}: {domain}"
    )


@app.async_command("create")
async def route_create(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[
        str,
        typer.Option(
            "--domain",
            "-d",
            help=(
                "Domain name to route (unique per project). Keep it short and "
                "DNS-safe; long room-name-derived domains may be rejected."
            ),
        ),
    ],
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
    port: Annotated[
        str,
        typer.Option(
            "--port",
            "-p",
            help="Published port to route to",
        ),
    ],
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-n",
            help=(
                'annotations in json format {"name":"value"}. When routing to a '
                "room service, include meshagent.service.id."
            ),
        ),
    ] = None,
):
    """Create a route attached to the project.

    Use a short, DNS-safe domain name that matches the suffix accepted by your
    environment. When routing to a room service, include the
    meshagent.service.id annotation so the route targets the created service.
    """
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)
        _warn_if_non_meshagent_app_domain(domain)
        try:
            parsed_annotations = _parse_annotations(annotations) or {}
            await client.create_route(
                project_id=project_id,
                domain=domain,
                room_name=room,
                port=port,
                annotations=parsed_annotations,
            )
        except (ClientResponseError, ValidationErrorResponse) as exc:
            status = exc.status if isinstance(exc, ClientResponseError) else 400
            if status == 409:
                print(f"[red]Route domain already in use:[/] {domain}")
                raise typer.Exit(code=1)
            if status == 400:
                print(f"[red]{_validation_error_message(exc)}[/]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Created route:[/] {domain}")
    finally:
        await client.close()


@app.async_command("update")
async def route_update(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[
        str,
        typer.Argument(help="Domain name to update"),
    ],
    room: Annotated[
        Optional[str],
        typer.Option(
            "--room",
            "-r",
            help="Room name to route traffic into",
        ),
    ] = os.getenv("MESHAGENT_ROOM"),
    port: Annotated[
        Optional[str],
        typer.Option(
            "--port",
            "-p",
            help="Published port to route to",
        ),
    ] = None,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-n",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Update a route configuration."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)
        _warn_if_non_meshagent_app_domain(domain)
        parsed_annotations = _parse_annotations(annotations)

        if room is None or port is None or parsed_annotations is None:
            try:
                route = await client.get_route(project_id=project_id, domain=domain)
            except (ClientResponseError, ValidationErrorResponse) as exc:
                status = exc.status if isinstance(exc, ClientResponseError) else 400
                if status == 404:
                    print(f"[red]Route not found:[/] {domain}")
                    raise typer.Exit(code=1)
                if status == 400:
                    print(f"[red]{_validation_error_message(exc)}[/]")
                    raise typer.Exit(code=1)
                raise
            room = room or route.room_name
            port = port or route.port
            parsed_annotations = (
                parsed_annotations
                if parsed_annotations is not None
                else route.annotations
            )

        try:
            await client.update_route(
                project_id=project_id,
                domain=domain,
                room_name=room,
                port=port,
                annotations=parsed_annotations,
            )
        except (ClientResponseError, ValidationErrorResponse) as exc:
            status = exc.status if isinstance(exc, ClientResponseError) else 400
            if status == 404:
                print(f"[red]Route not found:[/] {domain}")
                raise typer.Exit(code=1)
            if status == 400:
                print(f"[red]{_validation_error_message(exc)}[/]")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Updated route:[/] {domain}")
    finally:
        await client.close()


@app.async_command("show")
async def route_show(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[str, typer.Argument(help="Domain name to show")],
):
    """Show route details."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        try:
            route = await client.get_route(project_id=project_id, domain=domain)
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Route not found:[/] {domain}")
                raise typer.Exit(code=1)
            raise
        print(route.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("list")
async def route_list(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
    ] = os.getenv("MESHAGENT_ROOM"),
    filter: Annotated[
        Optional[str], typer.Option("--filter", help="Lowercase contains filter")
    ] = None,
    count: Annotated[
        int, typer.Option("--count", help="Maximum number of routes to return")
    ] = 100,
    offset: Annotated[
        int, typer.Option("--offset", help="Row offset for pagination")
    ] = 0,
    o: OutputFormatOption = "table",
):
    """List routes for the project."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        room = resolve_room(room)

        if room is not None:
            routes = await client.list_room_routes(
                project_id=project_id,
                room_name=room,
                count=count,
                offset=offset,
                filter=filter,
            )
        else:
            routes = await client.list_routes(
                project_id=project_id,
                count=count,
                offset=offset,
                filter=filter,
            )

        if o == "json":
            print({"routes": [route.model_dump(mode="json") for route in routes]})
        else:
            print_json_table(
                [
                    {
                        "domain": route.domain,
                        "room": route.room_name,
                        "port": route.port,
                    }
                    for route in routes
                ],
                "domain",
                "room",
                "port",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def route_delete(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[str, typer.Argument(help="Domain name to delete")],
):
    """Delete a route."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        try:
            await client.delete_route(project_id=project_id, domain=domain)
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Route not found:[/] {domain}")
                raise typer.Exit(code=1)
            raise
        else:
            print(f"[green]Route deleted:[/] {domain}")
    finally:
        await client.close()
