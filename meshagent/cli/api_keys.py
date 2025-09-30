import json
from rich import print

from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    resolve_project_id,
)
from meshagent.cli.common_options import OutputFormatOption


app = async_typer.AsyncTyper(help="Manage or activate api-keys for your project")


@app.async_command("list")
async def list(
    *,
    project_id: ProjectIdOption = None,
    o: OutputFormatOption = "table",
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
async def create(
    *, project_id: ProjectIdOption = None, name: str, description: str = ""
):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    api_key = await client.create_project_api_key(
        project_id=project_id, name=name, description=description
    )
    print(
        "[green]This is your token save it for later, you will not be able to get the value again:[/green]"
    )
    print(api_key["value"])
    await client.close()


@app.async_command("delete")
async def delete(*, project_id: ProjectIdOption = None, id: str):
    project_id = await resolve_project_id(project_id=project_id)

    client = await get_client()
    await client.delete_project_api_key(project_id=project_id, id=id)
    await client.close()
