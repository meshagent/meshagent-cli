from __future__ import annotations

from typing import Annotated

import typer

from meshagent.api import RoomException
from meshagent.api.client import MeshagentDeploymentConfig
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client
from meshagent.cli.local_settings import resolve_api_url

app = async_typer.AsyncTyper(help="Read MeshAgent deployment configuration")

SUPPORTED_CONFIG_PATHS = (
    "version",
    "openai.url",
    "anthropic.url",
    "grok.url",
    "domains.studio",
    "domains.accounts",
    "domains.powerboards",
    "domains.api",
    "domains.mail",
    "domains.pages",
    "domains.registry",
)


@app.callback()
def _config_callback() -> None:
    pass


def _llm_proxy_url(*, api_url: str, provider_path: str) -> str:
    return f"{api_url.rstrip('/')}/{provider_path}"


def _config_value(
    config: MeshagentDeploymentConfig,
    path: str,
    *,
    api_url: str,
) -> str | None:
    match path:
        case "version":
            return config.version
        case "openai.url":
            return _llm_proxy_url(api_url=api_url, provider_path="openai/v1")
        case "anthropic.url":
            return _llm_proxy_url(api_url=api_url, provider_path="anthropic")
        case "grok.url":
            return _llm_proxy_url(api_url=api_url, provider_path="grok/v1")
        case "domains.studio":
            return config.domains.studio
        case "domains.accounts":
            return config.domains.accounts
        case "domains.powerboards":
            return config.domains.powerboards
        case "domains.api":
            return config.domains.api
        case "domains.mail":
            return config.domains.mail
        case "domains.pages":
            return config.domains.pages
        case "domains.registry":
            return config.domains.registry
        case _:
            raise typer.BadParameter(
                "unsupported config path; choose one of: "
                f"{', '.join(SUPPORTED_CONFIG_PATHS)}"
            )


@app.async_command("get")
async def config_get(
    path: Annotated[
        str,
        typer.Argument(
            help=(
                "Config path to read. Supported paths: "
                f"{', '.join(SUPPORTED_CONFIG_PATHS)}"
            )
        ),
    ],
) -> None:
    """Print one deployment config value."""
    client = await get_client()
    try:
        config = await client.get_config()
    except RoomException as exc:
        typer.echo(f"Failed to read config: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        await client.close()

    value = _config_value(
        config,
        path.strip(),
        api_url=resolve_api_url(),
    )
    if value is None:
        typer.echo(f"Config value is not set: {path.strip()}", err=True)
        raise typer.Exit(1)
    typer.echo(value)
