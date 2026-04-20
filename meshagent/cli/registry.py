from __future__ import annotations

from typing import Annotated, Optional

import json

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.api.client import (
    CreateProjectRepositoryRequest,
    UpdateProjectRepositoryRequest,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(help="Manage registries for your project")


def _parse_annotations(annotations: Optional[str]) -> Optional[dict[str, str]]:
    if annotations is None:
        return None
    if annotations.strip() == "":
        return {}
    try:
        parsed = json.loads(annotations)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("Invalid JSON for --annotations") from exc

    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise typer.BadParameter(
            "--annotations must be a JSON object with string keys and string values"
        )
    return parsed


@app.async_command("create")
async def registry_create(
    *,
    project_id: ProjectIdOption,
    name: Annotated[
        str,
        typer.Option(
            "--name",
            "-n",
            help="Repository path in the public registry, for example 'apps/demo'",
        ),
    ],
    description: Annotated[
        str,
        typer.Option(
            "--description",
            "-d",
            help="Human-readable description",
        ),
    ] = "",
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-a",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Create a project registry repository."""
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        repository = await client.create_repository(
            project_id=resolved_project_id,
            repository=CreateProjectRepositoryRequest(
                name=name,
                description=description,
                annotations=_parse_annotations(annotations) or {},
            ),
        )
        print(f"[green]Created registry:[/] {repository.name} ({repository.id})")
    finally:
        await client.close()


@app.async_command("update")
async def registry_update(
    *,
    project_id: ProjectIdOption,
    repository_id: Annotated[
        str,
        typer.Argument(help="Repository id to update"),
    ],
    name: Annotated[
        Optional[str],
        typer.Option(
            "--name",
            "-n",
            help="Updated repository path",
        ),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(
            "--description",
            "-d",
            help="Updated description",
        ),
    ] = None,
    annotations: Annotated[
        Optional[str],
        typer.Option(
            "--annotations",
            "-a",
            help='annotations in json format {"name":"value"}',
        ),
    ] = None,
):
    """Update a project registry repository."""
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        parsed_annotations = _parse_annotations(annotations)

        try:
            current = await client.get_repository(
                project_id=resolved_project_id,
                repository_id=repository_id,
            )
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Registry not found:[/] {repository_id}")
                raise typer.Exit(code=1)
            raise

        repository = await client.update_repository(
            project_id=resolved_project_id,
            repository_id=repository_id,
            repository=UpdateProjectRepositoryRequest(
                name=name if name is not None else current.name,
                description=(
                    description if description is not None else current.description
                ),
                annotations=(
                    parsed_annotations
                    if parsed_annotations is not None
                    else current.annotations
                ),
            ),
        )
        print(f"[green]Updated registry:[/] {repository.name} ({repository.id})")
    finally:
        await client.close()


@app.async_command("show")
async def registry_show(
    *,
    project_id: ProjectIdOption,
    repository_id: Annotated[
        str,
        typer.Argument(help="Repository id to show"),
    ],
):
    """Show registry details."""
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        try:
            repository = await client.get_repository(
                project_id=resolved_project_id,
                repository_id=repository_id,
            )
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Registry not found:[/] {repository_id}")
                raise typer.Exit(code=1)
            raise
        print(repository.model_dump(mode="json"))
    finally:
        await client.close()


@app.async_command("list")
async def registry_list(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
):
    """List registries for the project."""
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        repositories = await client.list_repositories(project_id=resolved_project_id)
        if o == "json":
            print(
                {
                    "repositories": [
                        repository.model_dump(mode="json")
                        for repository in repositories
                    ]
                }
            )
        else:
            print_json_table(
                [
                    {
                        "id": repository.id,
                        "name": repository.name,
                        "description": repository.description,
                        "created_at": repository.created_at.isoformat(),
                    }
                    for repository in repositories
                ],
                "id",
                "name",
                "description",
                "created_at",
            )
    finally:
        await client.close()


@app.async_command("delete")
async def registry_delete(
    *,
    project_id: ProjectIdOption,
    repository_id: Annotated[
        str,
        typer.Argument(help="Repository id to delete"),
    ],
):
    """Delete a project registry repository."""
    client = await get_client()
    try:
        resolved_project_id = await resolve_project_id(project_id=project_id)
        try:
            await client.delete_repository(
                project_id=resolved_project_id,
                repository_id=repository_id,
            )
        except ClientResponseError as exc:
            if exc.status == 404:
                print(f"[red]Registry not found:[/] {repository_id}")
                raise typer.Exit(code=1)
            raise
        print(f"[green]Deleted registry:[/] {repository_id}")
    finally:
        await client.close()
