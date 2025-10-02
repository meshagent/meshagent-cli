import json
from rich import print

from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
    set_active_api_key,
)
from meshagent.cli.common_options import OutputFormatOption
from typing import Annotated
import typer

app = async_typer.AsyncTyper(help="Manage or activate api-keys for your project")


@app.async_command("list")
async def list(
    *,
    project_id: ProjectIdOption = None,
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
            print_json_table(keys, "id", "name", "description")
    else:
        print("There are not currently any API keys in the project")
    await client.close()


@app.async_command("create")
async def create(
    *,
    project_id: ProjectIdOption = None,
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


@app.async_command("activate")
async def activate(
    *,
    project_id: ProjectIdOption = None,
    key: str,
):
    project_id = await resolve_project_id(project_id=project_id)
    if activate:
        await set_active_api_key(project_id=project_id, key=key)


@app.async_command("delete")
async def delete(*, project_id: ProjectIdOption = None, id: str):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    await client.delete_api_key(project_id=project_id, id=id)
    await client.close()
