# meshagent/cli/routes.py

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import json
import os

import typer
from aiohttp import ClientResponseError
from pydantic import ValidationError
from rich import print

from meshagent.api.client import ValidationErrorResponse
from meshagent.api.specs.service import RouteSpec
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


def _load_route_spec(path: str) -> RouteSpec:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
        if path.endswith(".json"):
            return RouteSpec.model_validate_json(text)
        return RouteSpec.from_yaml(text)
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read route spec: {exc}") from exc
    except ValidationError as exc:
        raise typer.BadParameter(f"Invalid RouteSpec: {exc}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Invalid route spec: {exc}") from exc


def _warn_if_non_meshagent_app_domain(domain: str) -> None:
    if domain.strip().lower().endswith(MESHAGENT_APP_DOMAIN_SUFFIX):
        return
    print(
        f"[yellow]Warning:[/] domain does not end with {MESHAGENT_APP_DOMAIN_SUFFIX}: {domain}"
    )


async def _list_routes_view(
    client,
    *,
    project_id: str,
    count: int,
    offset: int,
    filter: str | None,
):
    routes = []
    continuation_token: str | None = None
    while len(routes) < offset + count:
        page = await client.list_routes_page(
            project_id=project_id,
            page_size=min(max(offset + count - len(routes), 1), 100),
            continuation_token=continuation_token,
            filter=filter,
        )
        routes.extend(page.routes)
        continuation_token = page.continuation_token
        if continuation_token is None:
            break
    return routes[offset : offset + count]


@app.async_command("create")
async def route_create(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[
        Optional[str],
        typer.Option(
            "--domain",
            "-d",
            help=(
                "Domain name to route (unique per project). Keep it short and "
                "DNS-safe; long room-name-derived domains may be rejected."
            ),
        ),
    ] = None,
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="Path to a RouteSpec YAML or JSON file"),
    ] = None,
    room: Annotated[
        Optional[str], typer.Option("--room", help="Room name")
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
        if file is None:
            if domain is None or port is None:
                raise typer.BadParameter("Provide --file or both --domain and --port")
            room = resolve_room(room)
            if room is None:
                print(
                    "[red]Room name not specified, pass --room or set MESHAGENT_ROOM[/red]"
                )
                raise typer.Exit(code=1)
            parsed_annotations = _parse_annotations(annotations) or {}
            spec = RouteSpec.model_validate(
                {
                    "metadata": {"name": domain, "annotations": parsed_annotations},
                    "domain": domain,
                    "backend": {"room": {"name": room}},
                    "paths": [{"path": "/", "pathType": "prefix", "targetPort": port}],
                }
            )
        else:
            spec = _load_route_spec(file)
            domain = spec.domain
        _warn_if_non_meshagent_app_domain(spec.domain)
        try:
            await client.create_route(
                project_id=project_id,
                spec=spec,
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
            print(f"[green]Created route:[/] {spec.domain}")
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
    file: Annotated[
        Optional[str],
        typer.Option("--file", "-f", help="Path to a RouteSpec YAML or JSON file"),
    ] = None,
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
        _warn_if_non_meshagent_app_domain(domain)
        if file is not None:
            spec = _load_route_spec(file)
        else:
            room = resolve_room(room)
            parsed_annotations = _parse_annotations(annotations)
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
            spec = RouteSpec.model_validate(
                {
                    "metadata": {"name": domain, "annotations": parsed_annotations},
                    "domain": domain,
                    "backend": {"room": {"name": room}},
                    "paths": [{"path": "/", "pathType": "prefix", "targetPort": port}],
                }
            )

        try:
            await client.update_route(
                project_id=project_id,
                domain=domain,
                spec=spec,
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


@app.async_command("get")
async def route_get(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[str, typer.Argument(help="Domain name to get")],
):
    """Get route details."""
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
            routes = await _list_routes_view(
                client,
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
                        "backend": route.spec.room_name or route.spec.agent_name or "",
                        "port": route.port,
                    }
                    for route in routes
                ],
                "domain",
                "backend",
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
