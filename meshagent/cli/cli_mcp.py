import typer
from rich import print
from typing import Annotated, Optional, List
from meshagent.cli.common_options import ProjectIdOption, RoomOption

from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)

from meshagent.tools.hosting import RemoteToolkit


from meshagent.api.services import ServiceHost
import os

import shlex

from meshagent.api import ParticipantToken, ApiScope


def _kv_to_dict(pairs: List[str]) -> dict[str, str]:
    """Convert ["A=1","B=2"] → {"A":"1","B":"2"}."""
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"'{p}' must be KEY=VALUE")
        k, v = p.split("=", 1)
        out[k] = v
    return out


app = async_typer.AsyncTyper()


@app.async_command("sse")
async def sse(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "tool",
    url: Annotated[str, typer.Option()],
    toolkit_name: Annotated[Optional[str], typer.Option()] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    from meshagent.mcp import MCPToolkit

    key = await resolve_key(project_id=project_id, key=key)

    if toolkit_name is None:
        toolkit_name = "mcp"

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        token = ParticipantToken(
            name=name,
        )

        token.add_api_grant(ApiScope.agent_default())

        token.add_role_grant(role=role)
        token.add_room_grant(room)

        jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        ) as client:
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream=read_stream, write_stream=write_stream
                ) as session:
                    mcp_tools_response = await session.list_tools()

                    toolkit = MCPToolkit(
                        name=toolkit_name,
                        session=session,
                        tools=mcp_tools_response.tools,
                    )

                    remote_toolkit = RemoteToolkit(
                        name=toolkit.name,
                        tools=toolkit.tools,
                        title=toolkit.title,
                        description=toolkit.description,
                    )

                    await remote_toolkit.start(room=client)
                    try:
                        await client.protocol.wait_for_close()
                    except KeyboardInterrupt:
                        await remote_toolkit.stop()

    except RoomException as e:
        print(f"[red]{e}[/red]")
    finally:
        await account_client.close()


@app.async_command("stdio")
async def stdio(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "tool",
    command: Annotated[str, typer.Option()],
    toolkit_name: Annotated[Optional[str], typer.Option()] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    from meshagent.mcp import MCPToolkit

    key = await resolve_key(project_id=project_id, key=key)

    if toolkit_name is None:
        toolkit_name = "mcp"

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        token = ParticipantToken(
            name=name,
        )

        token.add_api_grant(ApiScope.agent_default())

        token.add_role_grant(role=role)
        token.add_room_grant(room)

        jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
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

                    remote_toolkit = RemoteToolkit(
                        name=toolkit.name,
                        tools=toolkit.tools,
                        title=toolkit.title,
                        description=toolkit.description,
                    )

                    await remote_toolkit.start(room=client)
                    try:
                        await client.protocol.wait_for_close()
                    except KeyboardInterrupt:
                        await remote_toolkit.stop()

    except RoomException as e:
        print(f"[red]{e}[/red]")
    finally:
        await account_client.close()


@app.async_command("http-proxy")
async def stdio_host(
    *,
    command: Annotated[str, typer.Option()],
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[Optional[str], typer.Option()] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
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


@app.async_command("sse-proxy")
async def sse_proxy(
    *,
    command: Annotated[str, typer.Option()],
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[Optional[str], typer.Option()] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
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


@app.async_command("stdio-service")
async def stdio_service(
    *,
    command: Annotated[str, typer.Option()],
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    webhook_secret: Annotated[Optional[str], typer.Option()] = None,
    path: Annotated[Optional[str], typer.Option()] = None,
    toolkit_name: Annotated[Optional[str], typer.Option()] = None,
    env: Annotated[List[str], typer.Option("--env", "-e", help="KEY=VALUE")] = [],
):
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
                class CustomToolkit(RemoteToolkit):
                    def __init__(self):
                        super().__init__(
                            name=toolkit.name,
                            tools=toolkit.tools,
                            title=toolkit.title,
                            description=toolkit.description,
                        )

                await service_host.run()

    except RoomException as e:
        print(f"[red]{e}[/red]")
