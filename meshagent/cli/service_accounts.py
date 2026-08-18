import json
import shlex
from binascii import Error as BinasciiError
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import typer
from pydantic import BaseModel
from rich import print

from meshagent.api import RoomException
from meshagent.api.client import ApiKeysPage, ServiceAccount, ServiceAccountsPage
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

app = async_typer.AsyncTyper(help="Manage service accounts for your project")
api_key_app = async_typer.AsyncTyper(help="Manage API keys for a service account")
app.add_typer(api_key_app, name="api-key", help="Manage API keys")


def _model_or_mapping_to_dict(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return dict(value)


def _service_accounts_from_response(
    response: ServiceAccountsPage | Mapping[str, Any],
) -> list[ServiceAccount | Mapping[str, Any]]:
    if isinstance(response, ServiceAccountsPage):
        return list(response.service_accounts)
    service_accounts = response.get("service_accounts")
    if not isinstance(service_accounts, Sequence):
        raise RoomException("Invalid service accounts payload")
    return [item for item in service_accounts if isinstance(item, Mapping)]


def _api_keys_from_response(response: ApiKeysPage | Mapping[str, Any]) -> list[Any]:
    if isinstance(response, ApiKeysPage):
        return list(response.keys)
    keys = response.get("keys")
    if not isinstance(keys, Sequence):
        raise RoomException("Invalid API keys payload")
    return list(keys)


def _parse_json_object(label: str, value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RoomException(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RoomException(f"Invalid {label} JSON: expected an object")
    return parsed


def _parse_annotations(value: str | None) -> dict[str, str] | None:
    parsed = _parse_json_object("annotations", value)
    if parsed is None:
        return None
    output: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RoomException(
                "Invalid annotations JSON: expected string keys and values"
            )
        output[key] = item
    return output


async def _resolve_service_account_id(*, project_id: str, service_account: str) -> str:
    client = await get_client()
    try:
        response = await client.list_service_accounts(
            project_id=project_id,
            page_size=100,
            filter=service_account,
        )
    finally:
        await client.close()

    for item in _service_accounts_from_response(response):
        row = _model_or_mapping_to_dict(item)
        item_id = row.get("id")
        item_name = row.get("name")
        if item_id == service_account or item_name == service_account:
            if isinstance(item_id, str) and item_id.strip() != "":
                return item_id

    print(f"[red]Service account not found: {service_account}[/red]")
    raise typer.Exit(code=1)


async def _require_active_api_key(*, project_id: str | None) -> str:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    key = await get_active_api_key(project_id=resolved_project_id)
    if key is None:
        print(
            f"[red]No activated API key found for project {resolved_project_id}. "
            "Use meshagent service-account api-key activate or "
            "meshagent service-account api-key create --activate to store one locally.[/red]"
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


def _service_account_table_rows(
    service_accounts: Sequence[ServiceAccount | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "display_name": row.get("display_name"),
            "description": row.get("description"),
        }
        for row in (_model_or_mapping_to_dict(item) for item in service_accounts)
    ]


@app.async_command("list", help="List service accounts for a project.")
async def list_service_accounts(
    *,
    project_id: ProjectIdOption,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        response = await client.list_service_accounts(project_id=project_id)
    finally:
        await client.close()

    service_accounts = _service_accounts_from_response(response)
    if len(service_accounts) == 0:
        print("There are not currently any service accounts in the project")
        return

    rows = _service_account_table_rows(service_accounts)
    if o == "json":
        print(json.dumps({"service_accounts": rows}, indent=2))
        return
    print_json_table(rows, "id", "name", "display_name", "description")


@app.async_command("get", help="Get a service account.")
async def get(
    *,
    project_id: ProjectIdOption,
    service_account: Annotated[
        str,
        typer.Argument(help="service account id or name"),
    ],
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        result = await client.get_service_account(project_id, service_account_id)
    finally:
        await client.close()

    row = _model_or_mapping_to_dict(result)
    if o == "json":
        print(json.dumps(row, indent=2))
        return
    print_json_table([row], "id", "name", "display_name", "description")


@app.async_command("create", help="Create a service account for a project.")
async def create(
    *,
    project_id: ProjectIdOption,
    name: str,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", help="display name for the service account"),
    ] = None,
    description: Annotated[
        str, typer.Option(..., help="description for the service account")
    ] = "",
    metadata: Annotated[
        str | None,
        typer.Option("--metadata", help="metadata JSON object"),
    ] = None,
    annotations: Annotated[
        str | None,
        typer.Option("--annotations", help="annotations JSON object"),
    ] = None,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account = await client.create_service_account(
            project_id=project_id,
            name=name,
            display_name=display_name,
            description=description,
            metadata=_parse_json_object("metadata", metadata),
            annotations=_parse_annotations(annotations),
        )
    finally:
        await client.close()

    row = _model_or_mapping_to_dict(service_account)
    if o == "json":
        print(json.dumps(row, indent=2))
        return
    print_json_table([row], "id", "name", "display_name", "description")


@app.async_command("update", help="Update a service account.")
async def update(
    *,
    project_id: ProjectIdOption,
    service_account: Annotated[
        str,
        typer.Argument(help="service account id or name"),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", help="new service account name"),
    ] = None,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", help="display name for the service account"),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="description for the service account"),
    ] = None,
    metadata: Annotated[
        str | None,
        typer.Option("--metadata", help="metadata JSON object"),
    ] = None,
    annotations: Annotated[
        str | None,
        typer.Option("--annotations", help="annotations JSON object"),
    ] = None,
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        current = await client.get_service_account(project_id, service_account_id)
        row = _model_or_mapping_to_dict(current)
        await client.update_service_account(
            project_id,
            service_account_id,
            name=name or str(row["name"]),
            display_name=display_name
            if display_name is not None
            else row.get("display_name"),
            description=description
            if description is not None
            else row.get("description", ""),
            metadata=_parse_json_object("metadata", metadata)
            if metadata is not None
            else row.get("metadata"),
            annotations=_parse_annotations(annotations)
            if annotations is not None
            else row.get("annotations"),
        )
    finally:
        await client.close()


@app.async_command("delete", help="Delete a service account.")
async def delete(
    *,
    project_id: ProjectIdOption,
    service_account: Annotated[
        str,
        typer.Argument(help="service account id or name"),
    ],
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        await client.delete_service_account(project_id, service_account_id)
    finally:
        await client.close()


@api_key_app.async_command("list", help="List API keys for a service account.")
async def list_api_keys(
    *,
    project_id: ProjectIdOption,
    service_account: Annotated[
        str,
        typer.Option(
            "--service-account",
            help="service account id or name that owns the API keys",
        ),
    ],
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        response = await client.list_api_keys(
            project_id=project_id,
            service_account_id=service_account_id,
        )
    finally:
        await client.close()

    keys = [_model_or_mapping_to_dict(key) for key in _api_keys_from_response(response)]
    if len(keys) == 0:
        print("There are not currently any API keys for the service account")
        return

    if o == "json":
        print(json.dumps({"api_keys": keys}, indent=2))
        return

    active_key_id = await _get_active_api_key_id(project_id=project_id)
    rows = [
        {
            "active": "*" if key.get("id") == active_key_id else "",
            **key,
        }
        for key in keys
    ]
    print_json_table(rows, "active", "id", "name", "description")


@api_key_app.async_command("create", help="Create an API key for a service account.")
async def create_api_key(
    *,
    project_id: ProjectIdOption,
    name: str,
    service_account: Annotated[
        str,
        typer.Option(
            "--service-account",
            help="service account id or name that will own the API key",
        ),
    ],
    description: Annotated[
        str, typer.Option(..., help="a description for the API key")
    ] = "",
    activate: Annotated[
        bool,
        typer.Option(
            ..., help="use this key by default for commands that accept an API key"
        ),
    ] = False,
    silent: Annotated[bool, typer.Option(..., help="do not print API key")] = False,
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        api_key = await client.create_api_key(
            project_id=project_id,
            name=name,
            description=description,
            service_account_id=service_account_id,
        )
    finally:
        await client.close()

    row = _model_or_mapping_to_dict(api_key)
    value = row.get("value")
    if not silent and isinstance(value, str):
        print(
            "[green]This is your token. Save it for later, you will not be able to get the value again:[/green]\n"
        )
        print(value)
    if activate:
        if not isinstance(value, str) or value.strip() == "":
            raise RoomException("API key response did not include a token value")
        await set_active_api_key(project_id=project_id, key=value)
        print("[green]your API key has been activated[/green]\n")


@api_key_app.async_command("delete", help="Delete an API key.")
async def delete_api_key(
    *,
    project_id: ProjectIdOption,
    service_account: Annotated[
        str,
        typer.Option(
            "--service-account",
            help="service account id or name that owns the API key",
        ),
    ],
    id: str,
):
    project_id = await resolve_project_id(project_id=project_id)
    service_account_id = await _resolve_service_account_id(
        project_id=project_id,
        service_account=service_account,
    )
    client = await get_client()
    try:
        await client.delete_api_key(
            project_id=project_id,
            service_account_id=service_account_id,
            id=id,
        )
    finally:
        await client.close()


@api_key_app.async_command("get", help="Get the activated API key for a project.")
async def get_api_key(*, project_id: ProjectIdOption):
    key = await _require_active_api_key(project_id=project_id)
    typer.echo(key)


@api_key_app.async_command(
    "env",
    help="Print the activated API key as a shell export snippet.",
)
async def api_key_env(*, project_id: ProjectIdOption):
    key = await _require_active_api_key(project_id=project_id)
    typer.echo(f"export MESHAGENT_API_KEY={shlex.quote(key)}")


@api_key_app.async_command(
    "activate",
    help="Set the default API key for a project in local CLI settings.",
)
async def activate_api_key(
    *,
    project_id: ProjectIdOption,
    key: str,
):
    project_id = await resolve_project_id(project_id=project_id)
    await set_active_api_key(project_id=project_id, key=key)
