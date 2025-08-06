import typer
from rich import print
from typing import Annotated
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption, RoomOption
from meshagent.api import ParticipantToken, ApiScope
from meshagent.cli.helper import resolve_project_id, resolve_api_key, resolve_room
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client
import pathlib

app = async_typer.AsyncTyper()


@app.async_command("generate")
async def generate(
    *,
    project_id: ProjectIdOption = None,
    token_path: Annotated[str, typer.Option()] = None,
    room: RoomOption,
    api_key_id: ApiKeyIdOption = None,
    name: Annotated[str, typer.Option()],
    role: str = "agent",
):
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id=project_id, api_key_id=api_key_id)
        room = resolve_room(room)
        key = (
            await client.decrypt_project_api_key(project_id=project_id, id=api_key_id)
        )["token"]

        token = ParticipantToken(
            name=name, project_id=project_id, api_key_id=api_key_id
        )

        token.add_role_grant(role=role)

        token.add_room_grant(room)
        token.add_api_grant(ApiScope.agent_default())

        if token_path is None:
            print(token.to_jwt(token=key))

        else:
            pathlib.Path(token_path).expanduser().resolve().write_text(
                token.to_jwt(token=key)
            )

    finally:
        await client.close()
