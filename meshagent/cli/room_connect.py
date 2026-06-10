from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Sequence

import jwt
import typer
from typer import _click as typer_click
from pydantic import ValidationError
from rich import print

from meshagent.api import ApiScope, ParticipantToken, RoomException
from meshagent.api.client import ConflictError, NotFoundError, Room
from meshagent.api.helpers import websocket_room_url
from meshagent.cli import async_typer, auth_async
from meshagent.cli.helper import (
    CustomMeshagentClient,
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.local_settings import get_active_user_id, resolve_api_url

_CONNECTED_TOKEN_ENV_NAMES = (
    "MESHAGENT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

if TYPE_CHECKING:
    from meshagent.cli.tui.deploy_room import DeployRoomChoice, DeployRoomPickerResult


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


def _is_participant_token(*, value: str) -> bool:
    try:
        ParticipantToken.from_jwt(value, validate=False)
    except jwt.InvalidTokenError:
        return False
    return True


async def _get_account_client_for_room_connect(*, room: str) -> CustomMeshagentClient:
    meshagent_token = os.getenv("MESHAGENT_TOKEN")
    if (
        meshagent_token is not None
        and os.getenv("MESHAGENT_API_KEY") is None
        and os.getenv("MESHAGENT_SESSION_ID") is None
        and room != os.getenv("MESHAGENT_ROOM")
        and _is_participant_token(value=meshagent_token)
    ):
        return CustomMeshagentClient(
            base_url=resolve_api_url(),
            token=await auth_async.get_access_token(),
        )

    return await get_client()


def _parse_environment_variables(*, values: Sequence[str]) -> list[tuple[str, str]]:
    environment: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise typer.BadParameter("--env must be in the form 'KEY=VALUE'")
        name, env_value = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise typer.BadParameter("--env must include a non-empty variable name")
        environment.append((resolved_name, env_value))
    return environment


def _parse_environment_secret_variables(
    *, values: Sequence[str]
) -> list[_ParsedEnvironmentSecretVariable]:
    environment: list[_ParsedEnvironmentSecretVariable] = []
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(
                "--env-secret must be in the form 'NAME=SECRET_ID'"
            )
        name, secret_source = value.split("=", 1)
        resolved_name = name.strip()
        if resolved_name == "":
            raise typer.BadParameter(
                "--env-secret must include a non-empty variable name"
            )
        resolved_source = secret_source.strip()
        if resolved_source == "":
            raise typer.BadParameter(
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
        raise typer.BadParameter("--identity cannot be empty")
    return normalized_identity


def _normalize_connect_role(*, role: str | None) -> str:
    if role is None:
        return "agent"

    normalized_role = role.strip()
    if normalized_role == "":
        raise typer.BadParameter("--role cannot be empty")
    return normalized_role


def _normalize_connect_template(*, template: str) -> str:
    normalized_template = template.strip()
    if normalized_template not in {"agent", "none"}:
        raise typer.BadParameter("--template must be agent or none")
    return normalized_template


def _parse_meshagent_token_scope(*, value: str) -> ApiScope:
    cleaned = value.strip()
    if cleaned == "":
        raise typer.BadParameter("--meshagent-token cannot be empty")
    if cleaned == "userDefault":
        return ApiScope.user_default()
    if cleaned == "agentDefault":
        return ApiScope.agent_default()
    if cleaned == "full":
        return ApiScope.full()
    try:
        return ApiScope.model_validate_json(cleaned)
    except (ValidationError, ValueError) as exc:
        raise typer.BadParameter(
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
        raise typer.BadParameter(
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
        raise typer_click.exceptions.ClickException(
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


def _stdio_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def _user_can_create_connect_room(
    account_client: CustomMeshagentClient,
    *,
    project_id: str,
) -> bool:
    return await account_client.can_create_rooms(project_id)


async def _list_connect_rooms(
    account_client: CustomMeshagentClient,
    *,
    project_id: str,
) -> list[Room]:
    rooms_by_id: dict[str, Room] = {}
    limit = 500
    offset = 0
    while True:
        room_grants = await account_client.list_room_grants_by_user(
            project_id=project_id,
            user_id="me",
            limit=limit,
            offset=offset,
            order_by="room_name",
        )
        for room_grant in room_grants:
            rooms_by_id.setdefault(room_grant.room.id, room_grant.room)
        if len(room_grants) < limit:
            break
        offset += limit

    return sorted(rooms_by_id.values(), key=lambda room: room.name.lower())


async def _run_connect_room_picker_tui(
    *,
    rooms: Sequence["DeployRoomChoice"],
    can_create_room: bool,
    create_error: str | None,
) -> "DeployRoomPickerResult":
    from meshagent.cli.tui.deploy_room import run_deploy_room_picker_tui

    return await run_deploy_room_picker_tui(
        rooms=rooms,
        can_create_room=can_create_room,
        create_error=create_error,
        title="MeshAgent Room Connect",
        selection_message="Choose a room for local development.",
        selection_help_text=(
            "Use an existing room or create a new one. "
            "Use Up/Down and Enter. Esc or Ctrl+C cancels."
        ),
        create_message="Enter a name for the new room.",
        create_help_text=(
            "Press Enter to create the room. Esc or Ctrl+C returns to rooms."
        ),
        cancel_message="Room connect canceled.",
    )


async def _select_connect_room_interactively(*, project_id: str) -> str:
    account_client = await get_client()
    try:
        can_create_room = await _user_can_create_connect_room(
            account_client,
            project_id=project_id,
        )
        rooms = await _list_connect_rooms(
            account_client,
            project_id=project_id,
        )

        if len(rooms) == 0 and not can_create_room:
            print(
                "[red]No rooms found for this project. Create a room or pass --room "
                "explicitly.[/red]"
            )
            raise SystemExit(1)

        from meshagent.cli.tui.deploy_room import DeployRoomChoice

        room_choices = [
            DeployRoomChoice(
                id=room.id,
                name=room.name,
                description=room.annotations.get("meshagent.storage.class", ""),
            )
            for room in rooms
        ]
        create_error: str | None = None
        while True:
            result = await _run_connect_room_picker_tui(
                rooms=room_choices,
                can_create_room=can_create_room,
                create_error=create_error,
            )
            if result.status == "create" and result.create_room_name is not None:
                active_user_id = get_active_user_id()
                if active_user_id is None:
                    print(
                        "[red]Unable to determine the active user for the room owner "
                        "grant. Run `meshagent auth login` or pass --room explicitly."
                        "[/red]"
                    )
                    raise SystemExit(1)
                try:
                    room = await account_client.create_room(
                        project_id=project_id,
                        name=result.create_room_name,
                        permissions={active_user_id: ApiScope.full()},
                    )
                except ConflictError:
                    create_error = (
                        f"Room name '{result.create_room_name}' is already in use. "
                        "Enter a different room name."
                    )
                    continue
                print(f"[bold green]Created room {room.name}[/bold green]")
                return room.name
            if result.status == "completed" and result.selected_room_name is not None:
                return result.selected_room_name

            raise SystemExit(130)
    finally:
        await account_client.close()


async def _resolve_connected_room_inputs(
    *,
    project_id: str | None,
    room: str | None,
) -> tuple[str, str]:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    resolved_room = resolve_room(room)
    if resolved_room is None and _stdio_is_interactive():
        resolved_room = await _select_connect_room_interactively(
            project_id=resolved_project_id
        )
    if resolved_room is None:
        print("[red]--room is required (or set MESHAGENT_ROOM).[/red]")
        raise typer_click.exceptions.Exit(1)
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
        api_url=resolve_api_url(),
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
        account_client = await _get_account_client_for_room_connect(
            room=resolved_room,
        )
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
    role: str,
    api_scope: ApiScope,
) -> _ConnectedRoomEnv:
    token = await _mint_connected_meshagent_token(
        project_id=project_id,
        room_name=room,
        identity=identity,
        role=role,
        api_scope=api_scope,
    )
    return _ConnectedRoomEnv(
        project_id=project_id,
        api_url=resolve_api_url(),
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
        raise typer_click.exceptions.ClickException(
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
    role: str | None = None,
    meshagent_token: str | None = None,
    template: str = "agent",
) -> dict[str, str]:
    parsed_environment = _parse_environment_variables(values=env)
    parsed_secret_environment = _parse_environment_secret_variables(values=env_secret)
    normalized_identity = _normalize_connect_identity(identity=identity)
    normalized_role = _normalize_connect_role(role=role)
    normalized_template = _normalize_connect_template(template=template)
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
            or role is not None
            or meshagent_token_scope is not None
            or len(parsed_secret_environment) > 0
        )

        if uses_local_token:
            if normalized_identity is None:
                if role is not None:
                    raise typer.BadParameter("--identity is required when using --role")
                if parsed_secret_environment and meshagent_token_scope is not None:
                    raise typer.BadParameter(
                        "--identity is required when using --env-secret or "
                        "--meshagent-token"
                    )
                if parsed_secret_environment:
                    raise typer.BadParameter(
                        "--identity is required when using --env-secret"
                    )
                raise typer.BadParameter(
                    "--identity is required when using --meshagent-token"
                )
            room_env = await _mint_connected_room_env(
                project_id=resolved_project_id,
                room=resolved_room,
                identity=normalized_identity,
                role=normalized_role,
                api_scope=meshagent_token_scope or ApiScope.agent_default(),
            )
            connected_token = room_env.token
            resolved_secret_identity = normalized_identity
            if parsed_secret_environment:
                account_client = await get_client()
        else:
            account_client = await _get_account_client_for_room_connect(
                room=resolved_room,
            )
            room_env = await _connect_room_env_resolved(
                project_id=resolved_project_id,
                room=resolved_room,
                account_client=account_client,
            )
            connected_token = room_env.token
            resolved_secret_identity = None

        child_env = os.environ.copy()
        if normalized_template == "agent":
            child_env["MESHAGENT_API_URL"] = room_env.api_url
            child_env["MESHAGENT_PROJECT_ID"] = room_env.project_id
            child_env["MESHAGENT_ROOM"] = room_env.room_name
            child_env["OPENAI_BASE_URL"] = f"{room_env.room_url}/openai/v1"
            child_env["ANTHROPIC_BASE_URL"] = f"{room_env.room_url}/anthropic"
            _set_connected_token_environment(
                child_env=child_env,
                connected_token=connected_token,
            )
        elif meshagent_token_scope is not None:
            child_env["MESHAGENT_TOKEN"] = connected_token

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
                    raise typer.BadParameter(
                        f"environment variable '{env_var.name}' references missing "
                        f"secret '{resolved_secret_identity}/{env_var.source}'. Save the "
                        "room secret first, then retry room connect."
                    ) from exc
                except RoomException as exc:
                    raise typer_click.exceptions.ClickException(str(exc)) from exc
                child_env[env_var.name] = _decode_environment_secret_value(
                    env_name=env_var.name,
                    secret_id=f"{resolved_secret_identity}/{env_var.source}",
                    data=secret.data,
                )

        return child_env
    finally:
        if account_client is not None:
            await account_client.close()


app = async_typer.AsyncTyper(add_completion=False)


@app.command(
    "connect",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    help=(
        "Connect to a room and run a local command with "
        "MESHAGENT_API_URL, MESHAGENT_PROJECT_ID, MESHAGENT_TOKEN, "
        "OPENAI_API_KEY, ANTHROPIC_API_KEY, and MESHAGENT_ROOM set by "
        "the default agent template. Use -- before the local command."
    ),
)
def _connect_command(
    ctx: typer.Context,
    command: list[str] | None = typer.Argument(
        None,
    ),
    project_id: str | None = typer.Option(
        None,
        "--project-id",
        help="A MeshAgent project id. If empty, the activated project will be used.",
    ),
    room: str | None = typer.Option(None, "--room", help="Room name"),
    env: list[str] | None = typer.Option(
        None,
        "--env",
        "-e",
        help="Set environment variable as KEY=VALUE",
    ),
    env_secret: list[str] | None = typer.Option(
        None,
        "--env-secret",
        help="Set environment variable from a room secret as NAME=SECRET_ID",
    ),
    identity: str | None = typer.Option(
        None,
        "--identity",
        help=(
            "Identity name to use for the connected token, --meshagent-token, and "
            "--env-secret. Required with --role, --meshagent-token, and "
            "--env-secret. When set, room connect mints a participant token locally."
        ),
    ),
    role: str | None = typer.Option(
        None,
        "--role",
        help=(
            "Participant role for locally minted tokens. Requires --identity and "
            "defaults to agent."
        ),
    ),
    meshagent_token: str | None = typer.Option(
        None,
        "--meshagent-token",
        help=(
            "Inject MESHAGENT_TOKEN using userDefault, agentDefault, full, "
            "or a JSON ApiScope object."
        ),
    ),
    template: str = typer.Option(
        "agent",
        "--template",
        show_default=True,
        help=(
            "Allowed values: agent, none. agent: MeshAgent sets MESHAGENT_TOKEN, "
            "OPENAI_API_KEY, and ANTHROPIC_API_KEY to a room-scoped MeshAgent token, "
            "and sets OPENAI_BASE_URL, ANTHROPIC_BASE_URL, MESHAGENT_API_URL, "
            "MESHAGENT_PROJECT_ID, and MESHAGENT_ROOM from the connected room unless "
            "manually set. none: MeshAgent applies no template defaults."
        ),
    ),
) -> None:
    command_values = tuple(command or ctx.args)
    if len(command_values) == 0:
        raise typer_click.exceptions.UsageError(
            "Pass the local command after --, for example: "
            "meshagent room connect -- python script.py"
        )

    child_env = async_typer._run_coroutine_sync(
        _build_connected_command_env(
            project_id=project_id,
            room=room,
            env=env or (),
            env_secret=env_secret or (),
            identity=identity,
            role=role,
            meshagent_token=meshagent_token,
            template=template,
        )
    )
    raise typer.Exit(
        _run_connected_command(command=command_values, child_env=child_env)
    )


connect_command = async_typer.get_command(app)
