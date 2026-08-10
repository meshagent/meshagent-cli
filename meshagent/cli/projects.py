import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, ConfigDict
from pydantic_yaml import parse_yaml_raw_as
from rich import print

from meshagent.api import ApiScope
from meshagent.api.client import Meshagent, NotFoundError, ProjectInfo, ProjectsPage
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import (
    get_client,
    get_active_project,
    print_json_table,
    resolve_project_id,
    set_active_project,
)

app = async_typer.AsyncTyper(help="Manage or activate your meshagent projects")


@dataclass(frozen=True, slots=True)
class ListedProject:
    id: str
    name: str
    is_active: bool


RoomAccessRole = Literal["guest", "viewer", "operator", "developer", "admin"]


class ParticipantTokenRoleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: ApiScope


class RoomRoleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant: ParticipantTokenRoleDefinition | None = None
    site_user: bool = False


class RoomRolesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["v1"]
    kind: Literal["RoomRoles"]
    roles: dict[RoomAccessRole, RoomRoleDefinition]


def _parse_listed_projects(
    response: ProjectsPage,
    *,
    active_project_id: str | None,
) -> list[ListedProject]:
    selectable_projects: list[ListedProject] = []
    for row in response.projects:
        resolved_project_id = row.id.strip()
        if resolved_project_id == "":
            continue

        resolved_project_name = row.name.strip() or resolved_project_id

        selectable_projects.append(
            ListedProject(
                id=resolved_project_id,
                name=resolved_project_name,
                is_active=resolved_project_id == active_project_id,
            )
        )

    return selectable_projects


async def _list_selectable_projects(
    client: Meshagent,
    *,
    active_project_id: str | None,
) -> list[ListedProject]:
    return _parse_listed_projects(
        await client.list_projects(),
        active_project_id=active_project_id,
    )


async def _create_project_id(
    client: Meshagent,
    *,
    project_name: str,
) -> str:
    created = await client.create_project(project_name)
    resolved_project_id = created.id.strip()
    if resolved_project_id == "":
        raise RuntimeError("Project creation did not return a valid id.")

    return resolved_project_id


def _project_id_from_payload(project: ProjectInfo) -> str:
    project_id = project.id.strip()
    if project_id == "":
        raise RuntimeError("Project lookup did not return a valid id.")
    return project_id


async def _resolve_project_id_or_key(client: Meshagent, selector: str) -> str:
    try:
        return _project_id_from_payload(await client.get_project(selector))
    except NotFoundError:
        project = await client.get_project_by_key(selector)
        return _project_id_from_payload(project)


