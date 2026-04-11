from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Sequence

import click
from pydantic import ValidationError
from rich import print

from meshagent.api import ApiScope, ParticipantToken, RoomException
from meshagent.api.client import NotFoundError
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    CustomMeshagentClient,
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
)

_CONNECTED_TOKEN_ENV_NAMES = (
    "MESHAGENT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@dataclass(frozen=True)
class _ConnectedRoomEnv:
    project_id: str
    api_url: str
    room_name: str
    room_url: str
    token: str


@dataclass(frozen=True)
class _ParsedEnvironmentSecretVariable:
    name: str
    source: str


def _normalize_room_url(*, room_url: str) -> str:
    normalized = room_url.strip().rstrip("/")
    if normalized.startswith("wss:"):
        return "https:" + normalized.removeprefix("wss:")
    if normalized.startswith("ws:"):
        return "http:" + normalized.removeprefix("ws:")
    return normalized


def _parse_environment_variables(*, values: Sequence[str]) -> list[tuple[str, str]]:
    environment: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise click.BadParameter("--env must be in the form 'KEY=VALUE'")
        name, env_value = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise click.BadParameter("--env must include a non-empty variable name")
        environment.append((resolved_name, env_value))
    return environment


def _parse_environment_secret_variables(
    *, values: Sequence[str]
) -> list[_ParsedEnvironmentSecretVariable]:
    environment: list[_ParsedEnvironmentSecretVariable] = []
    for value in values:
        if "=" not in value:
            raise click.BadParameter(
                "--env-secret must be in the form 'NAME=SECRET_ID'"
            )
        name, secret_source = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise click.BadParameter(
                "--env-secret must include a non-empty variable name"
            )
        resolved_source = secret_source.strip()
        if resolved_source == "":
            raise click.BadParameter(
                "--env-secret must include a non-empty secret source"
            )
        environment.append(
            _ParsedEnvironmentSecretVariable(
                name=resolved_name,
                source=resolved_source,
            )
        )
    return environment


def _normalize_connect_identity(*, identity: str | None) -> str | None:
    if identity is None:
        return None

    normalized_identity = identity.strip()
    if normalized_identity == "":
        raise click.BadParameter("--identity cannot be empty")
    return normalized_identity


def _parse_meshagent_token_scope(*, value: str) -> ApiScope:
    cleaned = value.strip()
    if cleaned == "":
        raise click.BadParameter("--meshagent-token cannot be empty")
    if cleaned == "userDefault":
        return ApiScope.user_default()
    if cleaned == "agentDefault":
        return ApiScope.agent_default()
    if cleaned == "full":
        return ApiScope.full()
    try:
        return ApiScope.model_validate_json(cleaned)
    except (ValidationError, ValueError) as exc:
        raise click.BadParameter(
            "--meshagent-token must be one of userDefault, agentDefault, full, "
            "or a JSON ApiScope object"
        ) from exc


def _decode_environment_secret_value(
    *,
    env_name: str,
    secret_id: str,
    data: bytes,
) -> str:
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        raise click.BadParameter(
            f"environment variable '{env_name}' references secret '{secret_id}', "
            "but room connect env secrets must contain UTF-8 text"
        ) from exc


async def _mint_connected_meshagent_token(
    *,
    project_id: str,
    room_name: str,
    identity: str,
    role: str,
    api_scope: ApiScope,
) -> str:
    key = await resolve_key(project_id=project_id, key=None)
    if (
        key is None
        and os.getenv("MESHAGENT_API_KEY") is None
        and os.getenv("MESHAGENT_SECRET") is None
    ):
        raise click.ClickException(
            "Minting a connected room token locally requires an API key or "
            "signing secret. Set MESHAGENT_API_KEY, activate an API key for the "
            "project, or set MESHAGENT_SECRET."
        )

    token = ParticipantToken(name=identity, project_id=project_id)
    if role != "user":
        token.add_role_grant(role)
    token.add_api_grant(api_scope)
    token.add_room_grant(room_name)
    return token.to_jwt(api_key=key)


async def _resolve_connected_room_inputs(
    *,
    project_id: str | None,
    room: str | None,
) -> tuple[str, str]:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    resolved_room = resolve_room(room)
    if resolved_room is None:
        print("[red]--room is required (or set MESHAGENT_ROOM).[/red]")
        raise click.exceptions.Exit(1)
    return resolved_project_id, resolved_room


async def _connect_room_env_resolved(
    *,
    project_id: str,
    room: str,
    account_client: CustomMeshagentClient,
) -> _ConnectedRoomEnv:
    connection = await account_client.connect_room(
        project_id=project_id,
        room=room,
    )
    return _ConnectedRoomEnv(
        project_id=project_id,
        api_url=os.getenv("MESHAGENT_API_URL") or meshagent_base_url(),
        room_name=connection.room_name,
        room_url=_normalize_room_url(room_url=connection.room_url),
        token=connection.jwt,
    )


async def _connect_room_env(
    *,
    project_id: str | None,
    room: str | None,
    account_client: CustomMeshagentClient | None = None,
) -> _ConnectedRoomEnv:
    resolved_project_id, resolved_room = await _resolve_connected_room_inputs(
        project_id=project_id,
        room=room,
    )
    owns_account_client = account_client is None
    if account_client is None:
        account_client = await get_client()
    try:
        return await _connect_room_env_resolved(
            project_id=resolved_project_id,
            room=resolved_room,
            account_client=account_client,
        )
    finally:
        if owns_account_client:
            await account_client.close()


async def _mint_connected_room_env(
    *,
    project_id: str,
    room: str,
    identity: str,
    api_scope: ApiScope,
) -> _ConnectedRoomEnv:
    token = await _mint_connected_meshagent_token(
        project_id=project_id,
        room_name=room,
        identity=identity,
        role="agent",
        api_scope=api_scope,
    )
    return _ConnectedRoomEnv(
        project_id=project_id,
        api_url=os.getenv("MESHAGENT_API_URL") or meshagent_base_url(),
        room_name=room,
        room_url=_normalize_room_url(room_url=websocket_room_url(room_name=room)),
        token=token,
    )


def _run_connected_command(
    *,
    command: Sequence[str],
    child_env: dict[str, str],
) -> int:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            env=child_env,
        )
    except OSError as exc:
        error_message = exc.strerror or str(exc)
        raise click.ClickException(
            f"Failed to start {command[0]}: {error_message}"
        ) from exc

    return result.returncode


