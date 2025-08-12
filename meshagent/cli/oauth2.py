from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
    resolve_token_jwt,
)
from meshagent.api import RoomClient, WebSocketClientProtocol
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from rich import print
from typing import Annotated, Optional
import typer

app = async_typer.AsyncTyper(help="OAuth2 test commands")


@app.async_command("request")
async def oauth2(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    token_path: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option()] = "cli",
    from_participant_id: Annotated[str, typer.Option()],
    client_id: Annotated[str, typer.Option()],
    authorization_endpoint: Annotated[str, typer.Option()],
    token_endpoint: Annotated[str, typer.Option()],
    role: str = "user",
    scopes: Annotated[Optional[str], typer.Option()] = None,
    client_secret: Annotated[Optional[str], typer.Option()],
    redirect_uri: Annotated[Optional[str], typer.Option()],
):
    """
    Run an OAuth2 request test between two participants in the same room.
    One will act as the consumer, the other as the provider.
    """

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        jwt_consumer = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=f"{name}-consumer",
            role=role,
            room=room,
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt_consumer,
            )
        ) as consumer:
            print("[green]Requesting OAuth token from consumer side...[/green]")
            token = await consumer.secrets.request_oauth_token(
                client_id=client_id,
                authorization_endpoint=authorization_endpoint,
                token_endpoint=token_endpoint,
                scopes=scopes.split(",") if scopes is not None else scopes,
                from_participant_id=from_participant_id,
                timeout=10,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )

            print(f"[bold cyan]Got access token:[/bold cyan] {token}")

    finally:
        await account_client.close()
