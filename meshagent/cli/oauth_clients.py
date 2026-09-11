from enum import Enum
from typing import Annotated

import typer
from meshagent.api.oauth_branding import OAuthClientBranding
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import get_client, resolve_project_id
from pydantic import ValidationError
from rich import print_json

app = async_typer.AsyncTyper(help="Manage OAuth clients and login branding")


class Theme(str, Enum):
    light = "light"
    dark = "dark"
    auto = "auto"


def branding_metadata(
    existing: dict[str, str],
    *,
    name: str | None,
    logo_url: str | None,
    theme: Theme | None,
    clear_logo: bool = False,
) -> dict[str, str]:
    if clear_logo and logo_url is not None:
        raise typer.BadParameter("Use either --logo-url or --clear-logo")
    metadata = dict(existing)
    if name is not None:
        metadata["name"] = name
    if clear_logo:
        metadata.pop("logo_url", None)
    elif logo_url is not None:
        metadata["logo_url"] = logo_url
    if theme is not None:
        metadata["theme"] = theme.value
    try:
        OAuthClientBranding.model_validate(metadata)
    except ValidationError as exc:
        raise typer.BadParameter("Logo URL must be an absolute HTTP(S) URL") from exc
    return metadata


@app.command("create")
async def create_client(
    name: Annotated[str, typer.Option(help="Application name shown at login")],
    redirect_uri: Annotated[
        list[str], typer.Option(help="Allowed callback URL; repeat for multiple URLs")
    ],
    scope: Annotated[str, typer.Option(help="OAuth scopes")],
    project_id: ProjectIdOption,
    logo_url: Annotated[
        str | None, typer.Option(help="Optional HTTP(S) login logo URL")
    ] = None,
    theme: Annotated[Theme, typer.Option(help="Login appearance")] = Theme.auto,
):
    """Create an authorization-code client. The output includes its new secret."""
    metadata = branding_metadata({}, name=name, logo_url=logo_url, theme=theme)
    project_id = await resolve_project_id(project_id)
    async with await get_client() as client:
        result = await client.create_oauth_client(
            project_id=project_id,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            redirect_uris=redirect_uri,
            scope=scope,
            metadata=metadata,
        )
        print_json(result.model_dump_json())


@app.command("update")
async def update_client(
    client_id: Annotated[str, typer.Argument(help="OAuth client ID")],
    project_id: ProjectIdOption,
    name: Annotated[
        str | None, typer.Option(help="Application name shown at login")
    ] = None,
    logo_url: Annotated[str | None, typer.Option(help="HTTP(S) login logo URL")] = None,
    theme: Annotated[
        Theme | None, typer.Option(help="Login appearance: light, dark, or auto")
    ] = None,
    clear_logo: Annotated[
        bool, typer.Option(help="Restore the default MeshAgent logo")
    ] = False,
):
    """Update login branding, preserving other client settings and metadata."""
    project_id = await resolve_project_id(project_id)
    async with await get_client() as client:
        existing = await client.get_oauth_client(
            project_id=project_id, client_id=client_id
        )
        metadata = branding_metadata(
            existing.metadata,
            name=name,
            logo_url=logo_url,
            theme=theme,
            clear_logo=clear_logo,
        )
        result = await client.update_oauth_client(
            project_id=project_id, client_id=client_id, metadata=metadata
        )
        print_json(data=result)


@app.command("get")
async def get_client_config(
    client_id: Annotated[str, typer.Argument(help="OAuth client ID")],
    project_id: ProjectIdOption,
):
    """Show an OAuth client's configuration and branding."""
    project_id = await resolve_project_id(project_id)
    async with await get_client() as client:
        result = await client.get_oauth_client(
            project_id=project_id, client_id=client_id
        )
        print_json(result.model_dump_json())
