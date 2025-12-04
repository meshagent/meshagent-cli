from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
)
from meshagent.api.oauth import OAuthClientConfig
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from rich import print
from typing import Annotated, Optional
import typer
import json

app = async_typer.AsyncTyper(help="OAuth2 test commands")


@app.async_command("request")
async def oauth2(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    from_participant_id: Annotated[str, typer.Option()],
    client_id: Annotated[str, typer.Option()],
    authorization_endpoint: Annotated[str, typer.Option()],
    token_endpoint: Annotated[str, typer.Option()],
    scopes: Annotated[Optional[str], typer.Option()] = None,
    client_secret: Annotated[Optional[str], typer.Option()],
    redirect_uri: Annotated[Optional[str], typer.Option()],
    pkce: Annotated[bool, typer.Option()] = True,
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
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
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


@app.async_command("get")
async def get(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    delegated_to: Annotated[str, typer.Option()],
    client_id: Annotated[str, typer.Option()],
    authorization_endpoint: Annotated[str, typer.Option()],
    token_endpoint: Annotated[str, typer.Option()],
    scopes: Annotated[Optional[str], typer.Option()] = None,
    client_secret: Annotated[Optional[str], typer.Option()],
    redirect_uri: Annotated[Optional[str], typer.Option()],
    pkce: Annotated[bool, typer.Option()] = True,
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
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            print("[green]Requesting OAuth token from consumer side...[/green]")
            token = await consumer.secrets.get_offline_oauth_token(
                oauth=OAuthClientConfig(
                    client_id=client_id,
                    authorization_endpoint=authorization_endpoint,
                    token_endpoint=token_endpoint,
                    scopes=scopes.split(",") if scopes is not None else scopes,
                    client_secret=client_secret,
                    no_pkce=not pkce,
                ),
                delegated_to=delegated_to,
            )

            print(f"[bold cyan]Got access token:[/bold cyan] {token}")

    finally:
        await account_client.close()


@app.async_command("list")
async def list(
    *,
    project_id: ProjectIdOption = None,
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
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            secrets = await consumer.secrets.list_user_secrets()
            output = []
            for s in secrets:
                output.append(s.model_dump(mode="json"))

            print(json.dumps(output, indent=2))

    finally:
        await account_client.close()


@app.async_command("delete")
async def delete(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    id: str,
    delegated_to: Annotated[
        str, typer.Option(help="The value of the delegated_to field of the secret")
    ],
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
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt_consumer.jwt,
            )
        ) as consumer:
            await consumer.secrets.delete_user_secret(id=id, delegated_to=delegated_to)
            print("deleted secret")

    finally:
        await account_client.close()
