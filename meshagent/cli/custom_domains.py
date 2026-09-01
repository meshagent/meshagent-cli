from __future__ import annotations

import json
from typing import Annotated, Literal

import typer
from aiohttp import ClientResponseError
from rich import print

from meshagent.api.client import (
    CustomDomain,
    CustomDomainDnsRecord,
    CustomDomainsPage,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import OutputFormatOption, ProjectIdOption
from meshagent.cli.helper import get_client, print_json_table, resolve_project_id


app = async_typer.AsyncTyper(help="Manage custom domains for project routes")


def _record(record: CustomDomainDnsRecord | None) -> str:
    if record is None:
        return ""
    return f"{record.name} {record.type} {record.data}"


def _custom_domain_row(custom_domain: CustomDomain) -> dict[str, object]:
    return {
        "domain": custom_domain.domain,
        "phase": custom_domain.phase,
        "available": custom_domain.available,
        "dns_authorization": _record(custom_domain.dns_authorization_record),
        "route_dns": ", ".join(
            _record(record) for record in custom_domain.routing_records
        ),
        "certificate": custom_domain.certificate_state or "",
    }


def _print_custom_domains(
    custom_domains: list[CustomDomain], *, output_format: str
) -> None:
    if output_format == "json":
        print(
            json.dumps(
                {
                    "custom_domains": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in custom_domains
                    ]
                },
                indent=2,
            )
        )
        return
    print_json_table(
        [_custom_domain_row(item) for item in custom_domains],
        "domain",
        "phase",
        "available",
        "dns_authorization",
        "route_dns",
        "certificate",
    )


@app.async_command("create")
async def create_custom_domain(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[
        str,
        typer.Argument(help="Exact domain or wildcard such as *.example.com"),
    ],
    o: OutputFormatOption = "table",
) -> None:
    """Register an immutable custom domain and begin certificate provisioning."""
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        custom_domain = await client.create_custom_domain(
            project_id=project_id,
            domain=domain,
        )
    finally:
        await client.close()
    _print_custom_domains([custom_domain], output_format=o)


@app.async_command("get")
async def get_custom_domain(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[str, typer.Argument(help="Exact registered domain")],
    o: OutputFormatOption = "table",
) -> None:
    """Show availability and the DNS records that must be published."""
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        try:
            custom_domain = await client.get_custom_domain(
                project_id=project_id,
                domain=domain,
            )
        except ClientResponseError as error:
            if error.status == 404:
                print(f"[red]Custom domain not found:[/] {domain}")
                raise typer.Exit(code=1) from error
            raise
    finally:
        await client.close()
    _print_custom_domains([custom_domain], output_format=o)


@app.async_command("list")
async def list_custom_domains(
    *,
    project_id: ProjectIdOption,
    filter: Annotated[
        str | None,
        typer.Option("--filter", help="Lowercase contains filter"),
    ] = None,
    count: Annotated[
        int,
        typer.Option("--count", help="Maximum number of domains to return"),
    ] = 100,
    view: Annotated[
        Literal["my", "all"],
        typer.Option("--view", help="List directly accessible domains or all domains"),
    ] = "my",
    o: OutputFormatOption = "table",
) -> None:
    """List registered custom domains and certificate availability."""
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    custom_domains: list[CustomDomain] = []
    continuation_token: str | None = None
    try:
        while len(custom_domains) < count:
            page: CustomDomainsPage = await client.list_custom_domains(
                project_id=project_id,
                count=min(count - len(custom_domains), 100),
                continuation_token=continuation_token,
                filter=filter,
                view=view,
            )
            custom_domains.extend(page.custom_domains)
            continuation_token = page.continuation_token
            if continuation_token is None:
                break
    finally:
        await client.close()
    _print_custom_domains(custom_domains[:count], output_format=o)


@app.async_command("delete")
async def delete_custom_domain(
    *,
    project_id: ProjectIdOption,
    domain: Annotated[str, typer.Argument(help="Exact registered domain")],
) -> None:
    """Delete a custom domain after all covered routes have been removed."""
    project_id = await resolve_project_id(project_id=project_id)
    client = await get_client()
    try:
        try:
            await client.delete_custom_domain(project_id=project_id, domain=domain)
        except ClientResponseError as error:
            if error.status == 404:
                print(f"[red]Custom domain not found:[/] {domain}")
                raise typer.Exit(code=1) from error
            if error.status == 409:
                print(f"[red]Custom domain is still assigned to a route:[/] {domain}")
                raise typer.Exit(code=1) from error
            raise
    finally:
        await client.close()
    print(f"[green]Custom domain deletion requested:[/] {domain}")
