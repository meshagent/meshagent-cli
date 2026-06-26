import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel
from rich import print

from meshagent.api import RoomException
from meshagent.api.client import (
    ServiceAccount,
    ServiceAccountsPage,
    SecretProxyAccessGrantsPage,
    Secret,
    SecretVersion,
    SecretsPage,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(help="Manage user and service account secrets")
SubjectOption = Annotated[
    str,
    typer.Option(
        "--subject",
        help='secret owner: "me" or a service account email, id, key, or name',
    ),
]
ServiceAccountSubjectOption = Annotated[
    str,
    typer.Option(
        "--subject",
        help="service account email, id, key, or name",
    ),
]


def _model_or_mapping_to_dict(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return dict(value)


def _secrets_from_response(
    response: SecretsPage | Mapping[str, Any] | Sequence[Any],
) -> list[Secret | Mapping[str, Any]]:
    if isinstance(response, SecretsPage):
        return list(response.secrets)
    if isinstance(response, Mapping):
        secrets = response.get("secrets")
        if not isinstance(secrets, Sequence):
            raise RoomException("Invalid secrets payload")
        return [item for item in secrets if isinstance(item, Mapping)]
    return [item for item in response if isinstance(item, (BaseModel, Mapping))]


def _service_accounts_from_response(
    response: ServiceAccountsPage | Mapping[str, Any],
) -> list[ServiceAccount | Mapping[str, Any]]:
    if isinstance(response, ServiceAccountsPage):
        return list(response.service_accounts)
    service_accounts = response.get("service_accounts")
    if not isinstance(service_accounts, Sequence):
        raise RoomException("Invalid service accounts payload")
    return [item for item in service_accounts if isinstance(item, (BaseModel, Mapping))]


def _secret_rows(
    secrets: Sequence[Secret | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [_model_or_mapping_to_dict(secret) for secret in secrets]


def _secret_id(secret: Secret | Mapping[str, Any]) -> str:
    row = _model_or_mapping_to_dict(secret)
    secret_id = row.get("id")
    if not isinstance(secret_id, str) or secret_id.strip() == "":
        raise RoomException("Secret response did not include an id")
    return secret_id


def _is_me_subject(subject: str) -> bool:
    return subject.strip().lower() == "me"


def _service_account_lookup_values(subject: str) -> set[str]:
    normalized = subject.strip()
    values = {normalized}
    if "@" in normalized:
        values.add(normalized.split("@", 1)[0])
    return {value for value in values if value}


async def _resolve_service_account_subject(
    *,
    client: Any,
    project_id: str,
    subject: str,
) -> str:
    lookup_values = _service_account_lookup_values(subject)
    filters = [subject]
    local_part = subject.split("@", 1)[0] if "@" in subject else subject
    if local_part not in filters:
        filters.append(local_part)

    for filter_value in filters:
        response = await client.list_service_accounts(
            project_id,
            page_size=100,
            filter=filter_value,
        )
        for item in _service_accounts_from_response(response):
            row = _model_or_mapping_to_dict(item)
            item_id = row.get("id")
            candidates = {
                row.get("id"),
                row.get("key"),
                row.get("name"),
                row.get("display_name"),
            }
            if any(candidate in lookup_values for candidate in candidates):
                if isinstance(item_id, str) and item_id.strip() != "":
                    return item_id
    raise RoomException(f"Service account not found for subject: {subject}")


async def _resolve_service_account_subject_for_command(
    *,
    client: Any,
    project_id: str,
    subject: str,
) -> str:
    if _is_me_subject(subject):
        raise RoomException("This command requires a service account subject")
    return await _resolve_service_account_subject(
        client=client,
        project_id=project_id,
        subject=subject,
    )


def _secret_table_rows(
    secrets: Sequence[Secret | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for secret in secrets:
        row = _model_or_mapping_to_dict(secret)
        rows.append(
            {
                "name": row.get("name"),
                "id": row.get("id"),
                "type": row.get("type"),
                "http_only": row.get("http_only"),
                "current_version_id": row.get("current_version_id"),
            }
        )
    return rows


def _version_rows(
    versions: Sequence[SecretVersion | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [_model_or_mapping_to_dict(version) for version in versions]


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


def _read_secret_value(*, value: str | None, value_file: Path | None) -> bytes | None:
    if value is not None and value_file is not None:
        raise typer.BadParameter("use only one of --value or --value-file")
    if value is not None:
        return value.encode("utf-8")
    if value_file is not None:
        return value_file.read_bytes()
    return None


def _write_secret_value(value: bytes, *, o: OutputFormatOption) -> None:
    if o == "json":
        import base64

        print(json.dumps({"value_base64": base64.b64encode(value).decode()}, indent=2))
        return
    sys.stdout.buffer.write(value)
    if not value.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")


def _print_secrets(
    *,
    secrets: Sequence[Secret | Mapping[str, Any]],
    o: OutputFormatOption,
) -> None:
    if o == "json":
        print(json.dumps({"secrets": _secret_rows(secrets)}, indent=2))
        return
    rows = _secret_table_rows(secrets)
    if len(rows) == 0:
        print("No secrets")
        return
    print_json_table(rows, "name", "id", "type", "http_only", "current_version_id")


def _print_secret(
    *,
    secret: Secret | Mapping[str, Any],
    o: OutputFormatOption,
) -> None:
    row = _model_or_mapping_to_dict(secret)
    if o == "json":
        print(json.dumps(row, indent=2))
        return
    print_json_table([row], "name", "id", "type", "http_only", "current_version_id")


def _print_versions(
    *,
    versions: Sequence[SecretVersion | Mapping[str, Any]],
    o: OutputFormatOption,
) -> None:
    rows = _version_rows(versions)
    if o == "json":
        print(json.dumps({"versions": rows}, indent=2))
        return
    if len(rows) == 0:
        print("No versions")
        return
    print_json_table(rows, "version", "id", "secret_id", "created_at")


def _print_access_grants(
    *, page: SecretProxyAccessGrantsPage | Mapping[str, Any], o: OutputFormatOption
) -> None:
    if isinstance(page, SecretProxyAccessGrantsPage):
        grants = page.access_grants
    elif isinstance(page, Mapping):
        grants = page.get("access_grants")
        if not isinstance(grants, Sequence):
            raise RoomException("Invalid access grants payload")
    else:
        raise RoomException("Invalid access grants payload")
    rows = [_model_or_mapping_to_dict(grant) for grant in grants]
    if o == "json":
        print(json.dumps({"access_grants": rows}, indent=2))
        return
    table_rows: list[dict[str, Any]] = []
    for row in rows:
        subject = row.get("subject")
        if isinstance(subject, Mapping):
            table_rows.append(
                {
                    "subject_type": subject.get("type"),
                    "subject_id": subject.get("id"),
                    "subject_name": subject.get("name"),
                    "roles": ", ".join(row.get("roles") or []),
                }
            )
    if len(table_rows) == 0:
        print("No proxy access grants")
        return
    print_json_table(table_rows, "subject_type", "subject_id", "subject_name", "roles")


@app.async_command("list", help="List secrets for a subject.")
async def list_secrets(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    page_size: Annotated[int, typer.Option("--page-size", help="page size")] = 100,
    filter: Annotated[str | None, typer.Option("--filter", help="text filter")] = None,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            response = await client.list_user_secrets(
                page_size=page_size,
                filter=filter,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            response = await client.list_service_account_secrets(
                project_id=project_id,
                service_account_id=service_account_id,
                page_size=page_size,
                filter=filter,
            )
    finally:
        await client.close()
    _print_secrets(secrets=_secrets_from_response(response), o=o)


@app.async_command("search", help="Search secrets for a subject.")
async def search_secrets(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    filter: Annotated[str | None, typer.Option("--filter", help="text filter")] = None,
    name: Annotated[str | None, typer.Option("--name", help="secret name")] = None,
    type: Annotated[str | None, typer.Option("--type", help="secret type")] = None,
    http_only: Annotated[
        bool | None, typer.Option("--http-only/--not-http-only", help="http-only flag")
    ] = None,
    metadata: Annotated[
        str | None, typer.Option("--metadata", help="metadata JSON object")
    ] = None,
    annotations: Annotated[
        str | None, typer.Option("--annotations", help="annotations JSON object")
    ] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="standard provider annotation")
    ] = None,
    service: Annotated[
        str | None, typer.Option("--service", help="standard service annotation")
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", help="standard account annotation")
    ] = None,
    username: Annotated[
        str | None, typer.Option("--username", help="standard username annotation")
    ] = None,
    email: Annotated[
        str | None, typer.Option("--email", help="standard email annotation")
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="standard url annotation")
    ] = None,
    oauth_provider: Annotated[
        str | None,
        typer.Option("--oauth-provider", help="standard OAuth provider annotation"),
    ] = None,
    oauth_scopes: Annotated[
        str | None,
        typer.Option("--oauth-scopes", help="standard OAuth scopes annotation"),
    ] = None,
    page_size: Annotated[int, typer.Option("--page-size", help="page size")] = 100,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    metadata_obj = _parse_json_object("metadata", metadata)
    annotations_obj = _parse_json_object("annotations", annotations)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            response = await client.search_user_secrets(
                filter=filter,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
                provider=provider,
                service=service,
                account=account,
                username=username,
                email=email,
                url=url,
                oauth_provider=oauth_provider,
                oauth_scopes=oauth_scopes,
                page_size=page_size,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            response = await client.search_service_account_secrets(
                project_id=project_id,
                service_account_id=service_account_id,
                filter=filter,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
                provider=provider,
                service=service,
                account=account,
                username=username,
                email=email,
                url=url,
                oauth_provider=oauth_provider,
                oauth_scopes=oauth_scopes,
                page_size=page_size,
            )
    finally:
        await client.close()
    _print_secrets(secrets=_secrets_from_response(response), o=o)


@app.async_command("get", help="Get a secret for a subject.")
async def get_secret(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    include_value: Annotated[
        bool,
        typer.Option("--include-value", help="include the current secret value"),
    ] = False,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            secret = await client.get_user_secret(
                secret_id=secret_id,
                include_value=include_value,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            secret = await client.get_service_account_secret(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
                include_value=include_value,
            )
    finally:
        await client.close()
    _print_secret(secret=secret, o=o)


@app.async_command("create", help="Create a secret for a subject.")
async def create_secret(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    name: str,
    type: Annotated[str, typer.Option("--type", help="secret type")] = "opaque",
    http_only: Annotated[
        bool, typer.Option("--http-only", help="make the secret proxy-only")
    ] = False,
    metadata: Annotated[
        str | None, typer.Option("--metadata", help="metadata JSON object")
    ] = None,
    annotations: Annotated[
        str | None, typer.Option("--annotations", help="annotations JSON object")
    ] = None,
    value: Annotated[
        str | None, typer.Option("--value", help="initial secret value")
    ] = None,
    value_file: Annotated[
        Path | None, typer.Option("--value-file", help="file containing secret value")
    ] = None,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    initial_value = _read_secret_value(value=value, value_file=value_file)
    client = await get_client()
    try:
        metadata_obj = _parse_json_object("metadata", metadata)
        annotations_obj = _parse_json_object("annotations", annotations)
        if _is_me_subject(subject):
            secret = await client.create_user_secret(
                project_id=project_id,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
            )
            if initial_value is not None:
                secret_id = _secret_id(secret)
                await client.create_user_secret_version(
                    secret_id=secret_id,
                    value=initial_value,
                )
                secret = await client.get_user_secret(secret_id=secret_id)
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            secret = await client.create_service_account_secret(
                project_id=project_id,
                service_account_id=service_account_id,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
            )
            if initial_value is not None:
                secret_id = _secret_id(secret)
                await client.create_service_account_secret_version(
                    project_id=project_id,
                    service_account_id=service_account_id,
                    secret_id=secret_id,
                    value=initial_value,
                )
                secret = await client.get_service_account_secret(
                    project_id=project_id,
                    service_account_id=service_account_id,
                    secret_id=secret_id,
                )
    finally:
        await client.close()
    _print_secret(secret=secret, o=o)


@app.async_command("update", help="Update a secret for a subject.")
async def update_secret(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    name: Annotated[str | None, typer.Option("--name", help="new name")] = None,
    type: Annotated[str | None, typer.Option("--type", help="new type")] = None,
    http_only: Annotated[
        bool | None, typer.Option("--http-only/--not-http-only", help="http-only flag")
    ] = None,
    metadata: Annotated[
        str | None, typer.Option("--metadata", help="metadata JSON object")
    ] = None,
    annotations: Annotated[
        str | None, typer.Option("--annotations", help="annotations JSON object")
    ] = None,
):
    project_id = await resolve_project_id(project_id=project_id)
    metadata_obj = _parse_json_object("metadata", metadata)
    annotations_obj = _parse_json_object("annotations", annotations)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            await client.update_user_secret(
                secret_id=secret_id,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            await client.update_service_account_secret(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
                name=name,
                type=type,
                http_only=http_only,
                metadata=metadata_obj,
                annotations=annotations_obj,
            )
    finally:
        await client.close()


@app.async_command("delete", help="Delete a secret for a subject.")
async def delete_secret(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            await client.delete_user_secret(secret_id=secret_id)
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            await client.delete_service_account_secret(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
            )
    finally:
        await client.close()


@app.async_command("versions", help="List versions for a secret.")
async def list_secret_versions(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            versions = await client.list_user_secret_versions(secret_id=secret_id)
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            versions = await client.list_service_account_secret_versions(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
            )
    finally:
        await client.close()
    _print_versions(versions=versions, o=o)


@app.async_command("add-version", help="Add a new version to a secret.")
async def add_secret_version(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    value: Annotated[
        str | None, typer.Option("--value", help="new secret value")
    ] = None,
    value_file: Annotated[
        Path | None, typer.Option("--value-file", help="file containing secret value")
    ] = None,
    set_current: Annotated[
        bool,
        typer.Option("--set-current/--no-set-current", help="set as current version"),
    ] = True,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    raw_value = _read_secret_value(value=value, value_file=value_file)
    if raw_value is None:
        raise typer.BadParameter("one of --value or --value-file is required")
    client = await get_client()
    try:
        if _is_me_subject(subject):
            version = await client.create_user_secret_version(
                secret_id=secret_id,
                value=raw_value,
                set_current=set_current,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            version = await client.create_service_account_secret_version(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
                value=raw_value,
                set_current=set_current,
            )
    finally:
        await client.close()
    _print_versions(versions=[version], o=o)


@app.async_command("access", help="Access the contents of a secret version.")
async def access_secret_version(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    version_id: Annotated[
        str | None,
        typer.Option(
            "--version",
            help="secret version id; defaults to the current version",
        ),
    ] = None,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            resolved_version_id = version_id
            if resolved_version_id is None:
                secret = await client.get_user_secret(secret_id=secret_id)
                resolved_version_id = _model_or_mapping_to_dict(secret).get(
                    "current_version_id"
                )
            if not isinstance(resolved_version_id, str) or resolved_version_id == "":
                raise RoomException("Secret has no current version")
            value = await client.access_user_secret_version(
                secret_id=secret_id,
                version_id=resolved_version_id,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            resolved_version_id = version_id
            if resolved_version_id is None:
                secret = await client.get_service_account_secret(
                    project_id=project_id,
                    service_account_id=service_account_id,
                    secret_id=secret_id,
                )
                resolved_version_id = _model_or_mapping_to_dict(secret).get(
                    "current_version_id"
                )
            if not isinstance(resolved_version_id, str) or resolved_version_id == "":
                raise RoomException("Secret has no current version")
            value = await client.access_service_account_secret_version(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
                version_id=resolved_version_id,
            )
    finally:
        await client.close()
    _write_secret_value(value, o=o)


@app.async_command("delete-version", help="Delete a secret version.")
async def delete_secret_version(
    *,
    project_id: ProjectIdOption,
    subject: SubjectOption = "me",
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    version_id: Annotated[str, typer.Argument(help="secret version id")],
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        if _is_me_subject(subject):
            await client.delete_user_secret_version(
                secret_id=secret_id,
                version_id=version_id,
            )
        else:
            service_account_id = await _resolve_service_account_subject(
                client=client,
                project_id=project_id,
                subject=subject,
            )
            await client.delete_service_account_secret_version(
                project_id=project_id,
                service_account_id=service_account_id,
                secret_id=secret_id,
                version_id=version_id,
            )
    finally:
        await client.close()


@app.async_command("grants", help="List proxy access grants for one of your secrets.")
async def list_user_secret_access(
    *,
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    o: OutputFormatOption = "table",
):
    client = await get_client()
    try:
        page = await client.list_user_secret_proxy_access(secret_id=secret_id)
    finally:
        await client.close()
    _print_access_grants(page=page, o=o)


@app.async_command(
    "grant-proxy",
    help="Grant a service account proxy access to one of your secrets.",
)
async def grant_user_secret_proxy_access(
    *,
    project_id: ProjectIdOption,
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    subject: ServiceAccountSubjectOption,
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account_id = await _resolve_service_account_subject_for_command(
            client=client,
            project_id=project_id,
            subject=subject,
        )
        await client.grant_user_secret_proxy_access(
            secret_id=secret_id,
            service_account_id=service_account_id,
        )
    finally:
        await client.close()


@app.async_command(
    "revoke-proxy",
    help="Revoke a service account proxy access grant from one of your secrets.",
)
async def revoke_user_secret_proxy_access(
    *,
    project_id: ProjectIdOption,
    secret_id: Annotated[str, typer.Argument(help="secret id")],
    subject: ServiceAccountSubjectOption,
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account_id = await _resolve_service_account_subject_for_command(
            client=client,
            project_id=project_id,
            subject=subject,
        )
        await client.revoke_user_secret_proxy_access(
            secret_id=secret_id,
            service_account_id=service_account_id,
        )
    finally:
        await client.close()


@app.async_command("pull-secrets", help="List pull secrets for a service account.")
async def list_service_account_pull_secrets(
    *,
    project_id: ProjectIdOption,
    subject: ServiceAccountSubjectOption,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account_id = await _resolve_service_account_subject_for_command(
            client=client,
            project_id=project_id,
            subject=subject,
        )
        secrets = await client.list_service_account_pull_secrets(
            project_id=project_id,
            service_account_id=service_account_id,
        )
    finally:
        await client.close()
    _print_secrets(secrets=_secrets_from_response(secrets), o=o)


@app.async_command("add-pull-secret", help="Add a pull secret to a service account.")
async def add_service_account_pull_secret(
    *,
    project_id: ProjectIdOption,
    subject: ServiceAccountSubjectOption,
    secret_id: Annotated[str, typer.Argument(help="secret id")],
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account_id = await _resolve_service_account_subject_for_command(
            client=client,
            project_id=project_id,
            subject=subject,
        )
        await client.add_service_account_pull_secret(
            project_id=project_id,
            service_account_id=service_account_id,
            secret_id=secret_id,
        )
    finally:
        await client.close()


@app.async_command(
    "remove-pull-secret", help="Remove a pull secret from a service account."
)
async def remove_service_account_pull_secret(
    *,
    project_id: ProjectIdOption,
    subject: ServiceAccountSubjectOption,
    secret_id: Annotated[str, typer.Argument(help="secret id")],
):
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        service_account_id = await _resolve_service_account_subject_for_command(
            client=client,
            project_id=project_id,
            subject=subject,
        )
        await client.remove_service_account_pull_secret(
            project_id=project_id,
            service_account_id=service_account_id,
            secret_id=secret_id,
        )
    finally:
        await client.close()
