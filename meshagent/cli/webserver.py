from __future__ import annotations

import asyncio
import builtins
from dataclasses import dataclass
import importlib.util
import inspect
import os
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional, TypeVar, cast

import click
import typer
import yaml
from aiohttp import web
from click.core import ParameterSource
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
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


DEFAULT_ROUTES_FILE = "webserver.yaml"
DEFAULT_LOCAL_BIND_HOST = "127.0.0.1"
DEFAULT_SERVICE_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8000
ROUTE_ADD_COMMANDS = [
    "meshagent webserver add --path / --python handlers/home.py",
    "meshagent webserver add --path /assets --static ./public",
]
ROUTE_ADD_USAGE_TEXT = "Add routes with the CLI:\n" + "\n".join(
    f"  {command}" for command in ROUTE_ADD_COMMANDS
)
WEBSERVER_APP_HELP = f"""Run an HTTP webserver connected to a MeshAgent room.

The webserver mounts static folders and python handlers from a routes file.
Python handlers run with access to the active room and request objects.
This lets you build web applications that take advantage of the MeshAgent
room's full feature set.

Default routes file: webserver.yaml

{ROUTE_ADD_USAGE_TEXT}

Example routes file:
kind: WebServer
version: v1
host: 0.0.0.0
port: 8000
routes:
  - path: /
    methods:
      - GET
    python: handlers/home.py
  - path: /assets
    static: ./public

Example python handler (handlers/home.py):
from aiohttp import web
from meshagent.api import RoomClient

async def handler(
    *,
    room: RoomClient,
    req: web.Request,
) -> web.StreamResponse:
    return web.Response(text="hello")
"""
ROUTES_FILE_HELP = (
    "Path to routes file (default: webserver.yaml). "
    "YAML format: kind: WebServer, version: v1, host?, port?, routes: "
    "[{path, methods?, python?|static?}]. "
    "When --host/--port (or --web-host/--web-port) are not explicitly set, "
    "host/port from the routes file are used. "
    "Python handler signature: "
    "handler(*, room: RoomClient, req: web.Request) -> web.StreamResponse. "
    "Do not define METHOD/METHODS in handler files."
)
PYTHON_HANDLER_TEMPLATE = """from aiohttp import web
from meshagent.api import RoomClient


async def handler(
    *,
    room: RoomClient,
    req: web.Request,
) -> web.StreamResponse:
    return web.Response(text="ok")
"""


app = async_typer.AsyncTyper(help=WEBSERVER_APP_HELP)


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


class WebServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["WebServer"]
    version: Literal["v1"]
    host: str | None = None
    port: int | None = None
    routes: list[RouteConfig]

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        host = value.strip()
        if not host:
            raise ValueError("host must be a non-empty string")
        return host

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 1 or value > 65535:
            raise ValueError("port must be between 1 and 65535")
        return value


def _routes_file_path(routes_file: str) -> Path:
    return Path(routes_file).expanduser().resolve()


def _new_routes_config() -> WebServerConfig:
    return WebServerConfig(
        kind="WebServer",
        version="v1",
        host=DEFAULT_SERVICE_BIND_HOST,
        port=DEFAULT_BIND_PORT,
        routes=[],
    )


T = TypeVar("T")


def _cli_override_or_none(*, value: T, option_name: str) -> T | None:
    context = click.get_current_context(silent=True)
    if context is None:
        return value
    if context.get_parameter_source(option_name) == ParameterSource.DEFAULT:
        return None
    return value


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


def _resolve_routes_config(
    *,
    config: WebServerConfig,
    routes_path: Path,
) -> tuple[list[StaticRoute], list[PythonRoute]]:
    static_routes: list[StaticRoute] = []
    python_routes: list[PythonRoute] = []
    for index, route in enumerate(config.routes):
        source_label = f"-f/--routes-file {routes_path} routes[{index}]"
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


def _load_routes_config_file(*, routes_path: Path) -> WebServerConfig:
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
        return WebServerConfig.model_validate(raw)
    except ValidationError as exc:
        raise typer.BadParameter(f"Invalid routes file {routes_path}:\n{exc}") from exc


def _load_routes_file(
    *,
    routes_file: str,
) -> tuple[list[StaticRoute], list[PythonRoute]]:
    routes_path = _routes_file_path(routes_file)
    config = _load_routes_config_file(routes_path=routes_path)
    return _resolve_routes_config(config=config, routes_path=routes_path)


def _write_routes_config(*, routes_path: Path, config: WebServerConfig) -> None:
    routes_path.parent.mkdir(parents=True, exist_ok=True)
    routes_path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        )
    )


def _load_or_init_routes_config(
    *,
    routes_path: Path,
    create_if_missing: bool,
) -> tuple[WebServerConfig, bool]:
    if routes_path.exists():
        return _load_routes_config_file(routes_path=routes_path), False
    if not create_if_missing:
        raise typer.BadParameter(f"Routes file not found: {routes_path}")

    config = _new_routes_config()
    _write_routes_config(routes_path=routes_path, config=config)
    return config, True