def _set_connected_token_environment(
    *,
    child_env: dict[str, str],
    connected_token: str,
) -> None:
    for env_name in _CONNECTED_TOKEN_ENV_NAMES:
        child_env[env_name] = connected_token


async def _build_connected_command_env(
    *,
    project_id: str | None,
    room: str | None,
    env: Sequence[str],
    env_secret: Sequence[str],
    identity: str | None,
    meshagent_token: str | None,
) -> dict[str, str]:
    parsed_environment = _parse_environment_variables(values=env)
    parsed_secret_environment = _parse_environment_secret_variables(values=env_secret)
    normalized_identity = _normalize_connect_identity(identity=identity)
    meshagent_token_scope = (
        _parse_meshagent_token_scope(value=meshagent_token)
        if meshagent_token is not None
        else None
    )
    resolved_project_id, resolved_room = await _resolve_connected_room_inputs(
        project_id=project_id,
        room=room,
    )
    account_client: CustomMeshagentClient | None = None
    try:
        connected_token: str
        resolved_secret_identity: str | None
        uses_local_token = (
            normalized_identity is not None
            or meshagent_token_scope is not None
            or len(parsed_secret_environment) > 0
        )

        if uses_local_token:
            if normalized_identity is None:
                if parsed_secret_environment and meshagent_token_scope is not None:
                    raise click.BadParameter(
                        "--identity is required when using --env-secret or "
                        "--meshagent-token"
                    )
                if parsed_secret_environment:
                    raise click.BadParameter(
                        "--identity is required when using --env-secret"
                    )
                raise click.BadParameter(
                    "--identity is required when using --meshagent-token"
                )
            room_env = await _mint_connected_room_env(
                project_id=resolved_project_id,
                room=resolved_room,
                identity=normalized_identity,
                api_scope=meshagent_token_scope or ApiScope.agent_default(),
            )
            connected_token = room_env.token
            resolved_secret_identity = normalized_identity
            if parsed_secret_environment:
                account_client = await get_client()
        else:
            account_client = await get_client()
            room_env = await _connect_room_env_resolved(
                project_id=resolved_project_id,
                room=resolved_room,
                account_client=account_client,
            )
            connected_token = room_env.token
            resolved_secret_identity = None

        child_env = os.environ.copy()
        child_env["MESHAGENT_API_URL"] = room_env.api_url
        child_env["MESHAGENT_PROJECT_ID"] = room_env.project_id
        child_env["MESHAGENT_ROOM"] = room_env.room_name
        child_env["OPENAI_BASE_URL"] = f"{room_env.room_url}/openai/v1"
        child_env["ANTHROPIC_BASE_URL"] = f"{room_env.room_url}/anthropic"
        _set_connected_token_environment(
            child_env=child_env,
            connected_token=connected_token,
        )

        for name, value in parsed_environment:
            child_env[name] = value
        if parsed_secret_environment:
            if account_client is None:
                account_client = await get_client()
            if resolved_secret_identity is None:
                raise AssertionError("resolved_secret_identity must be set")
            for env_var in parsed_secret_environment:
                try:
                    secret = await account_client.get_room_secret(
                        project_id=room_env.project_id,
                        room_name=room_env.room_name,
                        secret_id=env_var.source,
                        for_identity=resolved_secret_identity,
                    )
                except NotFoundError as exc:
                    raise click.BadParameter(
                        f"environment variable '{env_var.name}' references missing "
                        f"secret '{resolved_secret_identity}/{env_var.source}'. Save the "
                        "room secret first, then retry room connect."
                    ) from exc
                except RoomException as exc:
                    raise click.ClickException(str(exc)) from exc
                child_env[env_var.name] = _decode_environment_secret_value(
                    env_name=env_var.name,
                    secret_id=f"{resolved_secret_identity}/{env_var.source}",
                    data=secret.data,
                )

        return child_env
    finally:
        if account_client is not None:
            await account_client.close()


