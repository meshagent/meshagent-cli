import json
import math
from enum import Enum
from typing import Annotated

import typer
from meshagent.api import RoomException
from meshagent.api.client import ProjectMembersPage, UpdateUserProfileRequest, User
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id
from pydantic import JsonValue, TypeAdapter
from rich import print
from rich.console import Console
from rich.table import Table
from rich.text import Text

app = async_typer.AsyncTyper(help="List project users and manage user profiles")


class ProfileView(str, Enum):
    project = "project"
    user = "user"
    merged = "merged"


class ProfileField(str, Enum):
    first_name = "first_name"
    last_name = "last_name"
    metadata = "metadata"
    annotations = "annotations"


ProfileViewOption = Annotated[
    ProfileView, typer.Option("--view", help="Profile attributes to return")
]
GlobalOption = Annotated[
    bool,
    typer.Option(
        "--global", help="Use your global account profile, ignoring the active project"
    ),
]

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
    view: ProfileViewOption = ProfileView.merged,
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
                view=view.value,
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
        _print_json(
            result.model_dump(mode="json", exclude_unset=view == ProfileView.project)
        )
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
    "get", help="Get a project user profile or your global account profile."
)
async def get(
    *,
    user_id: UserIdArgument = "me",
    project_id: ProjectIdOption,
    view: ProfileViewOption = ProfileView.merged,
    global_profile: GlobalOption = False,
    o: OutputFormatOption = "table",
) -> None:
    _validate_output(o)
    if global_profile:
        project_id = None
    if view == ProfileView.project and project_id is None:
        raise typer.BadParameter("--view project requires --project-id")
    client = await get_client()
    try:
        row = await client.get_user_profile(
            user_id, project_id=project_id, view=view.value
        )
    finally:
        await client.close()
    if o == "json":
        _print_json(row)
        return
    _print_profile(row)


def _print_profile(row: dict) -> None:
    values = {
        name: row.get(name) for name in ("id", "email", "first_name", "last_name")
    }
    values["metadata"] = json.dumps(row.get("metadata", {}))
    values["annotations"] = json.dumps(row.get("annotations", {}))
    _print_table(
        [values], "id", "email", "first_name", "last_name", "metadata", "annotations"
    )


@app.async_command(
    "update", help="Update supplied fields; supplied JSON maps replace existing maps."
)
async def update(
    *,
    project_id: ProjectIdOption,
    user_id: UserIdArgument = "me",
    global_profile: GlobalOption = False,
    inherit: Annotated[
        list[ProfileField] | None,
        typer.Option(
            "--inherit", help="Remove a project override; repeat for multiple fields"
        ),
    ] = None,
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
    if (
        all(value is None for value in (first_name, last_name, metadata, annotations))
        and not inherit
    ):
        raise typer.BadParameter("supply at least one profile field to update")
    parsed_metadata = _parse_object(metadata, "metadata")
    parsed_annotations = _parse_annotations(annotations)
    if global_profile:
        project_id = None
    if parsed_annotations is not None and project_id is None:
        raise typer.BadParameter(
            "Global annotations require the sysadmin user update command"
        )
    if inherit and project_id is None:
        raise typer.BadParameter("--inherit requires --project-id")
    supplied = {
        "first_name": first_name,
        "last_name": last_name,
        "metadata": metadata,
        "annotations": annotations,
    }
    if inherit and any(supplied[field.value] is not None for field in inherit):
        raise typer.BadParameter(
            "A field cannot be supplied and inherited in the same update"
        )
    client = await get_client()
    try:
        result = await client.update_user_profile(
            user_id,
            first_name=first_name,
            last_name=last_name,
            metadata=parsed_metadata,
            annotations=parsed_annotations,
            project_id=project_id,
            **({"inherit": [field.value for field in inherit]} if inherit else {}),
        )
    finally:
        await client.close()
    if o == "json":
        _print_json(result)
    else:
        print("User profile updated")


sysadmin_app = async_typer.AsyncTyper(
    help="Search and edit global user profiles; requires sysadmin access"
)


@sysadmin_app.async_command(
    "list", help="Search global users by email, name, or user id."
)
async def list_sysadmin_users(
    *,
    filter: Annotated[str | None, typer.Option("--filter")] = None,
    count: Annotated[int, typer.Option("--count", min=1)] = 100,
    continuation_token: Annotated[
        str | None, typer.Option("--continuation-token")
    ] = None,
    o: OutputFormatOption = "table",
) -> None:
    _validate_output(o)
    client = await get_client()
    records: list[User] = []
    try:
        while len(records) < count:
            page = await client.search_sysadmin_users(
                filter=filter,
                page_size=min(count - len(records), 100),
                continuation_token=continuation_token,
            )
            records.extend(page.users)
            if (
                page.continuation_token == continuation_token
                and page.continuation_token is not None
            ):
                raise RoomException("User listing continuation token did not advance")
            continuation_token = page.continuation_token
            if continuation_token is None:
                break
    finally:
        await client.close()
    rows = [record.model_dump(mode="json") for record in records]
    if o == "json":
        _print_json({"users": rows, "continuation_token": continuation_token})
    else:
        _print_table(rows, "id", "email", "first_name", "last_name")
        if continuation_token is not None:
            typer.echo(f"Continuation token: {continuation_token}")


@sysadmin_app.async_command("get", help="Get a global user profile.")
async def get_sysadmin_user(
    *, user_id: UserIdArgument = "me", o: OutputFormatOption = "table"
) -> None:
    _validate_output(o)
    client = await get_client()
    try:
        user = await client.get_sysadmin_user_profile(user_id)
    finally:
        await client.close()
    row = user.model_dump(mode="json")
    if o == "json":
        _print_json(row)
    else:
        _print_profile(row)


@sysadmin_app.async_command(
    "update", help="Update global names, metadata, or annotations."
)
async def update_sysadmin_user(
    *,
    user_id: UserIdArgument = "me",
    first_name: Annotated[str | None, typer.Option("--first-name")] = None,
    last_name: Annotated[str | None, typer.Option("--last-name")] = None,
    metadata: Annotated[
        str | None, typer.Option("--metadata", help="Replacement JSON object")
    ] = None,
    annotations: Annotated[
        str | None, typer.Option("--annotations", help="Replacement string map")
    ] = None,
    o: OutputFormatOption = "table",
) -> None:
    _validate_output(o)
    if all(value is None for value in (first_name, last_name, metadata, annotations)):
        raise typer.BadParameter("supply at least one profile field to update")
    fields = {
        key: value
        for key, value in {
            "first_name": first_name,
            "last_name": last_name,
            "metadata": _parse_object(metadata, "metadata"),
            "annotations": _parse_annotations(annotations),
        }.items()
        if value is not None
    }
    update = UpdateUserProfileRequest.model_validate(fields)
    client = await get_client()
    try:
        result = await client.update_sysadmin_user_profile(user_id, update=update)
    finally:
        await client.close()
    if o == "json":
        _print_json(result)
    else:
        print("Global user profile updated")
