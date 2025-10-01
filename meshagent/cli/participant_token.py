import typer
from rich import print
from typing import Annotated
from meshagent.api import ParticipantToken
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_key, resolve_project_id
import pathlib
from typing import Optional
from meshagent.api.participant_token import ParticipantTokenSpec
from pydantic_yaml import parse_yaml_raw_as
from meshagent.cli.common_options import ProjectIdOption

app = async_typer.AsyncTyper()


@app.async_command("generate")
async def generate(
    *,
    project_id: ProjectIdOption = None,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="File path to a file"),
    ] = None,
    input: Annotated[
        str,
        typer.Option("--input", "-i", help="File path to a token spec"),
    ],
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    project_id = await resolve_project_id(project_id=project_id)
    key = await resolve_key(project_id=project_id, key=key)

    client = await get_client()
    try:
        with open(str(pathlib.Path(input).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ParticipantTokenSpec, f.read())

        token = ParticipantToken(
            name=spec.identity,
        )

        if spec.role is not None:
            token.add_role_grant(role=spec.role)
        if spec.room is not None:
            token.add_room_grant(spec.room)

        token.add_api_grant(spec.api)

        if output is None:
            print(token.to_jwt(api_key=key))

        else:
            pathlib.Path(output).expanduser().resolve().write_text(
                token.to_jwt(api_key=key)
            )

    finally:
        await client.close()
