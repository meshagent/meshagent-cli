import json
import shlex
from binascii import Error as BinasciiError
from typing import Annotated

import typer
from rich import print

from meshagent.api.keys import parse_api_key
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_active_api_key,
    get_client,
    print_json_table,
    resolve_project_id,
    set_active_api_key,
)

app = async_typer.AsyncTyper(help="Manage or activate api-keys for your project")


async def _require_active_api_key(*, project_id: str | None) -> str:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    key = await get_active_api_key(project_id=resolved_project_id)
    if key is None:
        print(
            f"[red]No activated API key found for project {resolved_project_id}. "
            "Use meshagent api-key activate or meshagent api-key create "
            "--activate to store one locally.[/red]"
        )
        raise typer.Exit(code=1)
    return key


async def _get_active_api_key_id(*, project_id: str) -> str | None:
    active_key = await get_active_api_key(project_id=project_id)
    if active_key is None:
        return None

    try:
        parsed_key = parse_api_key(active_key)
    except (BinasciiError, IndexError, ValueError):
        return None

    if parsed_key.project_id != project_id:
        return None

    return parsed_key.id


@app.async_command("list", help="List API keys for a project.")
async def list(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    keys = (await client.list_api_keys(project_id=project_id))["keys"]

    if len(keys) > 0:
        if o == "json":
            sanitized_keys = [
                {k: v for k, v in key.items() if k != "created_by"} for key in keys
            ]
            print(json.dumps({"api-keys": sanitized_keys}, indent=2))
        else:
            active_key_id = await _get_active_api_key_id(project_id=project_id)
            table_keys = [
                {
                    "active": "*" if key.get("id") == active_key_id else "",
                    **key,
                }
                for key in keys
            ]
            print_json_table(table_keys, "active", "id", "name", "description")
    else:
        print("There are not currently any API keys in the project")
    await client.close()


@app.async_command("create", help="Create a new API key for a project.")
async def create(
    *,
    project_id: ProjectIdOption,
    name: str,
    description: Annotated[
        str, typer.Option(..., help="a description for the api key")
    ] = "",
    activate: Annotated[
        bool,
        typer.Option(
            ..., help="use this key by default for commands that accept an API key"
        ),
    ] = False,
    silent: Annotated[bool, typer.Option(..., help="do not print api key")] = False,
):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    api_key = await client.create_api_key(
        project_id=project_id, name=name, description=description
    )
    if not silent:
        if not activate:
            print(
                "[green]This is your token. Save it for later, you will not be able to get the value again:[/green]\n"
            )
            print(api_key["value"])
            print(
                "[green]\nNote: you can use the --activate flag to save a key in your local project settings when creating a key.[/green]\n"
            )
        else:
            print("[green]This is your token:[/green]\n")
            print(api_key["value"])

    await client.close()
    if activate:
        await set_active_api_key(project_id=project_id, key=api_key["value"])
        print(
            "[green]your api key has been activated and will be used automatically with commands that require a key[/green]\n"
        )


@app.async_command("get", help="Get the activated API key for a project.")
async def get(*, project_id: ProjectIdOption):
    key = await _require_active_api_key(project_id=project_id)
    typer.echo(key)


@app.async_command(
    "env",
    help="Print the activated API key as a shell export snippet.",
)
async def env(*, project_id: ProjectIdOption):
    key = await _require_active_api_key(project_id=project_id)
    typer.echo(f"export MESHAGENT_API_KEY={shlex.quote(key)}")


@app.async_command(
    "activate",
    help="Set the default API key for a project in local CLI settings.",
)
async def activate(
    *,
    project_id: ProjectIdOption,
    key: str,
):
    project_id = await resolve_project_id(project_id=project_id)
    await set_active_api_key(project_id=project_id, key=key)


@app.async_command("delete", help="Delete an API key from a project.")
async def delete(*, project_id: ProjectIdOption, id: str):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    await client.delete_api_key(project_id=project_id, id=id)
    await client.close()
