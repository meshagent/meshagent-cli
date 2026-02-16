from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import inspect
import os
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional, cast

import typer
import yaml
from aiohttp import web
from pydantic import BaseModel, ConfigDict, ValidationError
from rich import print

from meshagent.api import (
    ApiScope,
    ParticipantToken,
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.client import ConflictError
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.specs.service import AgentSpec, ANNOTATION_AGENT_TYPE, PortSpec
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    cleanup_args,
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.host import get_deferred, get_service, run_services, service_specs


app = async_typer.AsyncTyper(help="Run a webserver connected to a room")


@dataclass(frozen=True)
class StaticRoute:
    path: str
    source: Path


@dataclass(frozen=True)
class PythonRoute:
    path: str
    source: Path
    methods: list[str]


@dataclass(frozen=True)
class LoadedPythonRoute:
    path: str
    source: Path
    methods: list[str]
    handler: Callable[..., Any]


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    methods: list[str] | None = None
    python: str | None = None
    static: str | None = None


class RoutesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["Routes"]
    version: Literal["v1"]
    routes: list[RouteConfig]


def _normalize_methods(*, methods: list[str], source: str) -> list[str]:
    if len(methods) == 0:
        raise typer.BadParameter(f"{source} must include at least one HTTP method")

    normalized: list[str] = []
    for method in methods:
        if not isinstance(method, str):
            raise typer.BadParameter(f"{source} contains a non-string HTTP method")
        value = method.strip().upper()
        if not value:
            raise typer.BadParameter(f"{source} contains an empty HTTP method")
        normalized.append(value)
    return normalized


def _resolve_path(*, raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _load_routes_file(
    *,
    routes_file: str,
) -> tuple[list[StaticRoute], list[PythonRoute]]:
    routes_path = Path(routes_file).expanduser().resolve()
    if not routes_path.exists():
        raise typer.BadParameter(f"Routes file not found: {routes_path}")
    if not routes_path.is_file():
        raise typer.BadParameter(f"Routes file must be a file: {routes_path}")

    try:
        raw = yaml.safe_load(routes_path.read_text())
    except yaml.YAMLError as exc:
        raise typer.BadParameter(
            f"Unable to parse routes file {routes_path}: {exc}"
        ) from exc

    if raw is None:
        raw = {}

    try:
        config = RoutesConfig.model_validate(raw)
    except ValidationError as exc:
        raise typer.BadParameter(f"Invalid routes file {routes_path}:\n{exc}") from exc

    static_routes: list[StaticRoute] = []
    python_routes: list[PythonRoute] = []
    for index, route in enumerate(config.routes):
        source_label = f"--routes-file {routes_path} routes[{index}]"
        route_path = route.path.strip()
        if not route_path.startswith("/"):
            raise typer.BadParameter(
                f"{source_label}.path must start with '/' (got {route.path!r})"
            )

        has_python = route.python is not None
        has_static = route.static is not None
        if has_python == has_static:
            raise typer.BadParameter(
                f"{source_label} must define exactly one of 'python' or 'static'"
            )

        if route.static is not None:
            if route.methods is not None:
                raise typer.BadParameter(
                    f"{source_label}.methods is only valid for python routes"
                )
            static_path = route.static.strip()
            if not static_path:
                raise typer.BadParameter(
                    f"{source_label}.static must be a non-empty path"
                )
            static_routes.append(
                StaticRoute(
                    path=route_path,
                    source=_resolve_path(
                        raw_path=static_path,
                        base_dir=routes_path.parent,
                    ),
                )
            )
            continue

        python_path = cast(str, route.python).strip()
        if not python_path:
            raise typer.BadParameter(f"{source_label}.python must be a non-empty path")
        methods = _normalize_methods(
            methods=["GET"] if route.methods is None else route.methods,
            source=f"{source_label}.methods",
        )
        python_routes.append(
            PythonRoute(
                path=route_path,
                source=_resolve_path(
                    raw_path=python_path,
                    base_dir=routes_path.parent,
                ),
                methods=methods,
            )
        )

    return static_routes, python_routes


def _resolve_routes(
    *,
    routes_file: str,
) -> tuple[list[StaticRoute], list[PythonRoute]]:
    return _load_routes_file(routes_file=routes_file)


def _load_python_routes(routes: list[PythonRoute]) -> list[LoadedPythonRoute]:
    loaded: list[LoadedPythonRoute] = []
    for route in routes:
        handler = _load_python_handler(route.source)
        loaded.append(
            LoadedPythonRoute(
                path=route.path,
                source=route.source,
                methods=route.methods,
                handler=handler,
            )
        )
    return loaded


def _load_python_handler(path: Path) -> Callable[..., Any]:
    if not path.exists():
        raise typer.BadParameter(f"Python route file not found: {path}")
    if path.suffix != ".py":
        raise typer.BadParameter(f"Python route must point to a .py file: {path}")

    module_name = f"meshagent_webserver_{path.stem}_{abs(hash(str(path)))}"
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"Unable to import python route file: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    handler = getattr(module, "handler", None)
    if handler is None:
        raise typer.BadParameter(
            f"Python route file must define 'handler(*, room, req)': {path}"
        )
    if not callable(handler):
        raise typer.BadParameter(
            f"Python route file 'handler' must be callable: {path}"
        )

    if hasattr(module, "METHODS") or hasattr(module, "METHOD"):
        raise typer.BadParameter(
            f"Python route file must not define METHOD/METHODS; configure methods in --routes-file instead: {path}"
        )

    return cast(Callable[..., Any], handler)


def _watch_paths(
    *,
    routes_file: str,
) -> list[Path]:
    routes_path = Path(routes_file).expanduser().resolve()
    paths = [routes_path]
    try:
        _, routes_python = _resolve_routes(routes_file=routes_file)
        for route in routes_python:
            paths.append(route.source)
    except typer.BadParameter:
        pass

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    return unique


def _collect_mtimes(paths: list[Path]) -> dict[str, float | None]:
    mtimes: dict[str, float | None] = {}
    for path in paths:
        mtimes[str(path)] = path.stat().st_mtime if path.exists() else None
    return mtimes


def build_webserver(
    *,
    routes_file: str,
    host: str,
    port: int,
):
    static_routes, python_routes = _resolve_routes(routes_file=routes_file)
    if len(static_routes) == 0 and len(python_routes) == 0:
        raise typer.BadParameter("No routes were defined")

    for route in static_routes:
        if not route.source.exists() or not route.source.is_dir():
            raise typer.BadParameter(
                f"File route path must be a directory: {route.source}"
            )

    loaded_python_routes = _load_python_routes(python_routes)

    class WebServer:
        def __init__(self):
            self._runner: web.AppRunner | None = None
            self._mounted: list[tuple[str, str]] = []

        @property
        def mounted(self) -> list[tuple[str, str]]:
            return list(self._mounted)

        async def start(self, *, room: RoomClient) -> None:
            app = web.Application()
            mounted: list[tuple[str, str]] = []

            for route in static_routes:
                app.router.add_static(route.path, route.source, show_index=True)
                mounted.append((route.path, f"static:{route.source}"))

            for route in loaded_python_routes:

                async def _wrapped(
                    request: web.Request,
                    _handler: Callable[..., Any] = route.handler,
                ) -> web.StreamResponse:
                    result = _handler(room=room, req=request)
                    if inspect.isawaitable(result):
                        result = await result
                    return result

                for method in route.methods:
                    app.router.add_route(method, route.path, _wrapped)
                mounted.append((route.path, f"python:{route.source}"))

            self._mounted = mounted
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, host, port)
            await site.start()
            print(f"Listening on http://{host}:{port}")

        async def stop(self) -> None:
            if self._runner is None:
                return
            await self._runner.cleanup()
            self._runner = None

    return WebServer


def _set_port_spec(spec, *, web_port: int) -> None:
    spec.ports = [
        PortSpec(
            num=web_port,
            type="http",
            public=True,
            published=True,
            liveness="/",
        )
    ]


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    key: Annotated[
        Optional[str], typer.Option("--key", help="an api key to sign the token with")
    ] = None,
    routes_file: Annotated[
        str,
        typer.Option(
            "--routes-file",
            help="Path to a routes.yaml file with route definitions",
        ),
    ],
    host: Annotated[str, typer.Option(help="Host to bind the server")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind the server")] = 8000,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Reload routes when the routes file or python handlers change",
        ),
    ] = False,
):
    room_name = resolve_room(room)
    if room_name is None:
        print("[red]--room is required (or set MESHAGENT_ROOM).[/red]")
        raise typer.Exit(1)

    key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if agent_name is None:
                print(
                    f"[red]--agent-name must be specified when {token_env} is not set[/red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        print("[green]Connecting to room...[/green]", flush=True)

        WebServer = build_webserver(
            routes_file=routes_file,
            host=host,
            port=port,
        )
        server = WebServer()
        server_ref = {"server": server}

        async def _watch_routes(*, room: RoomClient) -> None:
            route_paths = _watch_paths(routes_file=routes_file)
            if not route_paths:
                return

            previous = _collect_mtimes(route_paths)
            while True:
                await asyncio.sleep(0.5)
                route_paths = _watch_paths(routes_file=routes_file)
                current = _collect_mtimes(route_paths)
                if current == previous:
                    continue
                previous = current
                print("[yellow]Detected route changes, reloading...[/yellow]")
                try:
                    WebServer = build_webserver(
                        routes_file=routes_file,
                        host=host,
                        port=port,
                    )
                except typer.BadParameter as exc:
                    print(f"[red]Failed to reload routes: {exc}[/red]")
                    continue

                previous_server = server_ref["server"]
                await previous_server.stop()
                server_ref["server"] = WebServer()
                try:
                    await server_ref["server"].start(room=room)
                except Exception as exc:
                    print(f"[red]Failed to apply route changes: {exc}[/red]")
                    server_ref["server"] = previous_server
                    await server_ref["server"].start(room=room)
                    continue
                for route_path, source in server_ref["server"].mounted:
                    print(f"  {route_path} -> {source}")

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=jwt,
            )
        ) as client:
            watch_task: asyncio.Task | None = None
            await server.start(room=client)
            for route_path, source in server.mounted:
                print(f"  {route_path} -> {source}")

            try:
                if watch:
                    watch_task = asyncio.create_task(_watch_routes(room=client))
                print(
                    f"[green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/green]",
                    flush=True,
                )
                await client.protocol.wait_for_close()
            except KeyboardInterrupt:
                pass
            finally:
                if watch_task is not None:
                    watch_task.cancel()
                await server.stop()
    finally:
        await account_client.close()


