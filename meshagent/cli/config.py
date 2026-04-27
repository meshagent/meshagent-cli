from __future__ import annotations

from typing import Annotated

import typer

from meshagent.api import RoomException
from meshagent.api.client import MeshagentDeploymentConfig
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client

app = async_typer.AsyncTyper(help="Read MeshAgent deployment configuration")


@app.callback()
def _config_callback() -> None:
    pass


def _config_value(config: MeshagentDeploymentConfig, path: str) -> str | None:
    match path:
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
            supported_paths = ", ".join(
                [
                    "domains.studio",
                    "domains.accounts",
                    "domains.powerboards",
                    "domains.api",
                    "domains.mail",
                    "domains.pages",
                    "domains.registry",
                ]
            )
            raise typer.BadParameter(
                f"unsupported config path; choose one of: {supported_paths}"
            )


@app.async_command("get")
async def config_get(
    path: Annotated[
        str,
        typer.Argument(help="Config path to read, for example domains.pages"),
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

    value = _config_value(config, path.strip())
    if value is None:
        typer.echo(f"Config value is not set: {path.strip()}", err=True)
        raise typer.Exit(1)
    typer.echo(value)
