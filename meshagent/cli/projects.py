import typer
from rich import print
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    print_json_table,
    set_active_project,
    get_active_project,
)
from meshagent.cli.common_options import OutputFormatOption

app = async_typer.AsyncTyper(help="Manage or activate your meshagent projects")


@app.async_command("create")
async def create(name: str):
    client = await get_client()
    try:
        result = await client.create_project(name)
        print(f"[green]Project created:[/] {result['id']}")
    finally:
        await client.close()


@app.async_command("list")
async def list(
    o: OutputFormatOption = "table",
):
    client = await get_client()
    projects = await client.list_projects()
    active_project = await get_active_project()
    for project in projects["projects"]:
        if project["id"] == active_project:
            project["name"] = "*" + project["name"]

    if o == "json":
        print(projects)
    else:
        print_json_table(projects["projects"], "id", "name")
    await client.close()


@app.async_command("activate")
async def activate(
    project_id: str | None = typer.Argument(None),
    interactive: bool = typer.Option(
        False,
        "-i",
        "--interactive",
        help="Interactively select or create a project",
    ),
):
    client = await get_client()
    try:
        if interactive:
            response = await client.list_projects()
            projects = response["projects"]

            if not projects:
                if typer.confirm(
                    "There are no projects. Would you like to create one?",
                    default=True,
                ):
                    name = typer.prompt("Project name")
                    created = await client.create_project(name)
                    project_id = created["id"]
                else:
                    raise typer.Exit(code=0)
            else:
                for idx, proj in enumerate(projects, start=1):
                    print(f"[{idx}] {proj['name']} ({proj['id']})")
                new_project_index = len(projects) + 1
                print(f"[{new_project_index}] Create a new project")
                exit_index = new_project_index + 1
                print(f"[{exit_index}] Exit")

                choice = typer.prompt("Select a project", type=int)
                if choice == exit_index:
                    return
                elif choice == new_project_index:
                    name = typer.prompt("Project name")
                    # TODO: validate name
                    created = await client.create_project(name)
                    project_id = created["id"]
                elif 1 <= choice <= len(projects):
                    project_id = projects[choice - 1]["id"]
                else:
                    print("[red]Invalid selection[/red]")
                    raise typer.Exit(code=1)

        if project_id is None and not interactive:
            print("[red]project_id required[/red]")
            raise typer.Exit(code=1)

        if project_id is not None:
            projects = (await client.list_projects())["projects"]
            for project in projects:
                if project["id"] == project_id:
                    await set_active_project(project_id=project_id)
                    return project_id

            print(f"[red]Invalid project id: {project_id}[/red]")
            raise typer.Exit(code=1)
    finally:
        await client.close()
