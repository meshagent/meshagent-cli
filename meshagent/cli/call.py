import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
import json
import aiohttp
from meshagent.api import (
    RoomClient,
    ParticipantToken,
    WebSocketClientProtocol,
    ParticipantGrant,
    ApiScope,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.services import send_webhook
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_project_id, resolve_key
from meshagent.cli.helper import resolve_room
from urllib.parse import urlparse
from pathlib import PurePath
import socket
import ipaddress
import pathlib
from pydantic_yaml import parse_yaml_raw_as
from meshagent.api.participant_token import ParticipantTokenSpec

app = async_typer.AsyncTyper()

PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # IPv4 link-local
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)


def is_local_url(url: str) -> bool:
    """
    Return True if *url* points to the local machine or a private-LAN host.
    """
    # 1. Handle bare paths and file://
    if "://" not in url:
        return PurePath(url).is_absolute() or not ("/" in url or "\\" in url)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return True

    # 2. Quick loop-back check on hostname literal
    hostname = parsed.hostname
    if hostname in {"localhost", None}:  # None ⇒ something like "http:///path"
        return True

    try:
        # Accept both direct IP literals and DNS names
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # Unresolvable host ⇒ treat as non-local (or raise)

    for *_, sockaddr in addr_info:
        ip_str = sockaddr[0]
        ip = ipaddress.ip_address(ip_str)

        if ip.is_loopback:
            return True
        if any(ip in net for net in PRIVATE_NETS):
            return True


@app.async_command("schema")
@app.async_command("toolkit")
@app.async_command("agent")
@app.async_command("tool")
async def make_call(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    role: str = "agent",
    local: Optional[bool] = None,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="deprecated and unused", hidden=True)
    ] = None,
    name: Annotated[str, typer.Option(..., help="deprecated", hidden=True)] = None,
    participant_name: Annotated[
        Optional[str],
        typer.Option(..., help="the participant name to be used by the callee"),
    ] = None,
    url: Annotated[str, typer.Option(..., help="URL the agent should call")],
    arguments: Annotated[
        str, typer.Option(..., help="JSON string with arguments for the call")
    ] = {},
    permissions: Annotated[
        Optional[str],
        typer.Option(
            "--permissions",
            "-p",
            help="File path to a token definition, if not specified default agent permissions will be used",
        ),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    key = await resolve_key(project_id=project_id, key=key)

    if permissions is not None:
        with open(str(pathlib.Path(permissions).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ParticipantTokenSpec, f.read())

            token = ParticipantToken(
                name=spec.identity,
            )
            token.add_role_grant(role=role)
            token.add_room_grant(room)
            token.add_api_grant(spec.api)

    else:
        token = None

    await _make_call(
        project_id=project_id,
        room=room,
        role=role,
        local=local,
        agent_name=agent_name,
        name=name,
        participant_name=participant_name,
        url=url,
        arguments=arguments,
        token=token,
        key=key,
    )


async def _make_call(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    role: str = "agent",
    local: Optional[bool] = None,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="deprecated and unused", hidden=True)
    ] = None,
    name: Annotated[str, typer.Option(..., help="deprecated", hidden=True)] = None,
    participant_name: Annotated[
        Optional[str],
        typer.Option(..., help="the participant name to be used by the callee"),
    ] = None,
    url: Annotated[str, typer.Option(..., help="URL the agent should call")],
    arguments: Annotated[
        str, typer.Option(..., help="JSON string with arguments for the call")
    ] = {},
    token: Optional[ParticipantToken] = None,
    permissions: Optional[ApiScope] = None,
    key: str,
):
    """
    Instruct an agent to 'call' a given URL with specific arguments.

    """
    if name is not None:
        print("[yellow]name is deprecated and should no longer be passed[/yellow]")

    if agent_name is not None:
        print(
            "[yellow]agent-name is deprecated and should no longer be passed, use participant-name instead[/yellow]"
        )
        participant_name = agent_name

    if participant_name is None:
        print("[red]--participant-name is required[/red]")
        raise typer.Exit(1)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        room = resolve_room(room)

        if token is None:
            token = ParticipantToken(
                name=participant_name,
            )
            token.add_api_grant(permissions or ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room)
            token.grants.append(ParticipantGrant(name="tunnel_ports", scope="9000"))

        if local is None:
            local = is_local_url(url)

        if local:
            async with aiohttp.ClientSession() as session:
                event = "room.call"
                data = {
                    "room_url": websocket_room_url(room_name=room),
                    "room_name": room,
                    "token": token.to_jwt(api_key=key),
                    "arguments": json.loads(arguments)
                    if isinstance(arguments, str)
                    else arguments,
                }

                await send_webhook(
                    session=session, url=url, event=event, data=data, secret=None
                )
        else:
            print("[bold green]Connecting to room...[/bold green]")
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(
                        room_name=room, base_url=meshagent_base_url()
                    ),
                    token=token.to_jwt(api_key=key),
                )
            ) as client:
                print("[bold green]Making agent call...[/bold green]")
                await client.agents.make_call(
                    name=participant_name, url=url, arguments=json.loads(arguments)
                )
                print("[bold cyan]Call request sent successfully.[/bold cyan]")

    finally:
        await account_client.close()