@click.command(
    "connect",
    help=(
        "Connect to a room and run a local command with "
        "MESHAGENT_API_URL, MESHAGENT_PROJECT_ID, MESHAGENT_TOKEN, "
        "OPENAI_API_KEY, ANTHROPIC_API_KEY, and MESHAGENT_ROOM set. "
        "Use -- before the local command."
    ),
)
@click.option(
    "--project-id",
    help="A MeshAgent project id. If empty, the activated project will be used.",
)
@click.option("--room", help="Room name")
@click.option(
    "--env",
    "-e",
    multiple=True,
    help="Set environment variable as KEY=VALUE",
)
@click.option(
    "--env-secret",
    multiple=True,
    help="Set environment variable from a room secret as NAME=SECRET_ID",
)
@click.option(
    "--identity",
    help=(
        "Identity name to use for the connected token, --meshagent-token, and "
        "--env-secret. Required with --meshagent-token and --env-secret. When "
        "set, room connect mints a participant token locally."
    ),
)
@click.option(
    "--meshagent-token",
    help=(
        "Inject MESHAGENT_TOKEN using userDefault, agentDefault, full, "
        "or a JSON ApiScope object."
    ),
)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def connect_command(
    project_id: str | None,
    room: str | None,
    env: tuple[str, ...],
    env_secret: tuple[str, ...],
    identity: str | None,
    meshagent_token: str | None,
    command: tuple[str, ...],
) -> None:
    if len(command) == 0:
        raise click.UsageError(
            "Pass the local command after --, for example: "
            "meshagent room connect -- python script.py"
        )

    child_env = async_typer._run_coroutine_sync(
        _build_connected_command_env(
            project_id=project_id,
            room=room,
            env=env,
            env_secret=env_secret,
            identity=identity,
            meshagent_token=meshagent_token,
        )
    )
    raise click.exceptions.Exit(
        _run_connected_command(command=command, child_env=child_env)
    )