def _should_launch_activate_tui(
    *,
    project_id: str | None,
    interactive: bool,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    return (project_id is None or interactive) and stdin_is_tty and stdout_is_tty


async def _run_project_activate_tui(
    *,
    selectable_projects: list[ListedProject],
):
    from meshagent.cli.tui.project_activate import (
        ProjectActivateProject,
        run_project_activate_tui,
    )

    return await run_project_activate_tui(
        projects=[
            ProjectActivateProject(
                id=project.id,
                name=project.name,
                is_active=project.is_active,
            )
            for project in selectable_projects
        ]
    )


async def _run_interactive_activate_prompt(
    *,
    client: Meshagent,
    selectable_projects: list[ListedProject],
) -> str | None:
    if len(selectable_projects) == 0:
        if typer.confirm(
            "There are no projects. Would you like to create one?",
            default=True,
        ):
            project_name = typer.prompt("Project name")
            return await _create_project_id(client, project_name=project_name)
        raise typer.Exit(code=0)

    for index, project in enumerate(selectable_projects, start=1):
        active_label = " (active)" if project.is_active else ""
        typer.echo(f"[{index}] {project.name} ({project.id}){active_label}")

    new_project_index = len(selectable_projects) + 1
    typer.echo(f"[{new_project_index}] Create a new project")
    exit_index = new_project_index + 1
    typer.echo(f"[{exit_index}] Exit")

    choice = typer.prompt("Select a project", type=int)
    if choice == exit_index:
        return None
    if choice == new_project_index:
        project_name = typer.prompt("Project name")
        return await _create_project_id(client, project_name=project_name)
    if 1 <= choice <= len(selectable_projects):
        return selectable_projects[choice - 1].id

    print("[red]Invalid selection[/red]")
    raise typer.Exit(code=1)


@app.async_command("create", help="Create a new MeshAgent project.")
async def create(name: str):
    client = await get_client()
    try:
        result = await client.create_project(name)
        print(f"[green]Project created:[/] {result.id}")
    finally:
        await client.close()


@app.async_command("list", help="List projects and mark the currently active one.")
async def list(
    o: OutputFormatOption = "table",
):
    client = await get_client()
    projects = await client.list_projects()
    active_project = await get_active_project()
    output = [
        project.model_dump(mode="json", exclude_none=True)
        for project in projects.projects
    ]
    for project in output:
        if project["id"] == active_project:
            project["name"] = "*" + str(project["name"])

    if o == "json":
        print({"projects": output})
    else:
        print_json_table(output, "id", "name", "project_key")
    await client.close()


@app.async_command("get", help="Get a MeshAgent project.")
async def get(
    project: Annotated[str, typer.Argument(help="Project id or key to get")],
    o: OutputFormatOption = "table",
):
    client = await get_client()
    try:
        project_id = await _resolve_project_id_or_key(client, project)
        project_info = await client.get_project(project_id)
        if o == "json":
            print(project_info.model_dump(mode="json", exclude_none=True))
        else:
            print_json_table(
                [project_info.model_dump(mode="json", exclude_none=True)],
                "id",
                "name",
                "project_key",
            )
    finally:
        await client.close()


@app.async_command(
    "set-room-roles",
    help="Set authoritative project room-role mappings from a YAML spec.",
)
async def set_room_roles(
    file: Annotated[Path, typer.Argument(help="RoomRoles YAML spec")],
    project_id: ProjectIdOption,
):
    try:
        spec = parse_yaml_raw_as(RoomRolesSpec, file.read_text())
    except (OSError, ValueError) as error:
        print(f"[red]Invalid room role spec: {error}[/red]")
        raise typer.Exit(code=1) from error

    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id)
        project = await client.get_project(resolved_project_id)
        settings = dict(project.settings or {})
        settings["room_roles"] = spec.model_dump(mode="json", exclude_none=True)[
            "roles"
        ]
        await client.update_project_settings(resolved_project_id, settings)
        print(f"[green]Room role override updated:[/] {resolved_project_id}")
    finally:
        await client.close()


@app.async_command(
    "reset-room-roles",
    help="Remove the project room-role override and restore built-in defaults.",
)
async def reset_room_roles(project_id: ProjectIdOption):
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id)
        project = await client.get_project(resolved_project_id)
        settings = dict(project.settings or {})
        settings.pop("room_roles", None)
        await client.update_project_settings(resolved_project_id, settings)
        print(f"[green]Room role defaults restored:[/] {resolved_project_id}")
    finally:
        await client.close()


@app.async_command(
    "activate", help="Set the active project for subsequent CLI commands."
)
async def activate(
    project_id: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Project id or key. If omitted, an interactive picker is shown in a TTY."
            ),
        ),
    ] = None,
    interactive: bool = typer.Option(
        False,
        "-i",
        "--interactive",
        help="Interactively select or create a project. Uses the TUI in a TTY.",
    ),
    return_project_id: Annotated[
        bool,
        typer.Option(
            "--return-project-id",
            hidden=True,
            help="Return the active project id for internal callers.",
        ),
    ] = False,
):
    client = await get_client()
    try:
        selected_project_id = project_id

        if _should_launch_activate_tui(
            project_id=project_id,
            interactive=interactive,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
        ):
            selectable_projects = await _list_selectable_projects(
                client,
                active_project_id=await get_active_project(),
            )
            result = await _run_project_activate_tui(
                selectable_projects=selectable_projects,
            )
            if result.status != "completed":
                if result.message is not None:
                    print(result.message)
                return None

            if result.new_project_name is not None:
                selected_project_id = await _create_project_id(
                    client,
                    project_name=result.new_project_name,
                )
            elif result.selected_project_id is not None:
                selected_project_id = result.selected_project_id
        elif interactive:
            selectable_projects = await _list_selectable_projects(
                client,
                active_project_id=await get_active_project(),
            )
            selected_project_id = await _run_interactive_activate_prompt(
                client=client,
                selectable_projects=selectable_projects,
            )

        if selected_project_id is None:
            print("[red]project_id required[/red]")
            raise typer.Exit(code=1)

        try:
            resolved_project_id = await _resolve_project_id_or_key(
                client, selected_project_id
            )
        except NotFoundError:
            print(f"[red]Invalid project id or key: {selected_project_id}[/red]")
            raise typer.Exit(code=1)

        await set_active_project(project_id=resolved_project_id)
        if return_project_id:
            return resolved_project_id
        print(resolved_project_id)
        return None
    finally:
        await client.close()
