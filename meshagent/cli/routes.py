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
from meshagent.api.specs.service import RouteContentSpec, RouteCorsRule, RouteSpec
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


def _parse_cors(cors: Optional[str]) -> Optional[list[RouteCorsRule]]:
    if cors is None:
        return None
    if cors.strip() == "":
        return []
    try:
        value = json.loads(cors)
        if not isinstance(value, list):
            raise ValueError("expected a JSON array")
        return [RouteCorsRule.model_validate(rule) for rule in value]
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
        raise typer.BadParameter("Invalid JSON for --cors") from exc


def _parse_compression(compression: Optional[str]) -> Optional[str]:
    if compression is None:
        return None
    normalized = compression.strip().lower()
    if normalized not in {"brotli", "gzip", "none"}:
        raise typer.BadParameter(
            "Invalid value for --compression: expected brotli, gzip, or none"
        )
    return normalized


def _parse_route_path(path: str) -> str:
    if not path.startswith("/"):
        raise typer.BadParameter("RouteSpec paths must start with /")
    return path


def _content_options_supplied(
    *,
    content_path: Optional[str],
    cors: Optional[str],
    index: Optional[bool],
    iap: Optional[bool],
    compression: Optional[str],
) -> bool:
    return any(
        value is not None for value in (content_path, cors, index, iap, compression)
    )


