import json
import math
from typing import Annotated

import typer
from pydantic import JsonValue, TypeAdapter
from rich import print
from rich.console import Console
from rich.table import Table
from rich.text import Text

from meshagent.api import RoomException
from meshagent.api.client import ProjectMembersPage, User
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id

app = async_typer.AsyncTyper(help="List project users and manage user profiles")
UserIdArgument = Annotated[
    str, typer.Argument(help="User id, or me for your own profile")
]


def _validate_output(output: str) -> None:
    if output not in {"table", "json"}:
        raise typer.BadParameter("expected one of: json, table", param_hint="--output")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not a JSON value")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{value} is not a finite JSON number")
    return parsed


def _parse_object(value: str | None, label: str) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(
            value,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except ValueError as error:
        raise typer.BadParameter(
            f"Invalid {label} JSON: {error}", param_hint=f"--{label}"
        ) from error
    if not isinstance(parsed, dict):
        raise typer.BadParameter("expected a JSON object", param_hint=f"--{label}")
    return TypeAdapter(dict[str, JsonValue]).validate_python(parsed)


def _parse_annotations(value: str | None) -> dict[str, str] | None:
    parsed = _parse_object(value, "annotations")
    if parsed is None:
        return None
    annotations: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(item, str):
            raise typer.BadParameter(
                "expected string keys and values", param_hint="--annotations"
            )
        annotations[key] = item
    return annotations


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2))


def _print_table(records: list[dict[str, object]], *columns: str) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    for column in columns:
        table.add_column(column.title())
    for row in records:
        table.add_row(
            *(
                Text(str(row[column]) if row[column] is not None else "")
                for column in columns
            )
        )
    Console().print(table)


@app.async_command("list", help="List project users and their direct roles.")
async def list_users(
    *,
    project_id: ProjectIdOption,
    count: Annotated[
        int, typer.Option("--count", min=1, help="Maximum number of users to return")
    ] = 100,
    filter: Annotated[
        str | None, typer.Option("--filter", help="Filter by email or name")
    ] = None,
    continuation_token: Annotated[
        str | None,
        typer.Option("--continuation-token", help="Resume a previous listing"),
    ] = None,
    o: OutputFormatOption = "table",
) -> None:
    _validate_output(o)
    project_id = await resolve_project_id(project_id=project_id)
    result = ProjectMembersPage(users=[], continuation_token=continuation_token)
    client = await get_client()
    try:
        while len(result.users) < count:
            page = await client.get_users_in_project_page(
                project_id,
                page_size=min(count - len(result.users), 100),
                continuation_token=result.continuation_token,
                filter=filter,
            )
            if (
                page.continuation_token is not None
                and page.continuation_token == result.continuation_token
            ):
                raise RoomException("User listing continuation token did not advance")
            result.users.extend(page.users)
            result.continuation_token = page.continuation_token
            if result.continuation_token is None:
                break
    finally:
        await client.close()
    if o == "json":
        _print_json(result.model_dump(mode="json"))
        return
    if not result.users:
        print("No users found")
        return
    _print_table(
        [
            {
                "id": member.user.id,
                "email": member.user.email,
                "first_name": member.user.first_name,
                "last_name": member.user.last_name,
                "direct_roles": ", ".join(member.direct_roles),
            }
            for member in result.users
        ],
        "id",
        "email",
        "first_name",
        "last_name",
        "direct_roles",
    )
    if result.continuation_token is not None:
        typer.echo(f"Continuation token: {result.continuation_token}")


@app.async_command(
    "get", help="Get a user profile, including metadata and annotations."
)
async def get(
    *, user_id: UserIdArgument = "me", o: OutputFormatOption = "table"
) -> None:
    _validate_output(o)
    client = await get_client()
    try:
        user = User.model_validate(await client.get_user_profile(user_id))
    finally:
        await client.close()
    row = user.model_dump(mode="json")
    if o == "json":
        _print_json(row)
        return
    row["metadata"] = json.dumps(user.metadata)
    row["annotations"] = json.dumps(user.annotations)
    _print_table(
        [row], "id", "email", "first_name", "last_name", "metadata", "annotations"
    )


@app.async_command(
    "update", help="Update supplied fields; supplied JSON maps replace existing maps."
)
async def update(
    *,
    project_id: ProjectIdOption,
    user_id: UserIdArgument = "me",
    first_name: Annotated[
        str | None, typer.Option("--first-name", help="First name")
    ] = None,
    last_name: Annotated[
        str | None, typer.Option("--last-name", help="Last name")
    ] = None,
    metadata: Annotated[
        str | None,
        typer.Option(
            "--metadata", help="Replacement metadata JSON object; {} clears it"
        ),
    ] = None,
    annotations: Annotated[
        str | None,
        typer.Option(
            "--annotations", help="Replacement string-valued JSON object; {} clears it"
        ),
    ] = None,
    o: OutputFormatOption = "table",
) -> None:
    _validate_output(o)
    if all(value is None for value in (first_name, last_name, metadata, annotations)):
        raise typer.BadParameter("supply at least one profile field to update")
    parsed_metadata = _parse_object(metadata, "metadata")
    parsed_annotations = _parse_annotations(annotations)
    if parsed_annotations is not None:
        project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        result = await client.update_user_profile(
            user_id,
            first_name=first_name,
            last_name=last_name,
            metadata=parsed_metadata,
            annotations=parsed_annotations,
            project_id=project_id,
        )
    finally:
        await client.close()
    if o == "json":
        _print_json(result)
    else:
        print("User profile updated")
