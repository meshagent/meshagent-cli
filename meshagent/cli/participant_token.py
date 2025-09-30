import typer
from rich import print
from typing import Annotated
from meshagent.api import ParticipantToken
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_key
import pathlib
from typing import Optional
from meshagent.api.participant_token import ParticipantTokenSpec
from meshagent.api.keys import parse_api_key
from pydantic_yaml import parse_yaml_raw_as

app = async_typer.AsyncTyper()


@app.async_command("generate")
async def generate(
    *,
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
    key = await resolve_key(key)

    client = await get_client()
    try:
        parsed_key = parse_api_key(key)
        project_id = await parsed_key.project_id

        with open(str(pathlib.Path(input).expanduser().resolve()), "rb") as f:
            spec = parse_yaml_raw_as(ParticipantTokenSpec, f.read())

        token = ParticipantToken(
            name=spec.identity, project_id=project_id, api_key_id=parsed_key.id
        )

        if spec.role is not None:
            token.add_role_grant(role=spec.role)
        if spec.room is not None:
            token.add_room_grant(spec.room)

        token.add_api_grant(spec.api)

        if output is None:
            print(token.to_jwt(token=parsed_key.secret))

        else:
            pathlib.Path(output).expanduser().resolve().write_text(
                token.to_jwt(token=parsed_key.secret)
            )

    finally:
        await client.close()
