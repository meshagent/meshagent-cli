import os
import shlex
import string

import typer
from rich import print
from typing import Annotated, Optional, List
from meshagent.cli.common_options import ProjectIdOption, RoomOption

from meshagent.api.helpers import websocket_room_url
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException, ApiScope
from meshagent.cli import async_typer
from meshagent.cli.helper import resolve_project_id, resolve_room, resolve_key

from meshagent.tools import Toolkit
from meshagent.tools.hosting import start_hosted_toolkit


from meshagent.api.services import ServiceHost

from meshagent.api import ParticipantToken


def _kv_to_dict(
    pairs: List[str], separator: str = "=", *, trim: bool = False
) -> dict[str, str]:
    """Convert ["A=1","B=2"] → {"A":"1","B":"2"}."""
    out: dict[str, str] = {}
    for p in pairs:
        if separator not in p:
            raise typer.BadParameter(f"'{p}' must be KEY{separator}VALUE")
        k, v = p.split(separator, 1)
        if trim:
            k = k.strip()
            v = v.strip()
        out[k] = v
    return out


def _parse_header_secret(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise typer.BadParameter(
            f"'{value}' must be HEADER:FORMAT (e.g. Header:Bearer {{secret}})"
        )
    header_name, format_spec = value.split(":", 1)
    header_name = header_name.strip()
    format_spec = format_spec.strip()
    if header_name == "" or format_spec == "":
        raise typer.BadParameter(
            f"'{value}' must be HEADER:FORMAT (e.g. Header:Bearer {{secret}})"
        )
    return header_name, format_spec


async def _resolve_header_secrets(
    room_client: RoomClient, header_secret: List[str]
) -> dict[str, str]:
    if not header_secret:
        return {}

    secrets = await room_client.secrets.list_secrets()
    secrets_by_id = {secret.id: secret for secret in secrets}
    secrets_by_name = {secret.name: secret for secret in secrets}

    resolved: dict[str, str] = {}
    for item in header_secret:
        header_name, format_spec = _parse_header_secret(item)
        placeholder_names = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(format_spec)
            if field_name
        }
        if not placeholder_names:
            raise typer.BadParameter(
                f"Header secret format '{format_spec}' must include a placeholder"
            )

        secrets_values: dict[str, str] = {}
        for secret_key in placeholder_names:
            secret = secrets_by_id.get(secret_key) or secrets_by_name.get(secret_key)
            if secret is None:
                raise typer.BadParameter(
                    f"Secret '{secret_key}' not found (use secret id or name)"
                )
            secret_response = await room_client.secrets.get_secret(secret_id=secret.id)
            if secret_response is None:
                raise typer.BadParameter(f"Secret '{secret_key}' has no data")
            try:
                resolved_value = secret_response.data.decode()
            except UnicodeDecodeError as exc:
                raise typer.BadParameter(
                    f"Secret '{secret_key}' is not valid UTF-8 text"
                ) from exc
            secrets_values[secret_key] = resolved_value

        try:
            formatted_value = format_spec.format(**secrets_values)
        except (KeyError, IndexError, ValueError) as exc:
            raise typer.BadParameter(
                f"Header secret format '{format_spec}' is invalid"
            ) from exc
        resolved[header_name] = formatted_value

    return resolved


app = async_typer.AsyncTyper(help="Bridge MCP servers into MeshAgent rooms")


@app.async_command(
    "sse", help="Connect an MCP server over SSE and register it as a toolkit"
)
async def sse(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "tool",
    url: Annotated[str, typer.Option(..., help="SSE URL for the MCP server")],
    header: Annotated[
        List[str],
        typer.Option(
            "--header",
            "-H",
            help="Request header (KEY:VALUE). Repeat for multiple headers",
        ),
    ] = [],
    header_secret: Annotated[
        List[str],
        typer.Option(
            "--header-secret",
            help=(
                "Header from secret (HEADER:FORMAT). "
                "FORMAT uses secret placeholders, e.g. Header:Bearer {my_secret}"
            ),
        ),
    ] = [],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(help="Toolkit name to register in the room (default: mcp)"),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    """Connect an MCP server over SSE and expose it as a room toolkit."""

    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    from meshagent.mcp import MCPToolkit

    key = await resolve_key(project_id=project_id, key=key)

    if toolkit_name is None:
        toolkit_name = "mcp"

    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            token = ParticipantToken(
                name=toolkit_name or "mcp",
            )

            token.add_role_grant(role=role)
            token.add_room_grant(room)
            token.add_api_grant(ApiScope.full())

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        ) as client:
            headers = _kv_to_dict(header, separator=":", trim=True) if header else {}
            secret_headers = await _resolve_header_secrets(client, header_secret)
            headers = {**headers, **secret_headers}
            if not headers:
                headers = None
            async with sse_client(url, headers=headers) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream=read_stream, write_stream=write_stream
                ) as session:
                    mcp_tools_response = await session.list_tools()

                    toolkit = MCPToolkit(
                        name=toolkit_name,
                        session=session,
                        tools=mcp_tools_response.tools,
                    )

                    toolkit = Toolkit(
                        name=toolkit.name,
                        tools=toolkit.tools,
                        title=toolkit.title,
                        description=toolkit.description,
                    )

                    hosted_toolkit = await start_hosted_toolkit(
                        room=client,
                        toolkit=toolkit,
                    )
                    try:
                        await client.protocol.wait_for_close()
                    except KeyboardInterrupt:
                        await hosted_toolkit.stop()

    except RoomException as e:
        print(e)


