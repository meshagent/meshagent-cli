import typer
from rich import print
from typing import Annotated
from meshagent.cli import async_typer
from meshagent.cli.helper import mint_participant_token_for_cli, resolve_project_id
import pathlib
from typing import Optional
from meshagent.api.participant_token import ParticipantTokenSpec
from pydantic_yaml import parse_yaml_raw_as
from meshagent.cli.common_options import ProjectIdOption

app = async_typer.AsyncTyper(help="Generate participant tokens (JWTs)")


@app.async_command("generate", help="Generate a participant token (JWT) from a spec")
async def generate(
    *,
    project_id: ProjectIdOption,
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
    """Generate a signed participant token (JWT) from a YAML spec."""

    project_id = await resolve_project_id(project_id=project_id)
    with open(str(pathlib.Path(input).expanduser().resolve()), "rb") as f:
        spec = parse_yaml_raw_as(ParticipantTokenSpec, f.read())

    jwt = await mint_participant_token_for_cli(
        project_id=project_id,
        name=spec.identity,
        room_name=spec.room,
        role=spec.role,
        api_scope=spec.api,
        key=key,
    )

    if output is None:
        print(jwt)
    else:
        pathlib.Path(output).expanduser().resolve().write_text(jwt)
