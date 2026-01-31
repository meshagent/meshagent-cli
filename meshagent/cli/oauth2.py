from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
)
from meshagent.api.oauth import OAuthClientConfig
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import websocket_room_url
from rich import print
from typing import Annotated, Optional
from pathlib import Path
import typer
import json
import sys

app = async_typer.AsyncTyper(help="OAuth2 test commands")


def _read_bytes(*, input_path: str) -> bytes:
    if input_path == "-":
        return sys.stdin.buffer.read()
    return Path(input_path).expanduser().resolve().read_bytes()


def _write_bytes(*, output_path: str, data: bytes) -> None:
    if output_path == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    Path(output_path).expanduser().resolve().write_bytes(data)


@app.async_command("oauth")
async def oauth2(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    from_participant_id: Annotated[
        str,
        typer.Option(..., help="Participant ID to request the token from"),
    ],
    client_id: Annotated[str, typer.Option(..., help="OAuth client ID")],
    authorization_endpoint: Annotated[
        str, typer.Option(..., help="OAuth authorization endpoint URL")
    ],
    token_endpoint: Annotated[str, typer.Option(..., help="OAuth token endpoint URL")],
    scopes: Annotated[
        Optional[str], typer.Option(help="Comma-separated OAuth scopes")
    ] = None,
    client_secret: Annotated[
        Optional[str], typer.Option(help="OAuth client secret (if required)")
    ],
    redirect_uri: Annotated[
        Optional[str], typer.Option(help="Redirect URI for the OAuth flow")
    ],
    pkce: Annotated[bool, typer.Option(help="Use PKCE (recommended)")] = True,
):
    """
    Run an OAuth2 request test between two participants in the same room.
    One will act as the consumer, the other as the provider.
    """

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            print("[green]Requesting OAuth token from consumer side...[/green]")
            token = await consumer.secrets.request_oauth_token(
                oauth=OAuthClientConfig(
                    client_id=client_id,
                    authorization_endpoint=authorization_endpoint,
                    token_endpoint=token_endpoint,
                    scopes=scopes.split(",") if scopes is not None else scopes,
                    client_secret=client_secret,
                    no_pkce=not pkce,
                ),
                from_participant_id=from_participant_id,
                timeout=300,
                redirect_uri=redirect_uri,
            )

            print(f"[bold cyan]Got access token:[/bold cyan] {token}")

    finally:
        await account_client.close()


@app.async_command("request")
async def secret_request(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    from_participant_id: Annotated[
        str,
        typer.Option(..., help="Participant ID to request the secret from"),
    ],
    url: Annotated[str, typer.Option(..., help="Secret URL identifier")],
    type: Annotated[
        str, typer.Option("--type", help="Secret type")
    ] = "application/octet-stream",
    delegate_to: Annotated[
        Optional[str],
        typer.Option(help="Delegate secret to this participant name"),
    ] = None,
    timeout: Annotated[int, typer.Option(help="Timeout in seconds")] = 300,
    out: Annotated[
        str,
        typer.Option(
            "--out",
            "-o",
            help="Output file path, or '-' for stdout",
        ),
    ] = "-",
):
    """Request a secret from another participant."""

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            typer.echo(
                f"Requesting secret from participant {from_participant_id}...",
                err=True,
            )
            secret = await consumer.secrets.request_secret(
                url=url,
                type=type,
                timeout=timeout,
                from_participant_id=from_participant_id,
                delegate_to=delegate_to,
            )

            _write_bytes(output_path=out, data=secret)
            if out != "-":
                typer.echo(f"Wrote {len(secret)} bytes to {out}", err=True)

    finally:
        await account_client.close()


@app.async_command("get")
async def secret_get(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    secret_id: Annotated[str, typer.Option(..., help="Secret ID")],
    delegated_to: Annotated[
        Optional[str],
        typer.Option(help="Fetch a secret delegated to this participant name"),
    ] = None,
    out: Annotated[
        str,
        typer.Option(
            "--out",
            "-o",
            help="Output file path, or '-' for stdout",
        ),
    ] = "-",
):
    """Get a stored secret by secret id."""

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            resp = await consumer.secrets.get_secret(
                secret_id=secret_id,
                delegated_to=delegated_to,
            )

            if resp is None:
                typer.echo("Secret not found", err=True)
                raise typer.Exit(1)

            typer.echo(
                f"Got secret name={resp.name} mime_type={resp.mime_type} bytes={len(resp.data)}",
                err=True,
            )
            _write_bytes(output_path=out, data=resp.data)
            if out != "-":
                typer.echo(f"Wrote {len(resp.data)} bytes to {out}", err=True)

    finally:
        await account_client.close()


@app.async_command("set")
async def secret_set(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    secret_id: Annotated[str, typer.Option(..., help="Secret ID")],
    type: Annotated[
        Optional[str],
        typer.Option("--type", help="Secret type"),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option(help="Optional secret name"),
    ] = None,
    delegated_to: Annotated[
        Optional[str],
        typer.Option(help="Store a secret delegated to this participant name"),
    ] = None,
    input_path: Annotated[
        str,
        typer.Option(
            "--in",
            "-i",
            help="Input file path, or '-' for stdin",
        ),
    ] = "-",
):
    """Set/store a secret (bytes from stdin or file)."""

    secret_bytes = _read_bytes(input_path=input_path)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            await consumer.secrets.set_secret(
                secret_id=secret_id,
                type=type,
                name=name,
                delegated_to=delegated_to,
                data=secret_bytes,
            )

            typer.echo(f"Stored {len(secret_bytes)} bytes", err=True)

    finally:
        await account_client.close()


@app.async_command("list")
async def list(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
):
    """
    list secrets
    """

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            secrets = await consumer.secrets.list_secrets()
            output = []
            for s in secrets:
                output.append(s.model_dump(mode="json"))

            print(json.dumps(output, indent=2))

    finally:
        await account_client.close()


@app.async_command("delete")
async def delete(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    id: str,
    delegated_to: Annotated[
        Optional[str],
        typer.Option(
            help="The value of the delegated_to field of the secret, must be specified if secret was delegated"
        ),
    ] = None,
):
    """
    delete a secret
    """

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        jwt_consumer = await account_client.connect_room(
            project_id=project_id, room=room
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            await consumer.secrets.delete_secret(id=id, delegated_to=delegated_to)
            print("deleted secret")

    finally:
        await account_client.close()
