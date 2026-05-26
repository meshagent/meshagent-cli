from __future__ import annotations

import ast
import asyncio
import builtins
from collections.abc import Awaitable
from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
import inspect
import logging
import os
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable, Literal, Optional, TypeVar, cast

import typer
import yaml
from aiohttp import web
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from rich import print
from typer._click.core import ParameterSource
from typer._click.globals import get_current_context

from meshagent.api import (
    ApiScope,
    ParticipantToken,
    RoomClient,
    WebSocketClientProtocol,
)
from meshagent.api.client import ConflictError
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ANNOTATION_SERVICE_ID,
    ContainerMountSpec,
    EnvironmentVariable,
    PortSpec,
    RoomStorageMountSpec,
    ServiceSpec,
    TokenValue,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    cleanup_args,
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
    upload_room_bytes_stream,
)
from meshagent.cli.host import get_service, service_specs


DEFAULT_ROUTES_FILE = "webserver.yaml"
DEFAULT_LOCAL_BIND_HOST = "127.0.0.1"
DEFAULT_SERVICE_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8000
DEFAULT_WEBSITE_MOUNT_PATH = "/website"
EMPTY_DIR_MARKER_NAME = ".meshagent.keep"
DIRECTORY_INDEX_FILE_NAME = "index.html"
MESHAGENT_APP_DOMAIN_SUFFIX = ".meshagent.app"
ROUTE_ADD_COMMANDS = [
    "meshagent webserver add --path / --python handlers/home.py",
    "meshagent webserver add --path /assets --static ./public",
]
ROUTE_ADD_USAGE_TEXT = "Add routes with the CLI:\n" + "\n".join(
    f"  {command}" for command in ROUTE_ADD_COMMANDS
)
JOIN_COMMANDS = [
    "meshagent webserver join --room my-room --agent-name my-web -f webserver.yaml --watch",
]
JOIN_USAGE_TEXT = "Start it locally by joining a room:\n" + "\n".join(
    f"  {command}" for command in JOIN_COMMANDS
)
LOCAL_BROWSER_USAGE_TEXT = """View the site from your local machine:
  http://127.0.0.1:8000

Use the host/port from webserver.yaml (or explicit --host/--port or --web-host/--web-port overrides).
"""
EXTERNAL_ACCESS_COMMANDS = [
    "meshagent webserver deploy --service-name my-web --agent-name my-web --room my-room -f webserver.yaml --website-path /website --domain my-web.meshagent.app",
]
EXTERNAL_ACCESS_USAGE_TEXT = (
    """To make the site available outside your machine:
"""
    + "\n".join(f"  {command}" for command in EXTERNAL_ACCESS_COMMANDS)
    + """

`--domain` automatically creates or updates the route to the deployed room and webserver port.
If the domain already targets a different room, deploy fails to avoid accidental repoints.
Without `--domain`, create the route manually:
  meshagent route create --room my-room --port 8000 --domain my-web.meshagent.app
"""
)
WEBSERVER_USAGE_TEXT = "\n\n".join(
    [
        ROUTE_ADD_USAGE_TEXT,
        JOIN_USAGE_TEXT,
        LOCAL_BROWSER_USAGE_TEXT.strip(),
        EXTERNAL_ACCESS_USAGE_TEXT.strip(),
    ]
)
ROUTE_SOURCE_USAGE_TEXT = """Route source path rules:
- `python` and `static` sources that start with `/` resolve to `{cwd}/...`.
- `python` and `static` sources without a leading `/` resolve relative to the routes file.
- `static` supports both files and directories.
- Set `--app-dir` to control Python import root (defaults to routes file directory).
"""
WEBSERVER_APP_HELP = f"""Run an HTTP webserver connected to a MeshAgent room.

The webserver mounts static files/folders and python handlers from a routes file.
Python handlers run with access to the active room and request objects.
This lets you build web applications that take advantage of the MeshAgent
room's full feature set.

Default routes file: webserver.yaml

{WEBSERVER_USAGE_TEXT}

{ROUTE_SOURCE_USAGE_TEXT}

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
    "Route source resolution: '/x' -> '{cwd}/x'; 'x' -> relative to routes file. "
    "'static' supports files and directories. "
    "Use --app-dir to control Python import root (defaults to routes file directory). "
    "When --host/--port (or --web-host/--web-port) are not explicitly set, "
    "host/port from the routes file are used. "
    "Python handler signature: "
    "handler(*, room: RoomClient, req: web.Request) -> web.StreamResponse. "
    "Do not define METHOD/METHODS in handler files."
)
APP_DIR_HELP = (
    "Python import root for route handlers (similar to uvicorn --app-dir). "
    "Defaults to the routes file directory."
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
logger = logging.getLogger("meshagent.webserver")


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


@dataclass(frozen=True)
class WebsiteUploadFile:
    local_source: Path | None
    relative_path: str
    content: bytes | None = None


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


def _warn_if_non_meshagent_app_domain(domain: str) -> None:
    if domain.strip().lower().endswith(MESHAGENT_APP_DOMAIN_SUFFIX):
        return
    print(
        f"[yellow]Warning:[/] domain does not end with {MESHAGENT_APP_DOMAIN_SUFFIX}: {domain}"
    )


async def _upsert_domain_route(
    *,
    client: Any,
    project_id: str,
    domain: str,
    room_name: str,
    port: str,
    service_id: str,
) -> None:
    _warn_if_non_meshagent_app_domain(domain)
    route_annotations = {ANNOTATION_SERVICE_ID: service_id}
    try:
        await client.create_route(
            project_id=project_id,
            domain=domain,
            room_name=room_name,
            port=port,
            annotations=route_annotations,
        )
    except ConflictError:
        existing = await client.get_route(project_id=project_id, domain=domain)
        if existing.room_name != room_name:
            raise typer.BadParameter(
                f"--domain {domain} already routes to room {existing.room_name}. "
                f"Refusing to change it to room {room_name}. "
                "Use `meshagent route update` if you want to repoint it."
            )
        updated_annotations = dict(existing.annotations)
        updated_annotations[ANNOTATION_SERVICE_ID] = service_id
        if existing.port == port and existing.annotations == updated_annotations:
            print(f"[green]Route already configured:[/] {domain} -> {room_name}:{port}")
            return
        await client.update_route(
            project_id=project_id,
            domain=domain,
            room_name=room_name,
            port=port,
            annotations=updated_annotations,
        )
        print(f"[green]Updated route:[/] {domain} -> {room_name}:{port}")
    else:
        print(f"[green]Created route:[/] {domain} -> {room_name}:{port}")


T = TypeVar("T")


def _cli_override_or_none(*, value: T, option_name: str) -> T | None:
    context = get_current_context(silent=True)
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
    if path.is_absolute():
        # Route sources are always workspace-relative for consistency with website copy mode.
        return (Path.cwd() / path.as_posix().lstrip("/")).resolve()
    return (base_dir / path).resolve()


def _resolve_routes_config(
    *,
    config: WebServerConfig,
    routes_path: Path,
) -> tuple[list[StaticRoute], list[PythonRoute]]:
    static_routes: list[StaticRoute] = []
    python_routes: list[PythonRoute] = []
    for index, route in enumerate(config.routes):
        source_label = f"-f {routes_path} routes[{index}]"
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


def _resolve_app_dir_path(*, routes_path: Path, app_dir: str | None) -> Path:
    if app_dir is None:
        resolved = routes_path.parent.resolve()
    else:
        candidate = Path(app_dir).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (Path.cwd() / candidate).resolve()

    if not resolved.exists():
        raise typer.BadParameter(f"--app-dir was not found: {resolved}")
    if not resolved.is_dir():
        raise typer.BadParameter(f"--app-dir must be a directory: {resolved}")
    return resolved


@contextmanager
def _with_app_dir_on_syspath(*, app_dir_path: Path):
    app_dir_entry = str(app_dir_path)
    inserted = False
    if app_dir_entry not in sys.path:
        sys.path.insert(0, app_dir_entry)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(app_dir_entry)
            except ValueError:
                pass


def _load_python_routes(
    routes: list[PythonRoute], *, app_dir_path: Path, allow_runtime_fallback: bool
) -> list[LoadedPythonRoute]:
    loaded: list[LoadedPythonRoute] = []
    for route in routes:
        try:
            handler = _load_python_handler(route.source, app_dir_path=app_dir_path)
        except typer.BadParameter as exc:
            if not allow_runtime_fallback:
                raise
            logger.error("%s", exc)
            handler = _build_failed_python_handler()
        except Exception as exc:
            if not allow_runtime_fallback:
                raise typer.BadParameter(
                    _python_route_load_error_message(path=route.source, error=exc)
                ) from exc
            logger.exception(
                "%s",
                _python_route_load_error_message(path=route.source, error=exc),
            )
            handler = _build_failed_python_handler()
        loaded.append(
            LoadedPythonRoute(
                path=route.path,
                source=route.source,
                methods=route.methods,
                handler=handler,
            )
        )
    return loaded


def _build_failed_python_handler() -> Callable[..., Awaitable[web.StreamResponse]]:
    async def _failed_handler(
        *, room: RoomClient, req: web.Request
    ) -> web.StreamResponse:
        del room
        del req
        return web.Response(status=500, text="Internal Server Error")

    return _failed_handler


def _python_route_load_error_message(*, path: Path, error: Exception) -> str:
    if isinstance(error, SyntaxError):
        syntax_path = path
        if isinstance(error.filename, str) and error.filename.strip() != "":
            syntax_path = Path(error.filename).expanduser().resolve()
        location = f"{syntax_path}:{error.lineno}:{error.offset}"
        return (
            f"Unable to load python route file {path}: "
            f"invalid syntax at {location}: {error.msg}"
        )

    error_name = type(error).__name__
    error_message = str(error).strip()
    if error_message == "":
        return f"Unable to load python route file {path}: {error_name}"
    return f"Unable to load python route file {path}: {error_name}: {error_message}"


def _load_python_handler(path: Path, *, app_dir_path: Path) -> Callable[..., Any]:
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
    with _with_app_dir_on_syspath(app_dir_path=app_dir_path):
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
            f"Python route file must not define METHOD/METHODS; configure methods in -f instead: {path}"
        )

    return cast(Callable[..., Any], handler)


def _validate_python_handler_file(path: Path) -> None:
    if not path.exists():
        raise typer.BadParameter(f"Python route file not found: {path}")
    if path.suffix != ".py":
        raise typer.BadParameter(f"Python route must point to a .py file: {path}")

    try:
        source = path.read_text()
    except OSError as exc:
        raise typer.BadParameter(
            f"Unable to read python route file: {path} ({exc})"
        ) from exc

    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        location = f"{path}:{exc.lineno}:{exc.offset}"
        raise typer.BadParameter(
            f"Invalid python route file syntax at {location}: {exc.msg}"
        ) from exc

    def _assigned_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        return None

    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = _assigned_name(target)
                if name in {"METHOD", "METHODS"}:
                    raise typer.BadParameter(
                        f"Python route file must not define METHOD/METHODS; configure methods in -f instead: {path}"
                    )
        if isinstance(node, ast.AnnAssign):
            name = _assigned_name(node.target)
            if name in {"METHOD", "METHODS"}:
                raise typer.BadParameter(
                    f"Python route file must not define METHOD/METHODS; configure methods in -f instead: {path}"
                )

    handler_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in module.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "handler"
        ):
            handler_node = node
            break

    if handler_node is None:
        raise typer.BadParameter(
            f"Python route file must define "
            f"'handler(*, room: RoomClient, req: web.Request)': {path}"
        )

    positional_only = {arg.arg for arg in handler_node.args.posonlyargs}
    if "room" in positional_only or "req" in positional_only:
        raise typer.BadParameter(
            f"Python route handler must accept keyword arguments room and req: {path}"
        )

    keyword_compatible = {
        *[arg.arg for arg in handler_node.args.args],
        *[arg.arg for arg in handler_node.args.kwonlyargs],
    }
    if "room" not in keyword_compatible or "req" not in keyword_compatible:
        raise typer.BadParameter(
            f"Python route handler must define room and req parameters: {path}"
        )


def _resolve_web_bind(
    *,
    config: WebServerConfig,
    routes_path: Path,
    default_host: str,
    default_port: int,
    host_override: str | None,
    port_override: int | None,
) -> tuple[str, int]:
    source = f"-f {routes_path}"

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
    app_dir_path: Path,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    return _resolve_loaded_routes(
        config=config,
        routes_path=routes_path,
        app_dir_path=app_dir_path,
        allow_runtime_fallback=False,
    )


def _resolve_loaded_routes(
    *,
    config: WebServerConfig,
    routes_path: Path,
    app_dir_path: Path,
    allow_runtime_fallback: bool,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    static_routes, python_routes = _resolve_routes_config(
        config=config,
        routes_path=routes_path,
    )
    if len(static_routes) == 0 and len(python_routes) == 0:
        raise typer.BadParameter("No routes were defined")

    for route in static_routes:
        if not route.source.exists():
            raise typer.BadParameter(f"File route path was not found: {route.source}")
        if not route.source.is_dir() and not route.source.is_file():
            raise typer.BadParameter(
                f"File route path must be a file or directory: {route.source}"
            )

    loaded_python_routes = _load_python_routes(
        python_routes,
        app_dir_path=app_dir_path,
        allow_runtime_fallback=allow_runtime_fallback,
    )
    return static_routes, loaded_python_routes


def _resolve_runtime_routes(
    *,
    config: WebServerConfig,
    routes_path: Path,
    app_dir_path: Path,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    return _resolve_loaded_routes(
        config=config,
        routes_path=routes_path,
        app_dir_path=app_dir_path,
        allow_runtime_fallback=True,
    )


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
    app_dir: str | None,
) -> tuple[list[StaticRoute], list[LoadedPythonRoute]]:
    routes_path = _routes_file_path(routes_file)
    app_dir_path = _resolve_app_dir_path(routes_path=routes_path, app_dir=app_dir)
    config = _load_routes_config_file(routes_path=routes_path)
    return _resolve_validated_routes(
        config=config,
        routes_path=routes_path,
        app_dir_path=app_dir_path,
    )


def _normalize_static_mount_path(*, route_path: str) -> str:
    normalized = route_path.rstrip("/")
    return normalized if normalized != "" else "/"


def _resolve_static_directory_target(
    *,
    source: Path,
    relative_url_path: str,
) -> Path | None:
    base_path = source.resolve()
    normalized_relative_url_path = relative_url_path.lstrip("/")
    if normalized_relative_url_path == "":
        return base_path

    relative_parts = PurePosixPath(normalized_relative_url_path).parts
    if any(part == ".." for part in relative_parts):
        return None

    resolved_target = (base_path / Path(*relative_parts)).resolve()
    if resolved_target != base_path and not resolved_target.is_relative_to(base_path):
        return None
    return resolved_target


async def _serve_static_directory_request(
    *,
    source: Path,
    relative_url_path: str,
) -> web.StreamResponse:
    target_path = _resolve_static_directory_target(
        source=source,
        relative_url_path=relative_url_path,
    )
    if target_path is None or not target_path.exists():
        raise web.HTTPNotFound()

    if target_path.is_dir():
        index_path = target_path / DIRECTORY_INDEX_FILE_NAME
        if not index_path.exists() or not index_path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path=index_path)

    if not target_path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path=target_path)


def _build_static_directory_handler(
    *,
    source: Path,
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def _static_directory(req: web.Request) -> web.StreamResponse:
        return await _serve_static_directory_request(
            source=source,
            relative_url_path=req.match_info.get("tail", ""),
        )

    return _static_directory


def _build_web_application(
    *,
    static_routes: list[StaticRoute],
    loaded_python_routes: list[LoadedPythonRoute],
    room: RoomClient,
) -> tuple[web.Application, list[tuple[str, str]]]:
    app = web.Application()
    mounted: list[tuple[str, str]] = []

    for route in static_routes:
        if route.source.is_dir():
            static_directory_handler = _build_static_directory_handler(
                source=route.source,
            )
            mount_path = _normalize_static_mount_path(route_path=route.path)
            wildcard_mount_path = (
                "/{tail:.*}" if mount_path == "/" else f"{mount_path}/{{tail:.*}}"
            )
            app.router.add_get(mount_path, static_directory_handler)
            app.router.add_get(wildcard_mount_path, static_directory_handler)
        else:

            async def _static_file(
                _req: web.Request,
                _source: Path = route.source,
            ) -> web.StreamResponse:
                if not _source.exists() or not _source.is_file():
                    raise web.HTTPNotFound()
                return web.FileResponse(path=_source)

            app.router.add_get(route.path, _static_file)
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

    return app, mounted


def build_webserver(
    *,
    routes_file: str,
    default_host: str,
    default_port: int,
    host_override: str | None,
    port_override: int | None,
    app_dir: str | None,
) -> tuple[type[Any], str, int]:
    routes_path = _routes_file_path(routes_file)
    app_dir_path = _resolve_app_dir_path(routes_path=routes_path, app_dir=app_dir)
    config = _load_routes_config_file(routes_path=routes_path)
    host, port = _resolve_web_bind(
        config=config,
        routes_path=routes_path,
        default_host=default_host,
        default_port=default_port,
        host_override=host_override,
        port_override=port_override,
    )
    static_routes, loaded_python_routes = _resolve_runtime_routes(
        config=config,
        routes_path=routes_path,
        app_dir_path=app_dir_path,
    )

    class WebServer:
        def __init__(self):
            self._runner: web.AppRunner | None = None
            self._mounted: list[tuple[str, str]] = []

        @property
        def mounted(self) -> list[tuple[str, str]]:
            return list(self._mounted)

        async def start(self, *, room: RoomClient) -> None:
            app, mounted = _build_web_application(
                static_routes=static_routes,
                loaded_python_routes=loaded_python_routes,
                room=room,
            )
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


def _spec_stub_webserver_class() -> type[Any]:
    class WebServer:
        async def start(self, *, room: RoomClient) -> None:
            return

        async def stop(self) -> None:
            return

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


def _website_config_relative_path(*, routes_file: str) -> str:
    return _routes_file_path(routes_file).name


def _website_mount_path(*, website_subpath: str) -> str:
    return str(PurePosixPath("/").joinpath(website_subpath))


def _replace_routes_file_arg(*, args: list[str], mounted_routes_path: str) -> list[str]:
    out: list[str] = []
    replaced = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--web-host":
            out.extend(["--host", args[i + 1]])
            i += 2
            continue
        if arg.startswith("--web-host="):
            out.append("--host=" + arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--web-port":
            out.extend(["--port", args[i + 1]])
            i += 2
            continue
        if arg.startswith("--web-port="):
            out.append("--port=" + arg.split("=", 1)[1])
            i += 1
            continue
        if arg == "--website-path":
            i += 2
            continue
        if arg.startswith("--website-path="):
            i += 1
            continue
        if arg == "--path":
            i += 2
            continue
        if arg.startswith("--path="):
            i += 1
            continue
        if arg == "--domain":
            i += 2
            continue
        if arg.startswith("--domain="):
            i += 1
            continue
        if arg == "-f":
            replaced = True
            out.extend([arg, mounted_routes_path])
            i += 2
            continue
        out.append(arg)
        i += 1

    if not replaced:
        out.extend(["-f", mounted_routes_path])
    return out


def _normalize_website_subpath(*, website_path: str) -> str:
    raw = website_path.strip()
    if raw == "":
        raise typer.BadParameter("--website-path must be a non-empty path")
    if raw.startswith("room://"):
        raw = raw[len("room://") :]
    normalized = PurePosixPath("/" + raw).as_posix().strip("/")
    if normalized in {"", ".", ".."}:
        raise typer.BadParameter("--website-path must resolve to a non-root room path")
    if any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts):
        raise typer.BadParameter("--website-path cannot contain '.' or '..' segments")
    return "/" + normalized


def _relative_copy_source_path(*, source: Path, source_label: str) -> str:
    resolved_source = source.resolve()
    cwd = Path.cwd().resolve()
    try:
        relative = resolved_source.relative_to(cwd)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--website-path copy {source_label} must be under current directory {cwd}; found: {resolved_source}"
        ) from exc

    value = relative.as_posix()
    if value in {"", ".", ".."}:
        raise typer.BadParameter(
            f"--website-path copy {source_label} must not resolve to the current directory root: {resolved_source}"
        )
    if any(part in {"", ".", ".."} for part in PurePosixPath(value).parts):
        raise typer.BadParameter(
            f"--website-path copy {source_label} resolved to invalid relative path: {value}"
        )
    return value


def _resolve_website_copy_source(
    *,
    raw_source: str,
    routes_root: Path,
    source_label: str,
) -> tuple[Path, str]:
    raw = raw_source.strip()
    if raw == "":
        raise typer.BadParameter(f"{source_label} must be a non-empty path")

    parsed = Path(raw).expanduser()
    if parsed.is_absolute():
        # In website copy mode, absolute route sources map to {cwd}/<absolute-path-without-leading-slash>.
        local_source = (Path.cwd() / parsed.as_posix().lstrip("/")).resolve()
    else:
        local_source = (routes_root / parsed).resolve()

    relative_path = _relative_copy_source_path(
        source=local_source,
        source_label=source_label,
    )
    return local_source, relative_path


def _collect_website_upload_files(
    *,
    routes_file: str,
    include_routes_config: bool,
    website_mount_path: str = DEFAULT_WEBSITE_MOUNT_PATH,
    runtime_paths_relative_to_working_dir: bool = False,
    app_dir: str | None = None,
) -> list[WebsiteUploadFile]:
    routes_path = _routes_file_path(routes_file)
    app_dir_path = _resolve_app_dir_path(routes_path=routes_path, app_dir=app_dir)
    routes_root = routes_path.parent.resolve()
    source_config = _load_routes_config_file(routes_path=routes_path)
    local_config = source_config.model_copy(deep=True)
    runtime_config = source_config.model_copy(deep=True)
    files: dict[str, WebsiteUploadFile] = {}
    static_source_roots: dict[str, str] = {}

    for index, route in enumerate(source_config.routes):
        source_prefix = f"-f {routes_path} routes[{index}]"
        runtime_target = runtime_config.routes[index]

        if route.python is not None:
            local_source, relative_path = _resolve_website_copy_source(
                raw_source=route.python,
                routes_root=routes_root,
                source_label=f"{source_prefix}.python",
            )
            runtime_target.python = (
                relative_path
                if runtime_paths_relative_to_working_dir
                else str(PurePosixPath(website_mount_path).joinpath(relative_path))
            )
            files[relative_path] = WebsiteUploadFile(
                local_source=local_source,
                relative_path=relative_path,
            )

        if route.static is not None:
            local_source, relative_path = _resolve_website_copy_source(
                raw_source=route.static,
                routes_root=routes_root,
                source_label=f"{source_prefix}.static",
            )
            runtime_target.static = (
                relative_path
                if runtime_paths_relative_to_working_dir
                else str(PurePosixPath(website_mount_path).joinpath(relative_path))
            )
            static_source_roots[str(local_source)] = relative_path

    static_routes, loaded_python_routes = _resolve_validated_routes(
        config=local_config,
        routes_path=routes_path,
        app_dir_path=app_dir_path,
    )

    for route in static_routes:
        source_key = str(route.source.resolve())
        if source_key not in static_source_roots:
            raise typer.BadParameter(
                f"Unable to resolve static route copy source for website deployment: {route.source}"
            )
        relative_dir = static_source_roots[source_key]
        if route.source.is_file():
            files[relative_dir] = WebsiteUploadFile(
                local_source=route.source,
                relative_path=relative_dir,
            )
            continue

        static_files = sorted(
            path for path in route.source.rglob("*") if path.is_file()
        )
        if len(static_files) == 0:
            marker_path = f"{relative_dir}/{EMPTY_DIR_MARKER_NAME}"
            marker_path = marker_path.lstrip("/")
            files[marker_path] = WebsiteUploadFile(
                local_source=None,
                relative_path=marker_path,
                content=b"",
            )
            continue

        for local_path in static_files:
            relative_file_path = str(
                PurePosixPath(relative_dir).joinpath(
                    local_path.relative_to(route.source).as_posix()
                )
            )
            files[relative_file_path] = WebsiteUploadFile(
                local_source=local_path,
                relative_path=relative_file_path,
            )

    # Ensure python route handler modules remain valid with strongly-typed signature rules.
    for route in loaded_python_routes:
        _validate_python_handler_file(route.source)

    if include_routes_config:
        config_relative_path = _website_config_relative_path(routes_file=routes_file)
        config_bytes = yaml.safe_dump(
            runtime_config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ).encode()
        files[config_relative_path] = WebsiteUploadFile(
            local_source=None,
            relative_path=config_relative_path,
            content=config_bytes,
        )

    return [files[key] for key in sorted(files.keys())]


async def _write_room_storage_file(
    *,
    room: RoomClient,
    path: str,
    data: bytes,
) -> None:
    await upload_room_bytes_stream(room=room, path=path, data=data, overwrite=True)


async def _sync_website_files_to_room_storage(
    *,
    account_client,
    project_id: str,
    room_name: str,
    routes_file: str,
    website_subpath: str,
    include_routes_config: bool,
    app_dir: str | None,
) -> None:
    website_mount_path = _website_mount_path(website_subpath=website_subpath)
    uploads = _collect_website_upload_files(
        routes_file=routes_file,
        include_routes_config=include_routes_config,
        website_mount_path=website_mount_path,
        runtime_paths_relative_to_working_dir=True,
        app_dir=app_dir,
    )
    connection = await account_client.connect_room(
        project_id=project_id, room=room_name
    )

    async with RoomClient(
        protocol_factory=WebSocketClientProtocol(
            url=websocket_room_url(room_name=room_name),
            token=connection.jwt,
        ).create_factory()
    ) as room:
        for upload in uploads:
            remote_path = str(
                PurePosixPath(website_subpath).joinpath(upload.relative_path)
            )
            print(f"{upload.relative_path} -> {remote_path}")
            if upload.content is not None:
                data = upload.content
            elif upload.local_source is None:
                data = b""
            else:
                try:
                    data = upload.local_source.read_bytes()
                except OSError as exc:
                    raise typer.BadParameter(
                        f"Unable to read local file for --website-path sync: {upload.local_source} ({exc})"
                    ) from exc
            await _write_room_storage_file(room=room, path=remote_path, data=data)

    print(
        f"[green]Synced {len(uploads)} route file(s) to room storage:[/] {website_subpath}"
    )


def _attach_website_storage_mount(
    *,
    spec: ServiceSpec,
    website_subpath: str,
    website_mount_path: str,
) -> None:
    if spec.container is None:
        raise typer.BadParameter("Service spec is missing container configuration")

    room_mount = RoomStorageMountSpec(
        path=website_mount_path,
        subpath=website_subpath,
        read_only=True,
    )
    if spec.container.storage is None:
        spec.container.storage = ContainerMountSpec(room=[room_mount])
        return

    existing_room_mounts = spec.container.storage.room or []
    spec.container.storage.room = [
        *[mount for mount in existing_room_mounts if mount.path != website_mount_path],
        room_mount,
    ]


def _ensure_website_llm_token_environment(
    *,
    spec: ServiceSpec,
    website_identity: str,
) -> None:
    if spec.container is None:
        raise typer.BadParameter("Service spec is missing container configuration")

    identity = website_identity.strip()
    if identity == "":
        raise typer.BadParameter("agent name must be a non-empty string")

    environment = list(spec.container.environment or [])
    default_scope = ApiScope.agent_default()

    for env_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        token_value = TokenValue(
            identity=identity,
            api=default_scope,
            role="agent",
        )
        for index, env_var in enumerate(environment):
            if env_var.name != env_name:
                continue
            environment[index] = EnvironmentVariable(
                name=env_name,
                token=token_value,
            )
            break
        else:
            environment.append(
                EnvironmentVariable(
                    name=env_name,
                    token=token_value,
                )
            )

    spec.container.environment = environment


def _ensure_webserver_token_environment(
    *,
    spec: ServiceSpec,
    website_identity: str,
) -> None:
    if spec.container is None:
        raise typer.BadParameter("Service spec is missing container configuration")

    identity = website_identity.strip()
    if identity == "":
        raise typer.BadParameter("agent name must be a non-empty string")

    environment = list(spec.container.environment or [])
    token_value = TokenValue(
        identity=identity,
        api=ApiScope.agent_default(),
        role="agent",
    )

    for index, env_var in enumerate(environment):
        if env_var.name != "MESHAGENT_TOKEN":
            continue
        environment[index] = EnvironmentVariable(
            name="MESHAGENT_TOKEN",
            token=token_value,
        )
        spec.container.environment = environment
        return

    environment.append(
        EnvironmentVariable(
            name="MESHAGENT_TOKEN",
            token=token_value,
        )
    )
    spec.container.environment = environment


@app.async_command("check")
async def check(
    *,
    routes_file: Annotated[
        str,
        typer.Option(
            "-f",
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    app_dir: Annotated[
        Optional[str],
        typer.Option("--app-dir", help=APP_DIR_HELP),
    ] = None,
):
    """Validate a routes file and print the resolved routes."""
    static_routes, python_routes = _validate_routes_file(
        routes_file=routes_file,
        app_dir=app_dir,
    )
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
    print(f"[green]{WEBSERVER_USAGE_TEXT}[/green]")


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
            help="Static file or directory path (relative to routes file)",
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

        _validate_python_handler_file(python_path)

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
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    app_dir: Annotated[
        Optional[str],
        typer.Option("--app-dir", help=APP_DIR_HELP),
    ] = None,
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
    """Join a room and run the configured webserver routes locally with optional hot reload."""
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
            app_dir=app_dir,
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
                        app_dir=app_dir,
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
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=jwt,
            ).create_factory()
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


@app.async_command("spec")
async def spec(
    *,
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
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
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    app_dir: Annotated[
        Optional[str],
        typer.Option("--app-dir", help=APP_DIR_HELP),
    ] = None,
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = DEFAULT_SERVICE_BIND_HOST,
    web_port: Annotated[
        int, typer.Option(help="Port to bind the webserver")
    ] = DEFAULT_BIND_PORT,
    website_path: Annotated[
        str,
        typer.Option(
            "--website-path",
            help="Required room storage subpath for route-referenced files (under the routes file directory) and the routes config file; adds a room storage mount so routes resolve in container (example: /website)",
        ),
    ],
):
    """Generate a service spec for deploying this webserver configuration."""
    resolved_service_name = service_name if service_name is not None else agent_name
    web_host_override = _cli_override_or_none(value=web_host, option_name="web_host")
    web_port_override = _cli_override_or_none(value=web_port, option_name="web_port")

    website_subpath = _normalize_website_subpath(website_path=website_path)
    website_mount_path = _website_mount_path(website_subpath=website_subpath)

    routes_path = _routes_file_path(routes_file)
    config = _load_routes_config_file(routes_path=routes_path)
    _, resolved_web_port = _resolve_web_bind(
        config=config,
        routes_path=routes_path,
        default_host=DEFAULT_SERVICE_BIND_HOST,
        default_port=DEFAULT_BIND_PORT,
        host_override=web_host_override,
        port_override=web_port_override,
    )
    service = get_service(host=DEFAULT_SERVICE_BIND_HOST, port=resolved_web_port)
    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "WebServer"})
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    _collect_website_upload_files(
        routes_file=routes_file,
        include_routes_config=True,
        website_mount_path=website_mount_path,
        runtime_paths_relative_to_working_dir=True,
        app_dir=app_dir,
    )
    WebServer = _spec_stub_webserver_class()
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=resolved_web_port)
    _attach_website_storage_mount(
        spec=spec,
        website_subpath=website_subpath,
        website_mount_path=website_mount_path,
    )
    if spec.container is None:
        raise typer.BadParameter("Service spec is missing container configuration")
    spec.container.working_dir = website_mount_path
    mounted_routes_path = _website_config_relative_path(routes_file=routes_file)
    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }
    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "webserver",
            "join",
            *_replace_routes_file_arg(
                args=cleanup_args(sys.argv[2:]),
                mounted_routes_path=mounted_routes_path,
            ),
        ]
    )
    _ensure_webserver_token_environment(spec=spec, website_identity=agent_name)
    _ensure_website_llm_token_environment(spec=spec, website_identity=agent_name)

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy")
async def deploy(
    *,
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
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
            help=ROUTES_FILE_HELP,
        ),
    ] = DEFAULT_ROUTES_FILE,
    app_dir: Annotated[
        Optional[str],
        typer.Option("--app-dir", help=APP_DIR_HELP),
    ] = None,
    web_host: Annotated[
        str, typer.Option(help="Host to bind the webserver")
    ] = DEFAULT_SERVICE_BIND_HOST,
    web_port: Annotated[
        int, typer.Option(help="Port to bind the webserver")
    ] = DEFAULT_BIND_PORT,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    domain: Annotated[
        Optional[str],
        typer.Option(
            "--domain",
            help="Domain to create/update route for this deploy (must already target this room if it exists)",
        ),
    ] = None,
    website_path: Annotated[
        str,
        typer.Option(
            "--website-path",
            help="Required room storage subpath to upload route-referenced files (under the routes file directory) and the routes config file into and mount at runtime (example: /website)",
        ),
    ],
):
    """Deploy this webserver as a service, updating an existing service with the same name."""
    resolved_service_name = service_name if service_name is not None else agent_name
    project_id = await resolve_project_id(project_id=project_id)
    room_name = resolve_room(room)
    if domain is not None:
        domain = domain.strip()
        if domain == "":
            raise typer.BadParameter("--domain must be a non-empty string")
        if room_name is None:
            raise typer.BadParameter(
                "--domain requires --room (or MESHAGENT_ROOM) so the route can target a specific room"
            )

    web_host_override = _cli_override_or_none(value=web_host, option_name="web_host")
    web_port_override = _cli_override_or_none(value=web_port, option_name="web_port")

    website_subpath = _normalize_website_subpath(website_path=website_path)
    website_mount_path = _website_mount_path(website_subpath=website_subpath)

    routes_path = _routes_file_path(routes_file)
    config = _load_routes_config_file(routes_path=routes_path)
    _, resolved_web_port = _resolve_web_bind(
        config=config,
        routes_path=routes_path,
        default_host=DEFAULT_SERVICE_BIND_HOST,
        default_port=DEFAULT_BIND_PORT,
        host_override=web_host_override,
        port_override=web_port_override,
    )
    service = get_service(host=DEFAULT_SERVICE_BIND_HOST, port=resolved_web_port)
    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "WebServer"})
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    _collect_website_upload_files(
        routes_file=routes_file,
        include_routes_config=True,
        website_mount_path=website_mount_path,
        runtime_paths_relative_to_working_dir=True,
        app_dir=app_dir,
    )
    WebServer = _spec_stub_webserver_class()
    service.add_path(identity=agent_name, path=path, cls=WebServer)

    spec = service_specs()[0]
    _set_port_spec(spec, web_port=resolved_web_port)
    _attach_website_storage_mount(
        spec=spec,
        website_subpath=website_subpath,
        website_mount_path=website_mount_path,
    )
    if spec.container is None:
        raise typer.BadParameter("Service spec is missing container configuration")
    spec.container.working_dir = website_mount_path
    mounted_routes_path = _website_config_relative_path(routes_file=routes_file)
    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }
    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "webserver",
            "join",
            *_replace_routes_file_arg(
                args=cleanup_args(sys.argv[2:]),
                mounted_routes_path=mounted_routes_path,
            ),
        ]
    )
    _ensure_webserver_token_environment(spec=spec, website_identity=agent_name)
    _ensure_website_llm_token_environment(spec=spec, website_identity=agent_name)

    client = await get_client()
    try:
        if room_name is None:
            raise typer.BadParameter(
                "--website-path requires --room (or MESHAGENT_ROOM) so files can be uploaded to room storage"
            )
        await _sync_website_files_to_room_storage(
            account_client=client,
            project_id=project_id,
            room_name=room_name,
            routes_file=routes_file,
            website_subpath=website_subpath,
            include_routes_config=True,
            app_dir=app_dir,
        )

        id = None
        try:
            if id is None:
                if room_name is None:
                    services = await client.list_services(project_id=project_id)
                else:
                    services = await client.list_room_services(
                        project_id=project_id, room_name=room_name
                    )

                for s in services:
                    if s.metadata.name == spec.metadata.name:
                        id = s.id

            if id is None:
                if room_name is None:
                    id = await client.create_service(
                        project_id=project_id, service=spec
                    )
                else:
                    id = await client.create_room_service(
                        project_id=project_id, service=spec, room_name=room_name
                    )

            else:
                spec.id = id
                if room_name is None:
                    await client.update_service(
                        project_id=project_id, service_id=id, service=spec
                    )
                else:
                    await client.update_room_service(
                        project_id=project_id,
                        service_id=id,
                        service=spec,
                        room_name=room_name,
                    )

        except ConflictError:
            print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Deployed service:[/] {id}")
            if domain is not None:
                await _upsert_domain_route(
                    client=client,
                    project_id=project_id,
                    domain=domain,
                    room_name=cast(str, room_name),
                    port=str(resolved_web_port),
                    service_id=resolved_service_name,
                )

    finally:
        await client.close()