def _scaffold_python_handler(*, path: Path) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PYTHON_HANDLER_TEMPLATE)
    return True


def _print_file_contents(*, path: Path) -> None:
    try:
        contents = path.read_text()
    except OSError as exc:
        print(f"[yellow]Unable to read file contents:[/] {path} ({exc})")
        return

    print(f"[green]File contents:[/] {path}")
    builtins.print(contents if len(contents) > 0 else "(empty)")


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
            f"Python route file must define "
            f"'handler(*, room: RoomClient, req: web.Request)': {path}"
        )
    if not callable(handler):
        raise typer.BadParameter(
            f"Python route file 'handler' must be callable: {path}"
        )

    if hasattr(module, "METHODS") or hasattr(module, "METHOD"):
        raise typer.BadParameter(
            f"Python route file must not define METHOD/METHODS; configure methods in -f/--routes-file instead: {path}"
        )

    return cast(Callable[..., Any], handler)


def _resolve_web_bind(
    *,
    config: WebServerConfig,
    routes_path: Path,
    default_host: str,
    default_port: int,
    host_override: str | None,
    port_override: int | None,
) -> tuple[str, int]:
    source = f"-f/--routes-file {routes_path}"

    host = host_override
    if host is None:
        host = config.host
    if host is None:
        host = default_host
    host = host.strip()
    if not host:
        raise typer.BadParameter(f"{source} resolved host must be non-empty")

    port = port_override
    if port is None:
        port = config.port
    if port is None:
        port = default_port
    if port < 1 or port > 65535:
        raise typer.BadParameter(f"{source} resolved port must be between 1 and 65535")

    return host, port


