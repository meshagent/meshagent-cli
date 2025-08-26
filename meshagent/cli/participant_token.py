import typer
from rich import print
from typing import Annotated
from meshagent.cli.common_options import ProjectIdOption, ApiKeyIdOption
from meshagent.api import ParticipantToken
from meshagent.cli.helper import resolve_project_id, resolve_api_key
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client
import pathlib
from typing import Optional
from meshagent.api.participant_token import ParticipantTokenSpec

from pydantic_yaml import parse_yaml_raw_as

app = async_typer.AsyncTyper()


@app.async_command("generate")
async def generate(
    *,
    project_id: ProjectIdOption = None,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="File path to a service definition"),
    ] = None,
    api_key_id: ApiKeyIdOption = None,
    file: Annotated[
        str,
        typer.Option("--input", "-i", help="File path to a service definition"),
    ],
):
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id=project_id, api_key_id=api_key_id)
        key = (
            await client.decrypt_project_api_key(project_id=project_id, id=api_key_id)
        )["token"]

        with open(str(pathlib.Path(file).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ParticipantTokenSpec, f.read())

        token = ParticipantToken(
            name=spec.identity, project_id=project_id, api_key_id=api_key_id
        )

        if spec.role is not None:
            token.add_role_grant(role=spec.role)
        if spec.room is not None:
            token.add_room_grant(spec.room)

        token.add_api_grant(spec.api)

        if output is None:
            print(token.to_jwt(token=key))

        else:
            pathlib.Path(output).expanduser().resolve().write_text(
                token.to_jwt(token=key)
            )

    finally:
        await client.close()
