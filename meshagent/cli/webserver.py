from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import shlex
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Optional, cast

import typer
import yaml
from aiohttp import web
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


def _parse_route_mapping(value: str, option_name: str) -> tuple[str, Path]:
    if ":" not in value:
        raise typer.BadParameter(
            f"{option_name} must be in the form /route:/path (got {value!r})"
        )
    route_path, raw_path = value.split(":", 1)
    route_path = route_path.strip()
    raw_path = raw_path.strip()
    if not route_path or not route_path.startswith("/"):
        raise typer.BadParameter(
            f"{option_name} route must start with '/' (got {route_path!r})"
        )
    if not raw_path:
        raise typer.BadParameter(f"{option_name} path is required")
    path = Path(raw_path).expanduser().resolve()
    return route_path, path


def _load_python_handler(path: Path) -> tuple[Callable[..., Any], list[str]]:
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

    methods = getattr(module, "METHODS", None)
    if methods is None:
        method = getattr(module, "METHOD", None)
        if method is not None:
            methods = [method]
    if methods is None:
        methods = ["GET"]
    if isinstance(methods, str):
        methods = [methods]

    normalized = [m.upper() for m in methods]
    return cast(Callable[..., Any], handler), normalized


def _ensure_routes(
    *, file_route: list[str] | None, python_route: list[str] | None
) -> None:
    if not file_route and not python_route:
        raise typer.BadParameter(
            "At least one --file-route or --python-route is required"
        )


def _python_route_paths(python_route: list[str] | None) -> list[Path]:
    paths: list[Path] = []
    for entry in python_route or []:
        _, path = _parse_route_mapping(entry, "--python-route")
        paths.append(path)
    return paths


def _collect_mtimes(paths: list[Path]) -> dict[str, float | None]:
    mtimes: dict[str, float | None] = {}
    for path in paths:
        mtimes[str(path)] = path.stat().st_mtime if path.exists() else None
    return mtimes


def build_webserver(
    *,
    file_route: list[str] | None,
    python_route: list[str] | None,
    host: str,
    port: int,
):
    _ensure_routes(file_route=file_route, python_route=python_route)

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

            for entry in file_route or []:
                route_path, path = _parse_route_mapping(entry, "--file-route")
                if not path.exists() or not path.is_dir():
                    raise typer.BadParameter(
                        f"File route path must be a directory: {path}"
                    )
                app.router.add_static(route_path, path, show_index=True)
                mounted.append((route_path, f"static:{path}"))

            for entry in python_route or []:
                route_path, path = _parse_route_mapping(entry, "--python-route")
                handler, methods = _load_python_handler(path)

                async def _wrapped(
                    request: web.Request,
                    _handler: Callable[..., Any] = handler,
                ) -> web.StreamResponse:
                    result = _handler(room=room, req=request)
                    if inspect.isawaitable(result):
                        result = await result
                    return result

                for method in methods:
                    app.router.add_route(method, route_path, _wrapped)
                mounted.append((route_path, f"python:{path}"))

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
    file_route: Annotated[
        list[str] | None,
        typer.Option(
            "--file-route",
            help="Expose a local folder as /route:/path (repeatable)",
        ),
    ] = None,
    python_route: Annotated[
        list[str] | None,
        typer.Option(
            "--python-route",
            help="Mount a python handler as /route:/path.py (repeatable)",
        ),
    ] = None,
    host: Annotated[str, typer.Option(help="Host to bind the server")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind the server")] = 8000,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Reload python routes when files change",
        ),
    ] = False,
):
    _ensure_routes(file_route=file_route, python_route=python_route)

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
            file_route=file_route,
            python_route=python_route,
            host=host,
            port=port,
        )
        server = WebServer()
        server_ref = {"server": server}

        async def _watch_routes(*, room: RoomClient) -> None:
            route_paths = _python_route_paths(python_route)
            if not route_paths:
                return

            previous = _collect_mtimes(route_paths)
            while True:
                await asyncio.sleep(0.5)
                current = _collect_mtimes(route_paths)
                if current == previous:
                    continue
                previous = current
                print("[yellow]Detected route changes, reloading...[/yellow]")
                await server_ref["server"].stop()
                WebServer = build_webserver(
                    file_route=file_route,
                    python_route=python_route,
                    host=host,
                    port=port,
                )
                server_ref["server"] = WebServer()
                await server_ref["server"].start(room=room)
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
    file_route: Annotated[
        list[str] | None,
        typer.Option(
            "--file-route",
            help="Expose a local folder as /route:/path (repeatable)",
        ),
    ] = None,
    python_route: Annotated[
        list[str] | None,
        typer.Option(
            "--python-route",
            help="Mount a python handler as /route:/path.py (repeatable)",
        ),
    ] = None,
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
    _ensure_routes(file_route=file_route, python_route=python_route)

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
        file_route=file_route,
        python_route=python_route,
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
    file_route: Annotated[
        list[str] | None,
        typer.Option(
            "--file-route",
            help="Expose a local folder as /route:/path (repeatable)",
        ),
    ] = None,
    python_route: Annotated[
        list[str] | None,
        typer.Option(
            "--python-route",
            help="Mount a python handler as /route:/path.py (repeatable)",
        ),
    ] = None,
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
    _ensure_routes(file_route=file_route, python_route=python_route)

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
        file_route=file_route,
        python_route=python_route,
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
    file_route: Annotated[
        list[str] | None,
        typer.Option(
            "--file-route",
            help="Expose a local folder as /route:/path (repeatable)",
        ),
    ] = None,
    python_route: Annotated[
        list[str] | None,
        typer.Option(
            "--python-route",
            help="Mount a python handler as /route:/path.py (repeatable)",
        ),
    ] = None,
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
    _ensure_routes(file_route=file_route, python_route=python_route)

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
        file_route=file_route,
        python_route=python_route,
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