@app.async_command(
    "http",
    help="Connect an MCP server over streamable HTTP and register it as a toolkit",
)
async def streamable_http(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "tool",
    url: Annotated[
        str, typer.Option(..., help="Streamable HTTP URL for the MCP server")
    ],
    header: Annotated[
        List[str],
        typer.Option(
            "--header",
            "-H",
            help="Request header (KEY:VALUE). Repeat for multiple headers",
        ),
    ] = [],
    header_secret: Annotated[
        List[str],
        typer.Option(
            "--header-secret",
            help=(
                "Header from secret (HEADER:FORMAT). "
                "FORMAT uses secret placeholders, e.g. Header:Bearer {my_secret}"
            ),
        ),
    ] = [],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(help="Toolkit name to register in the room (default: mcp)"),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    """Connect an MCP server over streamable HTTP and expose it as a room toolkit."""

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from meshagent.mcp import MCPToolkit

    key = await resolve_key(project_id=project_id, key=key)

    if toolkit_name is None:
        toolkit_name = "mcp"

    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            token = ParticipantToken(
                name=toolkit_name or "mcp",
            )

            token.add_role_grant(role=role)
            token.add_room_grant(room)
            token.add_api_grant(ApiScope.full())

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        ) as client:
            headers = _kv_to_dict(header, separator=":", trim=True) if header else {}
            secret_headers = await _resolve_header_secrets(client, header_secret)
            headers = {**headers, **secret_headers}
            if not headers:
                headers = None
            async with streamablehttp_client(url, headers=headers) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(
                    read_stream=read_stream, write_stream=write_stream
                ) as session:
                    await session.initialize()
                    mcp_tools_response = await session.list_tools()

                    toolkit = MCPToolkit(
                        name=toolkit_name,
                        session=session,
                        tools=mcp_tools_response.tools,
                    )

                    toolkit = Toolkit(
                        name=toolkit.name,
                        tools=toolkit.tools,
                        title=toolkit.title,
                        description=toolkit.description,
                    )

                    hosted_toolkit = await start_hosted_toolkit(
                        room=client,
                        toolkit=toolkit,
                    )
                    try:
                        await client.protocol.wait_for_close()
                    except KeyboardInterrupt:
                        await hosted_toolkit.stop()

    except RoomException as e:
        print(e)