def _resolve_validated_routes(
    *,
    config: WebServerConfig,
    routes_path: Path,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    static_routes, python_routes = _resolve_routes_config(
        config=config,
        routes_path=routes_path,
    )
    if len(static_routes) == 0 and len(python_routes) == 0:
        raise typer.BadParameter("No routes were defined")

    for route in static_routes:
        if not route.source.exists() or not route.source.is_dir():
            raise typer.BadParameter(
                f"File route path must be a directory: {route.source}"
            )

    loaded_python_routes = _load_python_routes(python_routes)
    return static_routes, loaded_python_routes


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


def _validate_routes_file(
    *,
    routes_file: str,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    routes_path = _routes_file_path(routes_file)
    config = _load_routes_config_file(routes_path=routes_path)
    return _resolve_validated_routes(config=config, routes_path=routes_path)


def build_webserver(
    *,
    routes_file: str,
    default_host: str,
    default_port: int,
    host_override: str | None,
    port_override: int | None,
) -> tuple[type[Any], str, int]:
    routes_path = _routes_file_path(routes_file)
    config = _load_routes_config_file(routes_path=routes_path)
    host, port = _resolve_web_bind(
        config=config,
        routes_path=routes_path,
        default_host=default_host,
        default_port=default_port,
        host_override=host_override,
        port_override=port_override,
    )
    static_routes, loaded_python_routes = _resolve_validated_routes(
        config=config,
        routes_path=routes_path,
    )

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

    return WebServer, host, port


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


@app.async_command("check")
async def check(
    *,
    routes_file: Annotated[
        str,
        typer.Option(
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
):
    """Validate a routes file and print the resolved routes."""
    static_routes, python_routes = _validate_routes_file(routes_file=routes_file)
    routes_path = _routes_file_path(routes_file)

    print(f"[green]Routes file is valid:[/] {routes_path}")
    print(f"  static routes: {len(static_routes)}")
    print(f"  python routes: {len(python_routes)}")

    for route in static_routes:
        print(f"  {route.path} -> static:{route.source}")

    for route in python_routes:
        methods = ",".join(route.methods)
        print(f"  {route.path} [{methods}] -> python:{route.source}")


@app.async_command("init")
async def init(
    *,
    routes_file: Annotated[
        str,
        typer.Option(
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing routes file",
        ),
    ] = False,
):
    """Create a routes file scaffold.

    Add routes with the CLI:
      meshagent webserver add --path / --python handlers/home.py
      meshagent webserver add --path /assets --static ./public
    """
    routes_path = _routes_file_path(routes_file)
    if routes_path.exists() and not force:
        print(
            f"[red]Routes file already exists: {routes_path}. Use --force to overwrite.[/red]"
        )
        raise typer.Exit(1)

    config = _new_routes_config()
    _write_routes_config(routes_path=routes_path, config=config)
    print(f"[green]Created routes file:[/] {routes_path}")
    _print_file_contents(path=routes_path)
    print(f"[green]{ROUTE_ADD_USAGE_TEXT}[/green]")


@app.async_command("add")
async def add(
    *,
    path: Annotated[
        str,
        typer.Option(
            "--path",
            help="Route path to add (must start with '/')",
        ),
    ],
    python_file: Annotated[
        Optional[str],
        typer.Option(
            "--python",
            help="Python handler path (relative to routes file); scaffolded if missing",
        ),
    ] = None,
    static_dir: Annotated[
        Optional[str],
        typer.Option(
            "--static",
            help="Static directory path (relative to routes file)",
        ),
    ] = None,
    method: Annotated[
        list[str],
        typer.Option(
            "--method",
            "-m",
            help="HTTP method for python routes (repeatable)",
        ),
    ] = [],
    routes_file: Annotated[
        str,
        typer.Option(
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
):
    """Add a route entry to the routes file."""
    routes_path = _routes_file_path(routes_file)
    config, created = _load_or_init_routes_config(
        routes_path=routes_path,
        create_if_missing=True,
    )
    if created:
        print(f"[yellow]Created missing routes file:[/] {routes_path}")

    route_path = path.strip()
    if not route_path.startswith("/"):
        raise typer.BadParameter("--path must start with '/'")

    has_python = python_file is not None
    has_static = static_dir is not None
    if has_python == has_static:
        raise typer.BadParameter("Specify exactly one of --python or --static")

    route: RouteConfig
    if has_static:
        if len(method) > 0:
            raise typer.BadParameter("--method is only valid with --python")
        static_value = cast(str, static_dir).strip()
        if not static_value:
            raise typer.BadParameter("--static must be a non-empty path")
        route = RouteConfig(path=route_path, static=static_value)
    else:
        python_value = cast(str, python_file).strip()
        if not python_value:
            raise typer.BadParameter("--python must be a non-empty path")

        python_path = _resolve_path(
            raw_path=python_value,
            base_dir=routes_path.parent,
        )
        if python_path.suffix != ".py":
            raise typer.BadParameter("--python must point to a .py file")

        scaffolded = _scaffold_python_handler(path=python_path)
        if scaffolded:
            print(f"[yellow]Scaffolded missing python handler:[/] {python_path}")
            _print_file_contents(path=python_path)

        _load_python_handler(python_path)

        route = RouteConfig(
            path=route_path,
            methods=_normalize_methods(
                methods=["GET"] if len(method) == 0 else method,
                source="--method",
            ),
            python=python_value,
        )

    config.routes.append(route)
    _resolve_routes_config(config=config, routes_path=routes_path)
    _write_routes_config(routes_path=routes_path, config=config)
    print(f"[green]Added route:[/] {route_path} -> {route.python or route.static}")
    if created:
        _print_file_contents(path=routes_path)


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
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    host: Annotated[
        str, typer.Option(help="Host to bind the server")
    ] = DEFAULT_LOCAL_BIND_HOST,
    port: Annotated[
        int, typer.Option(help="Port to bind the server")
    ] = DEFAULT_BIND_PORT,
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

        host_override = _cli_override_or_none(value=host, option_name="host")
        port_override = _cli_override_or_none(value=port, option_name="port")

        WebServer, _, _ = build_webserver(
            routes_file=routes_file,
            default_host=DEFAULT_LOCAL_BIND_HOST,
            default_port=DEFAULT_BIND_PORT,
            host_override=host_override,
            port_override=port_override,
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
                    WebServer, _, _ = build_webserver(
                        routes_file=routes_file,
                        default_host=DEFAULT_LOCAL_BIND_HOST,
                        default_port=DEFAULT_BIND_PORT,
                        host_override=host_override,
                        port_override=port_override,
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
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = DEFAULT_SERVICE_BIND_HOST,
    web_port: Annotated[
        int, typer.Option(help="Port to bind the webserver")
    ] = DEFAULT_BIND_PORT,
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

    web_host_override = _cli_override_or_none(value=web_host, option_name="web_host")
    web_port_override = _cli_override_or_none(value=web_port, option_name="web_port")

    WebServer, _, _ = build_webserver(
        routes_file=routes_file,
        default_host=DEFAULT_SERVICE_BIND_HOST,
        default_port=DEFAULT_BIND_PORT,
        host_override=web_host_override,
        port_override=web_port_override,
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
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = DEFAULT_SERVICE_BIND_HOST,
    web_port: Annotated[
        int, typer.Option(help="Port to bind the webserver")
    ] = DEFAULT_BIND_PORT,
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

    web_host_override = _cli_override_or_none(value=web_host, option_name="web_host")
    web_port_override = _cli_override_or_none(value=web_port, option_name="web_port")

    WebServer, _, resolved_web_port = build_webserver(
        routes_file=routes_file,
        default_host=DEFAULT_SERVICE_BIND_HOST,
        default_port=DEFAULT_BIND_PORT,
        host_override=web_host_override,
        port_override=web_port_override,
    )
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=resolved_web_port)
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
            "-f",
            "--routes-file",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = DEFAULT_SERVICE_BIND_HOST,
    web_port: Annotated[
        int, typer.Option(help="Port to bind the webserver")
    ] = DEFAULT_BIND_PORT,
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

    web_host_override = _cli_override_or_none(value=web_host, option_name="web_host")
    web_port_override = _cli_override_or_none(value=web_port, option_name="web_port")

    WebServer, _, resolved_web_port = build_webserver(
        routes_file=routes_file,
        default_host=DEFAULT_SERVICE_BIND_HOST,
        default_port=DEFAULT_BIND_PORT,
        host_override=web_host_override,
        port_override=web_port_override,
    )
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=resolved_web_port)
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
