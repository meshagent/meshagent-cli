import json
from typing import Annotated, cast

import typer
from rich import print

from meshagent.api.client import (
    AccessResourceType,
    AccessRole,
    AccessSubject,
    AccessSubjectType,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id

app = async_typer.AsyncTyper(help="Manage IAM policies for project resources")


def _parse_subject_type(value: str) -> AccessSubjectType:
    allowed = {"user", "group", "agent", "service_account", "userset"}
    if value not in allowed:
        raise typer.BadParameter(f"expected one of: {', '.join(sorted(allowed))}")
    return cast(AccessSubjectType, value)


def _parse_resource_type(value: str) -> AccessResourceType:
    allowed = {"project", "room", "agent", "group", "repository", "feed"}
    if value not in allowed:
        raise typer.BadParameter(f"expected one of: {', '.join(sorted(allowed))}")
    return cast(AccessResourceType, value)


def _parse_roles(values: list[str]) -> list[AccessRole]:
    allowed = {
        "member",
        "admin",
        "developer",
        "room_creator",
        "room_inventory",
        "room_manager",
        "session_inventory",
        "agent_creator",
        "agent_inventory",
        "agent_manager",
        "repository_creator",
        "repository_inventory",
        "repository_manager",
        "feed_creator",
        "feed_inventory",
        "feed_manager",
        "oauth_client_creator",
        "oauth_client_inventory",
        "oauth_client_manager",
        "api_key_creator",
        "api_key_inventory",
        "api_key_manager",
        "secret_creator",
        "secret_inventory",
        "secret_manager",
        "external_oauth_client_creator",
        "external_oauth_client_inventory",
        "external_oauth_client_manager",
        "service_creator",
        "service_inventory",
        "service_manager",
        "service_account_creator",
        "service_account_inventory",
        "service_account_manager",
        "participant_token_creator",
        "mailbox_creator",
        "mailbox_inventory",
        "mailbox_manager",
        "route_creator",
        "route_inventory",
        "route_manager",
        "scheduled_task_creator",
        "scheduled_task_inventory",
        "scheduled_task_manager",
        "feed_subscription_creator",
        "feed_subscription_inventory",
        "feed_subscription_manager",
        "llm_logger_creator",
        "llm_logger_inventory",
        "llm_logger_manager",
        "llm_proxy_user",
        "usage_reporter",
        "billing_manager",
        "group_manager",
        "viewer",
        "operator",
        "reader",
        "subscriber",
        "publisher",
        "manager",
        "list",
    }
    roles: list[AccessRole] = []
    for value in values:
        for role in value.split(","):
            normalized = role.strip()
            if normalized == "":
                continue
            if normalized not in allowed:
                raise typer.BadParameter(
                    f"unknown role {normalized}; expected one of: "
                    f"{', '.join(sorted(allowed))}"
                )
            roles.append(cast(AccessRole, normalized))
    return roles


def _grant_rows(grants) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for grant in grants:
        rows.append(
            {
                "resource_type": grant.resource.type,
                "resource_id": grant.resource.id,
                "resource_name": grant.resource.name,
                "subject_type": grant.subject.type,
                "subject_id": grant.subject.id,
                "subject_name": grant.subject.name,
                "subject_email": grant.subject.email,
                "roles": ", ".join(grant.direct_roles),
            }
        )
    return rows


@app.async_command("policy", help="List a resource IAM policy.")
async def policy(
    *,
    project_id: ProjectIdOption,
    resource_type: Annotated[
        str,
        typer.Option("--resource-type", help="resource type"),
    ],
    resource_id: Annotated[
        str,
        typer.Option("--resource-id", help="resource id"),
    ],
    page_size: Annotated[
        int,
        typer.Option("--page-size", help="OpenFGA read page size"),
    ] = 50,
    o: OutputFormatOption = "table",
):
    project_id = await resolve_project_id(project_id=project_id)
    parsed_resource_type = _parse_resource_type(resource_type)
    client = await get_client()
    try:
        grants = await client.get_resource_policy(
            project_id=project_id,
            resource_type=parsed_resource_type,
            resource_id=resource_id,
            page_size=page_size,
        )
    finally:
        await client.close()

    rows = _grant_rows(grants)
    if o == "json":
        print(json.dumps({"access_grants": rows}, indent=2))
        return
    if len(rows) == 0:
        print("No direct grants")
        return
    print_json_table(
        rows,
        "resource_type",
        "resource_id",
        "resource_name",
        "subject_type",
        "subject_id",
        "subject_name",
        "subject_email",
        "roles",
    )


@app.async_command("grant", help="Grant roles on a resource.")
async def grant(
    *,
    project_id: ProjectIdOption,
    resource_type: Annotated[
        str,
        typer.Option("--resource-type", help="resource type"),
    ],
    resource_id: Annotated[
        str,
        typer.Option("--resource-id", help="resource id"),
    ],
    subject_type: Annotated[
        str,
        typer.Option("--subject-type", help="subject type"),
    ],
    subject_id: Annotated[
        str,
        typer.Option("--subject-id", help="subject id"),
    ],
    role: Annotated[
        list[str],
        typer.Option("--role", help="role to grant; repeat or comma-separate"),
    ],
    invite_redirect_url: Annotated[
        str | None,
        typer.Option("--invite-redirect-url", help="invite redirect URL for users"),
    ] = None,
):
    project_id = await resolve_project_id(project_id=project_id)
    parsed_resource_type = _parse_resource_type(resource_type)
    parsed_subject_type = _parse_subject_type(subject_type)
    roles = _parse_roles(role)
    if len(roles) == 0:
        raise typer.BadParameter("at least one --role is required")

    client = await get_client()
    try:
        await client.grant_resource_policy(
            project_id=project_id,
            resource_type=parsed_resource_type,
            resource_id=resource_id,
            subject=AccessSubject(type=parsed_subject_type, id=subject_id),
            roles=roles,
            invite_redirect_url=invite_redirect_url,
        )
    finally:
        await client.close()


@app.async_command("revoke", help="Revoke all direct roles for a subject.")
async def revoke(
    *,
    project_id: ProjectIdOption,
    resource_type: Annotated[
        str,
        typer.Option("--resource-type", help="resource type"),
    ],
    resource_id: Annotated[
        str,
        typer.Option("--resource-id", help="resource id"),
    ],
    subject_type: Annotated[
        str,
        typer.Option("--subject-type", help="subject type"),
    ],
    subject_id: Annotated[
        str,
        typer.Option("--subject-id", help="subject id"),
    ],
):
    project_id = await resolve_project_id(project_id=project_id)
    parsed_resource_type = _parse_resource_type(resource_type)
    parsed_subject_type = _parse_subject_type(subject_type)

    client = await get_client()
    try:
        await client.revoke_resource_policy(
            project_id=project_id,
            resource_type=parsed_resource_type,
            resource_id=resource_id,
            subject=AccessSubject(type=parsed_subject_type, id=subject_id),
        )
    finally:
        await client.close()