@app.async_command(
    "stdio", help="Run an MCP server over stdio and register it as a toolkit"
)
async def stdio(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "tool",
    command: Annotated[
        str,
        typer.Option(
            ..., help="Command to start an MCP server over stdio (quoted string)"
        ),
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(help="Toolkit name to register in the room (default: mcp)"),
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    """Run an MCP server over stdio and expose it as a room toolkit."""

    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    from meshagent.mcp import MCPToolkit

    key = await resolve_key(project_id=project_id, key=key)

    if toolkit_name is None:
        toolkit_name = "mcp"

    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            token = ParticipantToken(
                name=toolkit_name or "mcp",
            )

            token.add_role_grant(role=role)
            token.add_room_grant(room)
            token.add_api_grant(ApiScope.full())

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        ) as client:
            parsed_command = shlex.split(command)

            async with (
                stdio_client(
                    StdioServerParameters(
                        command=parsed_command[0],  # Executable
                        args=parsed_command[1:],  # Optional command line arguments
                        env=_kv_to_dict(env),  # Optional environment variables
                    )
                ) as (read_stream, write_stream)
            ):
                async with ClientSession(
                    read_stream=read_stream, write_stream=write_stream
                ) as session:
                    mcp_tools_response = await session.list_tools()

                    toolkit = MCPToolkit(
                        name=toolkit_name,
                        session=session,
                        tools=mcp_tools_response.tools,
                    )

                    toolkit = Toolkit(
                        name=toolkit.name,
                        tools=toolkit.tools,
                        title=toolkit.title,
                        description=toolkit.description,
                    )

                    hosted_toolkit = await start_hosted_toolkit(
                        room=client,
                        toolkit=toolkit,
                    )
                    try:
                        await client.protocol.wait_for_close()
                    except KeyboardInterrupt:
                        await hosted_toolkit.stop()

    except RoomException as e:
        print(e)


@app.async_command("http-proxy", help="Expose a stdio MCP server over streamable HTTP")
async def stdio_host(
    *,
    command: Annotated[
        str,
        typer.Option(..., help="Command to start the MCP server (stdio transport)"),
    ],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the proxy server on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the proxy server on")
    ] = None,
    path: Annotated[
        Optional[str],
        typer.Option(help="HTTP path to mount the proxy server at"),
    ] = None,
    name: Annotated[
        Optional[str], typer.Option(help="Display name for the proxy server")
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
    """Expose a stdio-based MCP server over streamable HTTP."""

    from fastmcp import FastMCP, Client
    from fastmcp.client.transports import StdioTransport

    parsed_command = shlex.split(command)

    # Create a client that connects to the original server
    proxy_client = Client(
        transport=StdioTransport(
            parsed_command[0], parsed_command[1:], _kv_to_dict(env)
        ),
    )

    if name is None:
        name = "Stdio-to-Streamable Http Proxy"

    # Create a proxy server that connects to the client and exposes its capabilities
    proxy = FastMCP.as_proxy(proxy_client, name=name)
    if path is None:
        path = "/mcp"

    await proxy.run_async(transport="streamable-http", host=host, port=port, path=path)


@app.async_command("sse-proxy", help="Expose a stdio MCP server over SSE")
async def sse_proxy(
    *,
    command: Annotated[
        str,
        typer.Option(..., help="Command to start the MCP server (stdio transport)"),
    ],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the proxy server on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the proxy server on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="SSE path to mount the proxy at")
    ] = None,
    name: Annotated[
        Optional[str], typer.Option(help="Display name for the proxy server")
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
    """Expose a stdio-based MCP server over SSE."""

    from fastmcp import FastMCP, Client
    from fastmcp.client.transports import StdioTransport

    parsed_command = shlex.split(command)

    # Create a client that connects to the original server
    proxy_client = Client(
        transport=StdioTransport(
            parsed_command[0], parsed_command[1:], _kv_to_dict(env)
        ),
    )

    if name is None:
        name = "Stdio-to-SSE Proxy"

    # Create a proxy server that connects to the client and exposes its capabilities
    proxy = FastMCP.as_proxy(proxy_client, name=name)
    if path is None:
        path = "/sse"

    await proxy.run_async(transport="sse", host=host, port=port, path=path)


@app.async_command("stdio-service", help="Run a stdio MCP server as an HTTP service")
async def stdio_service(
    *,
    command: Annotated[
        str,
        typer.Option(
            ..., help="Command to start an MCP server over stdio (quoted string)"
        ),
    ],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        typer.Option(help="Optional webhook secret for authenticating requests"),
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    toolkit_name: Annotated[
        Optional[str], typer.Option(help="Toolkit name to expose (default: mcp)")
    ] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
    """Run a stdio-based MCP server as an HTTP service."""

    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    from meshagent.mcp import MCPToolkit

    try:
        parsed_command = shlex.split(command)

        async with (
            stdio_client(
                StdioServerParameters(
                    command=parsed_command[0],  # Executable
                    args=parsed_command[1:],  # Optional command line arguments
                    env=_kv_to_dict(env),  # Optional environment variables
                )
            ) as (read_stream, write_stream)
        ):
            async with ClientSession(
                read_stream=read_stream, write_stream=write_stream
            ) as session:
                mcp_tools_response = await session.list_tools()

                if toolkit_name is None:
                    toolkit_name = "mcp"

                toolkit = MCPToolkit(
                    name=toolkit_name, session=session, tools=mcp_tools_response.tools
                )

                if port is None:
                    port = int(os.getenv("MESHAGENT_PORT", "8080"))

                if host is None:
                    host = "0.0.0.0"

                service_host = ServiceHost(
                    host=host, port=port, webhook_secret=webhook_secret
                )

                if path is None:
                    path = "/service"

                print(
                    f"[bold green]Starting service host on {host}:{port}{path}...[/bold green]"
                )

                @service_host.path(path=path)
                class CustomToolkit(Toolkit):
                    def __init__(self):
                        super().__init__(
                            name=toolkit.name,
                            tools=toolkit.tools,
                            title=toolkit.title,
                            description=toolkit.description,
                        )

                await service_host.run()

    except RoomException as e:
        print(e)