@app.async_command("service")
async def service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    routes_file: Annotated[
        str,
        typer.Option(
            "--routes-file",
            help="Path to a routes.yaml file with route definitions",
        ),
    ],
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = "0.0.0.0",
    web_port: Annotated[int, typer.Option(help="Port to bind the webserver")] = 8000,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    service = get_service(host=cast(str, host), port=cast(int, port))

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "WebServer"})
    )

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    WebServer = build_webserver(
        routes_file=routes_file,
        host=web_host,
        port=web_port,
    )
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    if not get_deferred():
        await run_services()


@app.async_command("spec")
async def spec(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    routes_file: Annotated[
        str,
        typer.Option(
            "--routes-file",
            help="Path to a routes.yaml file with route definitions",
        ),
    ],
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = "0.0.0.0",
    web_port: Annotated[int, typer.Option(help="Port to bind the webserver")] = 8000,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    service = get_service(host=cast(str, host), port=cast(int, port))

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "WebServer"})
    )

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    WebServer = build_webserver(
        routes_file=routes_file,
        host=web_host,
        port=web_port,
    )
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=web_port)
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = (
        "us-central1-docker.pkg.dev/meshagent-public/images/cli:{SERVER_VERSION}-esgz"
    )
    spec.container.command = shlex.join(
        ["meshagent", "webserver", "service", *cleanup_args(sys.argv[2:])]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy")
async def deploy(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    routes_file: Annotated[
        str,
        typer.Option(
            "--routes-file",
            help="Path to a routes.yaml file with route definitions",
        ),
    ],
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = "0.0.0.0",
    web_port: Annotated[int, typer.Option(help="Port to bind the webserver")] = 8000,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
):
    project_id = await resolve_project_id(project_id=project_id)

    service = get_service(host=cast(str, host), port=cast(int, port))

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "WebServer"})
    )

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    WebServer = build_webserver(
        routes_file=routes_file,
        host=web_host,
        port=web_port,
    )
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=web_port)
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = (
        "us-central1-docker.pkg.dev/meshagent-public/images/cli:{SERVER_VERSION}-esgz"
    )
    spec.container.command = shlex.join(
        ["meshagent", "webserver", "service", *cleanup_args(sys.argv[2:])]
    )

    client = await get_client()
    try:
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

        except ConflictError:
            print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Deployed service:[/] {id}")

    finally:
        await client.close()