def _route_table_row(route) -> dict[str, object]:
    paths = route.spec.paths
    content = [path.targetContent for path in paths if path.targetContent is not None]
    return {
        "domain": route.domain,
        "backend": route.spec.room_name or route.spec.agent_name or "",
        "path": ", ".join(path.path for path in paths),
        "port": ", ".join(
            str(path.targetPort) for path in paths if path.targetPort is not None
        ),
        "content_path": ", ".join(item.subpath or "/" for item in content),
        "index": ", ".join(str(item.index).lower() for item in content),
        "iap": ", ".join(str(item.iap).lower() for item in content),
        "compression": ", ".join(item.compression for item in content),
        "cors": " | ".join(
            json.dumps(
                [rule.model_dump(mode="json") for rule in item.cors],
                separators=(",", ":"),
            )
            for item in content
        ),
    }


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
    path: Annotated[
        str,
        typer.Option("--path", help="Public URL path to expose"),
    ] = "/",
    content_path: Annotated[
        Optional[str],
        typer.Option(
            "--content-path",
            "--room-path",
            help="Room storage subpath to serve directly",
        ),
    ] = None,
    cors: Annotated[
        Optional[str],
        typer.Option("--cors", help="CORS rules as a JSON array"),
    ] = None,
    index: Annotated[
        Optional[bool],
        typer.Option("--index/--no-index", help="Serve index.html for directories"),
    ] = None,
    iap: Annotated[
        Optional[bool],
        typer.Option("--iap/--no-iap", help="Require identity-aware proxy access"),
    ] = None,
    compression: Annotated[
        Optional[str],
        typer.Option("--compression", help="brotli, gzip, or none"),
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
            content_options = _content_options_supplied(
                content_path=content_path,
                cors=cors,
                index=index,
                iap=iap,
                compression=compression,
            )
            if domain is None or (port is None and not content_options):
                raise typer.BadParameter(
                    "Provide --file or --domain and one of --port or --content-path"
                )
            if port is not None and content_options:
                raise typer.BadParameter(
                    "--port cannot be combined with room content options"
                )
            if content_options and content_path is None:
                raise typer.BadParameter(
                    "Provide --content-path when using room content options"
                )
            room = resolve_room(room)
            if room is None:
                print(
                    "[red]Room name not specified, pass --room or set MESHAGENT_ROOM[/red]"
                )
                raise typer.Exit(code=1)
            parsed_annotations = _parse_annotations(annotations) or {}
            target: dict[str, object]
            if content_path is not None:
                target = {
                    "targetContent": RouteContentSpec(
                        subpath=content_path,
                        cors=_parse_cors(cors) or [],
                        index=index or False,
                        iap=iap or False,
                        compression=_parse_compression(compression) or "brotli",
                    )
                }
            else:
                target = {"targetPort": port}
            spec = RouteSpec.model_validate(
                {
                    "metadata": {"name": domain, "annotations": parsed_annotations},
                    "domain": domain,
                    "backend": {"room": {"name": room}},
                    "paths": [
                        {
                            "path": _parse_route_path(path),
                            "pathType": "prefix",
                            **target,
                        }
                    ],
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
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="Public URL path to expose"),
    ] = None,
    content_path: Annotated[
        Optional[str],
        typer.Option(
            "--content-path",
            "--room-path",
            help="Room storage subpath to serve directly",
        ),
    ] = None,
    cors: Annotated[
        Optional[str],
        typer.Option("--cors", help="CORS rules as a JSON array"),
    ] = None,
    index: Annotated[
        Optional[bool],
        typer.Option("--index/--no-index", help="Serve index.html for directories"),
    ] = None,
    iap: Annotated[
        Optional[bool],
        typer.Option("--iap/--no-iap", help="Require identity-aware proxy access"),
    ] = None,
    compression: Annotated[
        Optional[str],
        typer.Option("--compression", help="brotli, gzip, or none"),
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
            parsed_cors = _parse_cors(cors)
            parsed_compression = _parse_compression(compression)
            content_options = _content_options_supplied(
                content_path=content_path,
                cors=cors,
                index=index,
                iap=iap,
                compression=compression,
            )
            if port is not None and content_options:
                raise typer.BadParameter(
                    "--port cannot be combined with room content options"
                )
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
            if room == "":
                raise typer.BadParameter("Provide --room for a room route")
            parsed_annotations = (
                parsed_annotations
                if parsed_annotations is not None
                else route.annotations
            )
            current_path = route.spec.paths[0] if route.spec.paths else None
            route_path = (
                _parse_route_path(path)
                if path is not None
                else current_path.path
                if current_path is not None
                else "/"
            )
            if port is not None:
                target: dict[str, object] = {"targetPort": port}
                strip_prefix = (
                    current_path.stripPrefix if current_path is not None else False
                )
            elif content_options:
                current_content = (
                    current_path.targetContent if current_path is not None else None
                )
                if content_path is None and current_content is None:
                    raise typer.BadParameter(
                        "Provide --content-path when using room content options"
                    )
                target = {
                    "targetContent": RouteContentSpec(
                        subpath=(
                            content_path
                            if content_path is not None
                            else current_content.subpath
                            if current_content is not None
                            else ""
                        ),
                        cors=(
                            parsed_cors
                            if parsed_cors is not None
                            else current_content.cors
                            if current_content is not None
                            else []
                        ),
                        index=(
                            index
                            if index is not None
                            else current_content.index
                            if current_content is not None
                            else False
                        ),
                        iap=(
                            iap
                            if iap is not None
                            else current_content.iap
                            if current_content is not None
                            else False
                        ),
                        compression=(
                            parsed_compression
                            if parsed_compression is not None
                            else current_content.compression
                            if current_content is not None
                            else "brotli"
                        ),
                    )
                }
                strip_prefix = False
            else:
                if current_path is None:
                    raise typer.BadParameter(
                        "Provide --port or --content-path for a room route"
                    )
                if current_path.targetPort is not None:
                    target = {"targetPort": current_path.targetPort}
                elif current_path.targetContent is not None:
                    target = {"targetContent": current_path.targetContent}
                else:
                    raise typer.BadParameter(
                        "Provide --port or --content-path for a room route"
                    )
                strip_prefix = current_path.stripPrefix
            paths = [
                {
                    "path": route_path,
                    "pathType": (
                        current_path.pathType if current_path is not None else "prefix"
                    ),
                    "stripPrefix": strip_prefix,
                    **target,
                },
                *[item.model_dump(mode="python") for item in route.spec.paths[1:]],
            ]
            spec = RouteSpec.model_validate(
                {
                    "metadata": {"name": domain, "annotations": parsed_annotations},
                    "domain": domain,
                    "backend": {"room": {"name": room}},
                    "paths": paths,
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
                [_route_table_row(route) for route in routes],
                "domain",
                "backend",
                "path",
                "port",
                "content_path",
                "index",
                "iap",
                "compression",
                "cors",
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
