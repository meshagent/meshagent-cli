import typer
import json
from rich import print
from typing import Annotated
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, print_json_table
from meshagent.cli.helper import resolve_project_id, set_active_api_key

app = async_typer.AsyncTyper()


@app.async_command("list")
async def list(
    *,
    project_id: str = None,
    o: Annotated[
        str,
        typer.Option(
            "--output",
            "-o",
            help="output format [json|table]",
        ),
    ] = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    keys = (await client.list_project_api_keys(project_id=project_id))["keys"]
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
async def create(*, project_id: str = None, name: str, description: str = ""):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    api_key = await client.create_project_api_key(
        project_id=project_id, name=name, description=description
    )
    print(api_key["token"])
    await client.close()


@app.async_command("delete")
async def delete(*, project_id: str = None, id: str):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    await client.delete_project_api_key(project_id=project_id, id=id)
    await client.close()


@app.async_command("show")
async def show(*, project_id: str = None, api_key_id: str):
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        key = await client.decrypt_project_api_key(project_id=project_id, id=api_key_id)

        print(key["token"])

    finally:
        await client.close()


@app.async_command("activate")
async def activate(
    api_key_id: str | None = typer.Argument(None),
    project_id: str = None,
    interactive: bool = typer.Option(
        False,
        "-i",
        "--interactive",
        help="Interactively select or create an api key",
    ),
):
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id)
        if interactive:
            response = await client.list_project_api_keys(project_id=project_id)
            api_keys = response["keys"]

            if not api_keys:
                if typer.confirm(
                    "There are no API keys. Would you like to create one?",
                    default=True,
                ):
                    name = typer.prompt("API key name")
                    created = await client.create_project_api_key(
                        project_id=project_id,
                        name=name,
                        description="",
                    )
                    api_key_id = created["id"]
                else:
                    raise typer.Exit(code=0)
            else:
                for idx, key in enumerate(api_keys, start=1):
                    print(f"[{idx}] {key['name']} ({key['id']})")
                new_key_index = len(api_keys) + 1
                print(f"[{new_key_index}] Create a new api key")
                exit_index = new_key_index + 1
                print(f"[{exit_index}] Exit")

                choice = typer.prompt("Select an api key", type=int)
                if choice == exit_index:
                    return
                elif choice == new_key_index:
                    name = typer.prompt("API key name")
                    created = await client.create_project_api_key(
                        project_id=project_id,
                        name=name,
                        description="",
                    )
                    api_key_id = created["id"]
                elif 1 <= choice <= len(api_keys):
                    api_key_id = api_keys[choice - 1]["id"]
                else:
                    print("[red]Invalid selection[/red]")
                    raise typer.Exit(code=1)

        if api_key_id is None and not interactive:
            print("[red]api_key_id required[/red]")
            raise typer.Exit(code=1)

        response = await client.list_project_api_keys(project_id=project_id)
        api_keys = response["keys"]
        for api_key in api_keys:
            if api_key["id"] == api_key_id:
                await set_active_api_key(project_id=project_id, api_key_id=api_key_id)
                return api_key_id

        print(f"[red]Invalid api key id or project id: {project_id}[/red]")
        raise typer.Exit(code=1)
    finally:
        await client.close()
