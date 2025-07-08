import typer
from rich.console import Console
from rich.table import Table
from pydantic import BaseModel
from pathlib import Path
from typing import Optional

from meshagent.cli import auth_async
from meshagent.cli import async_typer
from meshagent.api.helpers import meshagent_base_url
from meshagent.api.accounts_client import AccountsClient
from meshagent.api.participant_token import ParticipantToken

import os

SETTINGS_FILE = Path.home() / ".meshagent" / "project.json"


def _ensure_cache_dir():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)


class Settings(BaseModel):
    active_project: Optional[str] = None
    active_api_keys: Optional[dict] = {}


def _save_settings(s: Settings):
    _ensure_cache_dir()
    SETTINGS_FILE.write_text(s.model_dump_json())


def _load_settings():
    _ensure_cache_dir()
    if SETTINGS_FILE.exists():
        return Settings.model_validate_json(SETTINGS_FILE.read_text())

    return Settings()


async def get_active_project():
    settings = _load_settings()
    if settings is None:
        return None
    return settings.active_project


async def set_active_project(project_id: str | None):
    settings = _load_settings()
    settings.active_project = project_id
    _save_settings(settings)


async def get_active_api_key(project_id: str):
    settings = _load_settings()
    if settings is None:
        return None
    return settings.active_api_keys.get(project_id, None)


async def set_active_api_key(project_id: str, api_key_id: str | None):
    settings = _load_settings()
    settings.active_api_keys[project_id] = api_key_id
    _save_settings(settings)


app = async_typer.AsyncTyper()


async def get_client():
    access_token = await auth_async.get_access_token()
    return AccountsClient(base_url=meshagent_base_url(), token=access_token)


def print_json_table(records: list, *cols):
    if not records:
        raise SystemExit("No rows to print")

    # 2️⃣  --- build the table -------------------------------------------
    table = Table(show_header=True, header_style="bold magenta")

    if len(cols) > 0:
        # use the keys of the first object as column order
        for col in cols:
            table.add_column(col.title())  # "id" → "Id"

        for row in records:
            table.add_row(*(str(row.get(col, "")) for col in cols))

    else:
        # use the keys of the first object as column order
        for col in records[0]:
            table.add_column(col.title())  # "id" → "Id"

        for row in records:
            table.add_row(*(str(row.get(col, "")) for col in records[0]))

    # 3️⃣  --- render ------------------------------------------------------
    Console().print(table)


def resolve_room(room_name: Optional[str] = None):
    if room_name is None:
        room_name = os.getenv("MESHAGENT_ROOM")

    return room_name


async def resolve_project_id(project_id: Optional[str] = None):
    if project_id is None:
        project_id = await get_active_project()

    if project_id is None:
        print(
            "[red]Project ID not specified, activate a project or pass a project on the command line[/red]"
        )
        raise typer.Exit(code=1)

    return project_id


async def resolve_api_key(project_id: str, api_key_id: Optional[str] = None):
    if api_key_id is None:
        api_key_id = await get_active_api_key(project_id=project_id)

    if api_key_id is None:
        print(
            "[red]API Key ID not specified, activate an api key or pass an api key id on the command line[/red]"
        )
        raise typer.Exit(code=1)

    return api_key_id


async def resolve_token_jwt(
    *,
    project_id: str,
    api_key_id: Optional[str] = None,
    token_path: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    room: Optional[str] = None,
) -> str:
    jwt = None

    if api_key_id is None:
        if token_path is not None:
            if token_path is None:
                token_path = os.getenv(
                    "MESHAGENT_TOKEN_PATH",
                    (Path.home() / ".meshagent" / "token").as_posix(),
                )

            p = Path(token_path)
            jwt = p.read_text().strip()

        else:
            jwt = os.getenv("MESHAGENT_TOKEN", None)

    if jwt is None:
        account_client = await get_client()
        try:
            key = (
                await account_client.decrypt_project_api_key(
                    project_id=project_id, id=api_key_id
                )
            )["token"]

            token = ParticipantToken(
                name=name, project_id=project_id, api_key_id=api_key_id
            )

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(token=key)
        finally:
            await account_client.close()

    return jwt
