import asyncio
import os
import shlex
import json
import sys
import logging
import yaml
from pathlib import Path
from typing import Annotated, List, Optional, Literal

import typer
from rich import print

from meshagent.agents.config import RulesConfig
from meshagent.api import (
    ApiScope,
    ParticipantToken,
    RequiredSchema,
    RequiredToolkit,
    RoomClient,
    RoomException,
    RemoteParticipant,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.messaging import JsonResponse, TextResponse
from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ContainerMountSpec,
    ProjectStorageMountSpec,
    RoomStorageMountSpec,
)
from meshagent.api.client import ConflictError
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    cleanup_args,
    get_client,
    split_container_mount,
    resolve_key,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.host import (
    get_deferred,
    get_service,
    run_services,
    service_specs,
)

app = async_typer.AsyncTyper(help="Codex-backed agents")
chatbot_app = async_typer.AsyncTyper(help="Run codex chatbot agents")
task_runner_app = async_typer.AsyncTyper(help="Run codex task-runner agents")
worker_app = async_typer.AsyncTyper(help="Run codex worker agents")
app.add_typer(chatbot_app, name="chatbot")
app.add_typer(task_runner_app, name="task-runner")
app.add_typer(worker_app, name="worker")

DelegateShellTokenOption = Annotated[
    bool,
    typer.Option(
        "--delegate-shell-token",
        help=(
            "Pass the room token through to codex app-server as "
            "MESHAGENT_TOKEN / OPENAI_API_KEY / ANTHROPIC_API_KEY."
        ),
    ),
]

CopyEnvOption = Annotated[
    list[str],
    typer.Option(
        "--copy-env",
        help=(
            "Copy local env vars into codex app-server env. "
            "Accepts comma-separated names and can be repeated."
        ),
    ),
]

SetEnvOption = Annotated[
    list[str],
    typer.Option(
        "--set-env",
        help=("Set env vars in codex app-server env as NAME=VALUE. Can be repeated."),
    ),
]


def _room_openai_base_url(*, room_name: str) -> str:
    room_url = websocket_room_url(room_name=room_name)

    if room_url.startswith("wss:"):
        room_url = "https:" + room_url.removeprefix("wss:")
    elif room_url.startswith("ws:"):
        room_url = "http:" + room_url.removeprefix("ws:")

    return f"{room_url}/openai/v1"


def _load_rules(
    *,
    rule: list[str],
    rules_file: Optional[list[str]],
) -> dict[str, list[str]]:
    client_rules: dict[str, list[str]] = {}
    if rules_file is None:
        return client_rules

    for rules_path in rules_file:
        try:
            with open(Path(os.path.expanduser(rules_path)).resolve(), "r") as f:
                rules_config = RulesConfig.parse(f.read())
                if rules_config.rules is not None:
                    rule.extend(rules_config.rules)
                if rules_config.client_rules is not None:
                    client_rules.update(rules_config.client_rules)
        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    return client_rules


def _default_codex_command(*, room_name: str) -> str:
    openai_base_url = _room_openai_base_url(room_name=room_name)
    return (
        "codex app-server "
        "-c model_providers.openai.name='OpenAI' "
        "-c "
        f"model_providers.openai.base_url={shlex.quote(openai_base_url)}"
    )


def _default_codex_container_command() -> str:
    return (
        "codex app-server "
        "-c model_providers.openai.name='OpenAI' "
        '-c model_providers.openai.base_url="$OPENAI_BASE_URL"'
    )


def _delegate_shell_token_env(*, jwt: Optional[str]) -> Optional[dict[str, str]]:
    token = jwt or os.getenv("MESHAGENT_TOKEN")
    if token is None:
        return None

    normalized = token.strip()
    if normalized == "":
        return None

    return {
        "MESHAGENT_TOKEN": normalized,
        "OPENAI_API_KEY": normalized,
        "ANTHROPIC_API_KEY": normalized,
    }


def _copy_env_vars(*, copy_env: Optional[list[str]]) -> dict[str, str]:
    if copy_env is None:
        return {}

    names: list[str] = []
    seen: set[str] = set()
    for item in copy_env:
        for split_item in item.split(","):
            name = split_item.strip()
            if name == "":
                continue
            if name in seen:
                continue
            seen.add(name)
            names.append(name)

    env: dict[str, str] = {}
    for name in names:
        value = os.getenv(name)
        if value is None:
            raise typer.BadParameter(f"--copy-env variable is not set: {name}")
        env[name] = value

    return env


def _set_env_vars(*, set_env: Optional[list[str]]) -> dict[str, str]:
    if set_env is None:
        return {}

    env: dict[str, str] = {}
    for item in set_env:
        value = item.strip()
        if value == "":
            continue

        if "=" not in value:
            raise typer.BadParameter(f"--set-env value must be NAME=VALUE, got: {item}")

        name, assigned_value = value.split("=", 1)
        name = name.strip()
        if name == "":
            raise typer.BadParameter(f"--set-env variable name cannot be empty: {item}")

        env[name] = assigned_value

    return env


def _read_task_runner_input(input_value: Optional[str]) -> str:
    if input_value is None:
        if sys.stdin.isatty():
            print("[bold red]--input is required unless stdin is provided[/bold red]")
            raise typer.Exit(1)
        input_value = sys.stdin.read()
    elif input_value == "-":
        input_value = sys.stdin.read()

    if not input_value:
        print("[bold red]input payload is empty[/bold red]")
        raise typer.Exit(1)

    return input_value


def _normalize_codex_image(codex_image: Optional[str]) -> Optional[str]:
    if codex_image is None:
        return None

    normalized = codex_image.strip()
    if normalized == "":
        return None

    if normalized.lower() == "none":
        return None

    return normalized


def _resolve_sandbox_policy(
    *,
    codex_image: Optional[str],
    ws_url: Optional[str],
    sandbox_policy: Optional[str],
) -> Optional[str]:
    if sandbox_policy is not None:
        return sandbox_policy

    if codex_image is not None and ws_url is None:
        return "danger-full-access"

    return None


CodexMode = Literal[
    "default",
    "yolo",
    "workspace-write",
    "read-only",
]


def _resolve_codex_policies(
    *,
    mode: Optional[str],
    codex_image: Optional[str],
    ws_url: Optional[str],
    approval_policy: Optional[str],
    sandbox_policy: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    mode_approval_policy = None
    mode_sandbox_policy = None
    if mode is not None:
        normalized_mode = mode.strip().lower().replace("_", "-")
        if normalized_mode == "default":
            pass
        elif normalized_mode == "yolo":
            mode_approval_policy = "never"
            mode_sandbox_policy = "danger-full-access"
        elif normalized_mode == "workspace-write":
            mode_approval_policy = "never"
            mode_sandbox_policy = "workspace-write"
        elif normalized_mode == "read-only":
            mode_approval_policy = "never"
            mode_sandbox_policy = "read-only"
        else:
            raise typer.BadParameter(
                "Invalid --mode. Expected one of: default, yolo, workspace-write, read-only"
            )

    if approval_policy is None:
        approval_policy = mode_approval_policy

    if sandbox_policy is None:
        sandbox_policy = mode_sandbox_policy

    sandbox_policy = _resolve_sandbox_policy(
        codex_image=codex_image,
        ws_url=ws_url,
        sandbox_policy=sandbox_policy,
    )

    return approval_policy, sandbox_policy


def _parse_codex_mounts(
    *,
    mount_room_path: list[str],
    mount_project_path: list[str],
) -> Optional[ContainerMountSpec]:
    room_mounts: list[RoomStorageMountSpec] = []
    project_mounts: list[ProjectStorageMountSpec] = []

    for value in mount_room_path:
        source, mount, read_only = split_container_mount(
            value, "--mount-room-path", False
        )
        subpath = source if source not in {"", ".", "/"} else None
        room_mounts.append(
            RoomStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    for value in mount_project_path:
        source, mount, read_only = split_container_mount(
            value, "--mount-project-path", True
        )
        subpath = source if source not in {"", ".", "/"} else None
        project_mounts.append(
            ProjectStorageMountSpec(path=mount, subpath=subpath, read_only=read_only)
        )

    if not room_mounts and not project_mounts:
        return None

    return ContainerMountSpec(
        room=room_mounts or None,
        project=project_mounts or None,
    )


async def _upsert_service(
    *,
    project_id: str,
    room: Optional[str],
    service_spec,
):
    client = await get_client()
    try:
        service_id = None
        if room is None:
            services = await client.list_services(project_id=project_id)
        else:
            services = await client.list_room_services(
                project_id=project_id, room_name=room
            )

        for service in services:
            if service.metadata.name == service_spec.metadata.name:
                service_id = service.id
                break

        if service_id is None:
            if room is None:
                service_id = await client.create_service(
                    project_id=project_id, service=service_spec
                )
            else:
                service_id = await client.create_room_service(
                    project_id=project_id,
                    service=service_spec,
                    room_name=room,
                )
        else:
            service_spec.id = service_id
            if room is None:
                await client.update_service(
                    project_id=project_id,
                    service_id=service_id,
                    service=service_spec,
                )
            else:
                await client.update_room_service(
                    project_id=project_id,
                    service_id=service_id,
                    service=service_spec,
                    room_name=room,
                )

        return service_id
    finally:
        await client.close()


def _resolve_codex_runtime(
    *,
    command: Optional[str],
    ws_url: Optional[str],
    codex_image: Optional[str] = None,
    room_name: Optional[str] = None,
    jwt: Optional[str] = None,
    delegate_shell_token: bool = False,
    copy_env: Optional[list[str]] = None,
    set_env: Optional[list[str]] = None,
) -> tuple[Optional[str], Optional[str], Optional[dict[str, str]]]:
    if command is not None and ws_url is not None:
        print(
            "[yellow]Both --command and --ws-url were provided. Using --ws-url and ignoring --command.[/yellow]"
        )

    if ws_url is not None:
        return None, ws_url, None

    app_server_env = _copy_env_vars(copy_env=copy_env)
    app_server_env.update(_set_env_vars(set_env=set_env))
    if delegate_shell_token:
        delegate_env = _delegate_shell_token_env(jwt=jwt)
        if delegate_env is not None:
            app_server_env.update(delegate_env)

    resolved_app_server_env = app_server_env or None
    if room_name is not None and command is None:
        if codex_image is not None:
            command = _default_codex_container_command()
        else:
            command = _default_codex_command(room_name=room_name)

    return command, None, resolved_app_server_env


def build_codex_chatbot(
    *,
    model: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    rule: list[str],
    toolkit: list[str],
    schema: list[str],
    rules_file: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    skill_dirs: Optional[list[str]] = None,
    command: Optional[str] = None,
    ws_url: Optional[str] = None,
    codex_image: Optional[str] = None,
    codex_mounts: Optional[ContainerMountSpec] = None,
    working_dir: Optional[str] = None,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[str] = None,
    app_server_env: Optional[dict[str, str]] = None,
    verbose: bool = False,
):
    try:
        from meshagent.codex import CodexChatBot
    except ImportError as exc:
        raise typer.BadParameter(
            "meshagent-codex is required for this command. Install the package and try again."
        ) from exc

    requirements = []
    for toolkit_name in toolkit:
        requirements.append(RequiredToolkit(name=toolkit_name))
    for schema_name in schema:
        requirements.append(RequiredSchema(name=schema_name))

    client_rules = _load_rules(
        rule=rule,
        rules_file=rules_file,
    )

    class CustomCodexChatBot(CodexChatBot):
        def __init__(self):
            super().__init__(
                title=title,
                description=description,
                requires=requirements,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules if len(client_rules) > 0 else None,
                skill_dirs=skill_dirs if len(skill_dirs or []) > 0 else None,
                model=model,
                command=command,
                ws_url=ws_url,
                image=codex_image,
                mounts=codex_mounts,
                cwd=working_dir,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                app_server_env=app_server_env,
                verbose=verbose,
            )

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_path is not None:
                for path in room_rules_path:
                    await self._load_room_rules(path=path)

        async def _load_room_rules(
            self,
            *,
            path: str,
            participant: Optional[RemoteParticipant] = None,
        ) -> list[str]:
            rules = []
            try:
                room_rules = await self.room.storage.download(path=path)

                rules_txt = room_rules.data.decode()

                rules_config = RulesConfig.parse(rules_txt)

                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)

                if participant is not None:
                    client = participant.get_attribute("client")
                    if rules_config.client_rules is not None and client is not None:
                        client_rules = rules_config.client_rules.get(client)
                        if client_rules is not None:
                            rules.extend(client_rules)

            except RoomException:
                try:
                    handle = await self.room.storage.open(path=path, overwrite=False)
                    await self.room.storage.write(
                        handle=handle,
                        data="# Add rules to this file to customize your agent's behavior, lines starting with # will be ignored.\n\n".encode(),
                    )
                    await self.room.storage.close(handle=handle)
                except RoomException:
                    pass

            return rules

        async def get_rules(self, *, thread_context, participant):
            rules = await super().get_rules(
                thread_context=thread_context,
                participant=participant,
            )

            if room_rules_path is not None:
                for path in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(
                            path=path,
                            participant=participant,
                        )
                    )

            return rules

        async def create_thread_context(
            self,
            *,
            path: str,
            thread,
            participants,
            event_handler,
        ):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().create_thread_context(
                path=path,
                thread=thread,
                participants=participants,
                event_handler=event_handler,
            )
            await init_context_from_spec(context.chat)
            return context

    return CustomCodexChatBot


def build_codex_task_runner(
    *,
    model: str,
    title: Optional[str],
    description: Optional[str],
    supports_tools: bool,
    rule: list[str],
    toolkit: list[str],
    schema: list[str],
    rules_file: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    skill_dirs: Optional[list[str]] = None,
    command: Optional[str] = None,
    ws_url: Optional[str] = None,
    codex_image: Optional[str] = None,
    codex_mounts: Optional[ContainerMountSpec] = None,
    working_dir: Optional[str] = None,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[str] = None,
    app_server_env: Optional[dict[str, str]] = None,
    verbose: bool = False,
):
    try:
        from meshagent.codex import CodexTaskRunner
    except ImportError as exc:
        raise typer.BadParameter(
            "meshagent-codex is required for this command. Install the package and try again."
        ) from exc

    requirements = []
    for toolkit_name in toolkit:
        requirements.append(RequiredToolkit(name=toolkit_name))
    for schema_name in schema:
        requirements.append(RequiredSchema(name=schema_name))

    client_rules = _load_rules(
        rule=rule,
        rules_file=rules_file,
    )

    class CustomCodexTaskRunner(CodexTaskRunner):
        def __init__(self):
            super().__init__(
                title=title,
                description=description,
                requires=requirements,
                supports_tools=supports_tools,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules if len(client_rules) > 0 else None,
                skill_dirs=skill_dirs if len(skill_dirs or []) > 0 else None,
                model=model,
                command=command,
                ws_url=ws_url,
                image=codex_image,
                mounts=codex_mounts,
                cwd=working_dir,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                app_server_env=app_server_env,
                verbose=verbose,
            )

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_path is not None:
                for path in room_rules_path:
                    await self._load_room_rules(path=path)

        async def _load_room_rules(
            self,
            *,
            path: str,
            participant: Optional[RemoteParticipant] = None,
        ) -> list[str]:
            rules = []
            try:
                room_rules = await self.room.storage.download(path=path)
                rules_txt = room_rules.data.decode()
                rules_config = RulesConfig.parse(rules_txt)

                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)

                if participant is not None:
                    client = participant.get_attribute("client")
                    if rules_config.client_rules is not None and client is not None:
                        client_rules = rules_config.client_rules.get(client)
                        if client_rules is not None:
                            rules.extend(client_rules)

            except RoomException:
                try:
                    handle = await self.room.storage.open(path=path, overwrite=False)
                    await self.room.storage.write(
                        handle=handle,
                        data="# Add rules to this file to customize your agent's behavior, lines starting with # will be ignored.\n\n".encode(),
                    )
                    await self.room.storage.close(handle=handle)
                except RoomException:
                    pass

            return rules

        async def get_rules(self, *, context):
            rules = await super().get_rules(context=context)

            if room_rules_path is not None:
                for path in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(
                            path=path,
                            participant=context.caller,
                        )
                    )

            return rules

        async def init_chat_context(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_chat_context()
            await init_context_from_spec(context)
            return context

    return CustomCodexTaskRunner


def build_codex_worker(
    *,
    queue: str,
    model: str,
    title: Optional[str],
    description: Optional[str],
    supports_context: bool,
    prompt: Optional[str],
    rule: list[str],
    toolkit: list[str],
    schema: list[str],
    rules_file: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    skill_dirs: Optional[list[str]] = None,
    command: Optional[str] = None,
    ws_url: Optional[str] = None,
    codex_image: Optional[str] = None,
    codex_mounts: Optional[ContainerMountSpec] = None,
    working_dir: Optional[str] = None,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[str] = None,
    app_server_env: Optional[dict[str, str]] = None,
    toolkit_name: Optional[str] = None,
    verbose: bool = False,
):
    try:
        from meshagent.codex import CodexWorker
    except ImportError as exc:
        raise typer.BadParameter(
            "meshagent-codex is required for this command. Install the package and try again."
        ) from exc

    requirements = []
    for toolkit_name_value in toolkit:
        requirements.append(RequiredToolkit(name=toolkit_name_value))
    for schema_name in schema:
        requirements.append(RequiredSchema(name=schema_name))

    _load_rules(
        rule=rule,
        rules_file=rules_file,
    )

    class CustomCodexWorker(CodexWorker):
        def __init__(self):
            super().__init__(
                queue=queue,
                title=title,
                description=description,
                requires=requirements,
                rules=rule if len(rule) > 0 else None,
                toolkit_name=toolkit_name,
                skill_dirs=skill_dirs if len(skill_dirs or []) > 0 else None,
                supports_context=supports_context,
                model=model,
                command=command,
                ws_url=ws_url,
                image=codex_image,
                mounts=codex_mounts,
                cwd=working_dir,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                app_server_env=app_server_env,
                verbose=verbose,
            )

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_path is not None:
                for path in room_rules_path:
                    await self._load_room_rules(path=path)

        async def _load_room_rules(self, *, path: str) -> list[str]:
            rules = []
            try:
                room_rules = await self.room.storage.download(path=path)
                rules_txt = room_rules.data.decode()
                rules_config = RulesConfig.parse(rules_txt)
                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)
            except RoomException:
                try:
                    handle = await self.room.storage.open(path=path, overwrite=False)
                    await self.room.storage.write(
                        handle=handle,
                        data=(
                            "# Add rules to this file to customize your worker's behavior. "
                            "Lines starting with # will be ignored.\n\n"
                        ).encode(),
                    )
                    await self.room.storage.close(handle=handle)
                except RoomException:
                    pass

            return rules

        async def get_rules(self):
            rules = [*await super().get_rules()]
            if room_rules_path is not None:
                for path in room_rules_path:
                    rules.extend(await self._load_room_rules(path=path))
            return rules

        def get_prompt_for_message(self, *, message: dict) -> str:
            if prompt:
                return prompt
            return super().get_prompt_for_message(message=message)

        async def init_chat_context(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_chat_context()
            await init_context_from_spec(context)
            return context

    return CustomCodexWorker


@chatbot_app.async_command("join", help="Join a room and run a Codex chatbot agent.")
async def chatbot_join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )

    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        command, ws_url, app_server_env = _resolve_codex_runtime(
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            room_name=room_name,
            jwt=jwt,
            delegate_shell_token=delegate_shell_token,
            copy_env=copy_env,
            set_env=set_env,
        )

        CustomCodexChatBot = build_codex_chatbot(
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        )
        bot = CustomCodexChatBot()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room_name),
                    token=jwt,
                )
            ) as client:
                await bot.start(room=client)
                try:
                    print(
                        f"[bold green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/bold green]",
                        flush=True,
                    )
                    await client.protocol.wait_for_close()
                except KeyboardInterrupt:
                    await bot.stop()
    finally:
        await account_client.close()


@task_runner_app.async_command(
    "join", help="Join a room and run a Codex task-runner agent."
)
async def task_runner_join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the task runner")
    ] = None,
    supports_tools: Annotated[
        bool, typer.Option(..., help="Whether the task runner supports tools")
    ] = True,
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )

    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        command, ws_url, app_server_env = _resolve_codex_runtime(
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            room_name=room_name,
            jwt=jwt,
            delegate_shell_token=delegate_shell_token,
            copy_env=copy_env,
            set_env=set_env,
        )

        CustomCodexTaskRunner = build_codex_task_runner(
            title=title,
            description=description,
            supports_tools=supports_tools,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        )
        bot = CustomCodexTaskRunner()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room_name),
                    token=jwt,
                )
            ) as client:
                await bot.start(room=client)
                try:
                    print(
                        f"[bold green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/bold green]",
                        flush=True,
                    )
                    await client.protocol.wait_for_close()
                except KeyboardInterrupt:
                    await bot.stop()
    finally:
        await account_client.close()


@chatbot_app.async_command("service")
async def chatbot_service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the chatbot")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the chatbot")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_chatbot(
            model=model,
            title=title,
            description=description,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    if not get_deferred():
        await run_services()


@chatbot_app.async_command(
    "spec", help="Generate a service spec for deploying a Codex chatbot."
)
async def chatbot_spec(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the chatbot")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the chatbot")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_chatbot(
            model=model,
            title=title,
            description=description,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        ["meshagent", "codex", "chatbot", "service", *cleanup_args(sys.argv[3:])]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@chatbot_app.async_command(
    "deploy", help="Deploy a Codex chatbot service to a project or room."
)
async def chatbot_deploy(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the chatbot")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the chatbot")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    project_id = await resolve_project_id(project_id=project_id)

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_chatbot(
            model=model,
            title=title,
            description=description,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        ["meshagent", "codex", "chatbot", "service", *cleanup_args(sys.argv[3:])]
    )

    try:
        service_id = await _upsert_service(
            project_id=project_id,
            room=room,
            service_spec=spec,
        )
    except ConflictError:
        print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
        raise typer.Exit(code=1)
    else:
        print(f"[green]Deployed service:[/] {service_id}")


@worker_app.async_command("join", help="Join a room and run a Codex worker agent.")
async def worker_join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    queue: Annotated[str, typer.Option(..., help="The queue to consume")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the worker")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the worker")
    ] = None,
    supports_context: Annotated[
        bool, typer.Option(..., help="Whether to merge caller context messages")
    ] = True,
    prompt: Annotated[
        Optional[str], typer.Option(..., help="Override prompt used for each message")
    ] = None,
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="Optional toolkit name to expose worker operations"),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )

    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        command, ws_url, app_server_env = _resolve_codex_runtime(
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            room_name=room_name,
            jwt=jwt,
            delegate_shell_token=delegate_shell_token,
            copy_env=copy_env,
            set_env=set_env,
        )

        CustomCodexWorker = build_codex_worker(
            queue=queue,
            title=title,
            description=description,
            supports_context=supports_context,
            prompt=prompt,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            toolkit_name=toolkit_name,
            verbose=verbose,
        )
        bot = CustomCodexWorker()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room_name),
                    token=jwt,
                )
            ) as client:
                await bot.start(room=client)
                try:
                    print(
                        f"[bold green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/bold green]",
                        flush=True,
                    )
                    await client.protocol.wait_for_close()
                except KeyboardInterrupt:
                    await bot.stop()
    finally:
        await account_client.close()


@worker_app.async_command("service")
async def worker_service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    queue: Annotated[str, typer.Option(..., help="The queue to consume")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the worker")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the worker")
    ] = None,
    supports_context: Annotated[
        bool, typer.Option(..., help="Whether to merge caller context messages")
    ] = True,
    prompt: Annotated[
        Optional[str], typer.Option(..., help="Override prompt used for each message")
    ] = None,
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="Optional toolkit name to expose worker operations"),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_worker(
            queue=queue,
            title=title,
            description=description,
            supports_context=supports_context,
            prompt=prompt,
            toolkit_name=toolkit_name,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    if not get_deferred():
        await run_services()


@worker_app.async_command(
    "spec", help="Generate a service spec for deploying a Codex worker."
)
async def worker_spec(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    queue: Annotated[str, typer.Option(..., help="The queue to consume")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the worker")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the worker")
    ] = None,
    supports_context: Annotated[
        bool, typer.Option(..., help="Whether to merge caller context messages")
    ] = True,
    prompt: Annotated[
        Optional[str], typer.Option(..., help="Override prompt used for each message")
    ] = None,
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="Optional toolkit name to expose worker operations"),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_worker(
            queue=queue,
            title=title,
            description=description,
            supports_context=supports_context,
            prompt=prompt,
            toolkit_name=toolkit_name,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "codex",
            "worker",
            "service",
            *cleanup_args(sys.argv[3:]),
        ]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@worker_app.async_command(
    "deploy", help="Deploy a Codex worker service to a project or room."
)
async def worker_deploy(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    queue: Annotated[str, typer.Option(..., help="The queue to consume")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the worker")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the worker")
    ] = None,
    supports_context: Annotated[
        bool, typer.Option(..., help="Whether to merge caller context messages")
    ] = True,
    prompt: Annotated[
        Optional[str], typer.Option(..., help="Override prompt used for each message")
    ] = None,
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="Optional toolkit name to expose worker operations"),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    project_id = await resolve_project_id(project_id=project_id)

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_worker(
            queue=queue,
            title=title,
            description=description,
            supports_context=supports_context,
            prompt=prompt,
            toolkit_name=toolkit_name,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "codex",
            "worker",
            "service",
            *cleanup_args(sys.argv[3:]),
        ]
    )

    try:
        service_id = await _upsert_service(
            project_id=project_id,
            room=room,
            service_spec=spec,
        )
    except ConflictError:
        print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
        raise typer.Exit(code=1)
    else:
        print(f"[green]Deployed service:[/] {service_id}")


@chatbot_app.async_command(
    "run", help="Join a room, start a Codex chatbot, and wait for messages."
)
async def chatbot_run(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the chatbot")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the chatbot")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
    thread_path: Annotated[
        Optional[str],
        typer.Option(..., help="Thread path to use for chat history"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="Optional one-shot message"),
    ] = None,
    use_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the web search tool"),
    ] = None,
    use_image_gen: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the image generation tool"),
    ] = None,
    use_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the storage tool"),
    ] = None,
):
    from meshagent.cli.chatbot import chat_with

    if not verbose:
        root = logging.getLogger()
        root.setLevel(logging.ERROR)

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        command, ws_url, app_server_env = _resolve_codex_runtime(
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            room_name=room_name,
            jwt=jwt,
            delegate_shell_token=delegate_shell_token,
            copy_env=copy_env,
            set_env=set_env,
        )

        CustomCodexChatBot = build_codex_chatbot(
            model=model,
            title=title,
            description=description,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        )
        bot = CustomCodexChatBot()

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=jwt,
            )
        ) as client:
            await bot.start(room=client)

            _, pending = await asyncio.wait(
                [
                    asyncio.create_task(client.protocol.wait_for_close()),
                    asyncio.create_task(
                        chat_with(
                            participant_name=client.local_participant.get_attribute(
                                "name"
                            ),
                            room=room_name,
                            project_id=project_id,
                            thread_path=thread_path,
                            message=message,
                            use_web_search=bool(use_web_search),
                            use_image_gen=bool(use_image_gen),
                            use_storage=bool(use_storage),
                        )
                    ),
                ],
                return_when="FIRST_COMPLETED",
            )

            for task in pending:
                task.cancel()
    finally:
        await account_client.close()


@chatbot_app.async_command(
    "use",
    help="Send a one-shot or interactive message to a running Codex chatbot.",
)
async def chatbot_use(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    thread_path: Annotated[
        Optional[str],
        typer.Option(..., help="Thread path to use for chat history"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="Optional one-shot message"),
    ] = None,
    use_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the web search tool"),
    ] = None,
    use_image_gen: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the image generation tool"),
    ] = None,
    use_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Request the storage tool"),
    ] = None,
):
    from meshagent.cli.chatbot import chat_with

    project_id = await resolve_project_id(project_id=project_id)
    room_name = resolve_room(room)

    await chat_with(
        participant_name=agent_name,
        project_id=project_id,
        room=room_name,
        thread_path=thread_path,
        message=message,
        use_web_search=bool(use_web_search),
        use_image_gen=bool(use_image_gen),
        use_storage=bool(use_storage),
    )


@task_runner_app.async_command(
    "run", help="Join a room, start a Codex task-runner, and wait for tasks."
)
async def task_runner_run(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: Annotated[str, typer.Option(..., help="Role to use for the agent token")] = (
        "agent"
    ),
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to run")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the task runner")
    ] = None,
    supports_tools: Annotated[
        bool, typer.Option(..., help="Whether the task runner supports tools")
    ] = True,
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    key: Annotated[
        Optional[str], typer.Option("--key", help="An api key to sign the token with")
    ] = None,
    input: Annotated[
        Optional[str],
        typer.Option(..., help="JSON input for the task runner, or '-' for stdin"),
    ] = None,
    with_caller: Annotated[
        bool,
        typer.Option(
            help="Whether to invoke the tool with the currently logged in user as the caller"
        ),
    ] = os.getenv("MESHAGENT_TOKEN") is None,
):
    if not verbose:
        root = logging.getLogger()
        root.setLevel(logging.ERROR)

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(name=agent_name)
            token.add_api_grant(ApiScope.agent_default())
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)
            jwt = token.to_jwt(api_key=key)

        command, ws_url, app_server_env = _resolve_codex_runtime(
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            room_name=room_name,
            jwt=jwt,
            delegate_shell_token=delegate_shell_token,
            copy_env=copy_env,
            set_env=set_env,
        )

        CustomCodexTaskRunner = build_codex_task_runner(
            title=title,
            description=description,
            supports_tools=supports_tools,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        )
        bot = CustomCodexTaskRunner()

        input_payload = _read_task_runner_input(input)
        arguments = json.loads(input_payload)

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room_name),
                token=jwt,
            )
        ) as client:
            if with_caller:
                connection = await account_client.connect_room(
                    project_id=project_id, room=room_name
                )
                async with RoomClient(
                    protocol=WebSocketClientProtocol(
                        url=websocket_room_url(room_name=room_name),
                        token=connection.jwt,
                    ),
                ) as user_client:
                    result = await bot.run(
                        room=client,
                        arguments=arguments,
                        attachment=None,
                        caller=user_client.local_participant,
                    )
            else:
                result = await bot.run(
                    room=client,
                    arguments=arguments,
                    attachment=None,
                )

            if isinstance(result, JsonResponse):
                print(result.json)
            elif isinstance(result, TextResponse):
                print(result.text)
            else:
                print(result)
    finally:
        await account_client.close()


@task_runner_app.async_command("service")
async def task_runner_service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the task runner")
    ] = None,
    supports_tools: Annotated[
        bool, typer.Option(..., help="Whether the task runner supports tools")
    ] = True,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "TaskRunner"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_task_runner(
            title=title,
            description=description,
            supports_tools=supports_tools,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    if not get_deferred():
        await run_services()


@task_runner_app.async_command(
    "spec", help="Generate a service spec for deploying a Codex task-runner."
)
async def task_runner_spec(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the task runner")
    ] = None,
    supports_tools: Annotated[
        bool, typer.Option(..., help="Whether the task runner supports tools")
    ] = True,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "TaskRunner"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_task_runner(
            title=title,
            description=description,
            supports_tools=supports_tools,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "codex",
            "task-runner",
            "service",
            *cleanup_args(sys.argv[3:]),
        ]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@task_runner_app.async_command(
    "deploy", help="Deploy a Codex task-runner service to a project or room."
)
async def task_runner_deploy(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="Service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="Service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="A display name for service"),
    ] = None,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to run")],
    title: Annotated[
        Optional[str], typer.Option(..., help="A friendly title for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="A description for the task runner")
    ] = None,
    supports_tools: Annotated[
        bool, typer.Option(..., help="Whether the task runner supports tools")
    ] = True,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="A system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="A path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="The name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="The name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="The name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="The name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Codex model to use")
    ] = "gpt-5.2-codex",
    command: Annotated[
        Optional[str], typer.Option(..., help="Command used to launch codex app-server")
    ] = None,
    ws_url: Annotated[
        Optional[str],
        typer.Option(..., help="Websocket URL for an existing codex app-server"),
    ] = None,
    codex_image: Annotated[
        str,
        typer.Option(
            "--codex-image",
            help="Container image used to run codex app-server; use 'none' to disable",
        ),
    ] = "meshagent/shell-codex:default",
    mount_room_path: Annotated[
        list[str],
        typer.Option(
            "--mount-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    mount_project_path: Annotated[
        list[str],
        typer.Option(
            "--mount-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    working_dir: Annotated[
        Optional[str],
        typer.Option(..., help="Working directory passed to codex app-server"),
    ] = None,
    approval_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex approval policy")
    ] = None,
    sandbox_policy: Annotated[
        Optional[str], typer.Option(..., help="Codex sandbox policy")
    ] = None,
    mode: Annotated[
        Optional[CodexMode],
        typer.Option(
            "--mode",
            help="Preset Codex mode: default | yolo | workspace-write | read-only",
        ),
    ] = None,
    skill_dir: Annotated[
        list[str], typer.Option(..., help="An agent skills directory")
    ] = [],
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Log codex app-server JSON-RPC requests/responses",
        ),
    ] = False,
    delegate_shell_token: DelegateShellTokenOption = False,
    copy_env: CopyEnvOption = [],
    set_env: SetEnvOption = [],
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
):
    del service_title

    project_id = await resolve_project_id(project_id=project_id)

    codex_image = _normalize_codex_image(codex_image)
    approval_policy, sandbox_policy = _resolve_codex_policies(
        mode=mode,
        codex_image=codex_image,
        ws_url=ws_url,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
    )
    codex_mounts = _parse_codex_mounts(
        mount_room_path=mount_room_path,
        mount_project_path=mount_project_path,
    )
    command, ws_url, app_server_env = _resolve_codex_runtime(
        command=command,
        ws_url=ws_url,
        codex_image=codex_image,
        delegate_shell_token=delegate_shell_token,
        copy_env=copy_env,
        set_env=set_env,
    )
    service = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "TaskRunner"})
    )
    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_codex_task_runner(
            title=title,
            description=description,
            supports_tools=supports_tools,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_path=room_rules,
            skill_dirs=skill_dir,
            command=command,
            ws_url=ws_url,
            codex_image=codex_image,
            codex_mounts=codex_mounts,
            working_dir=working_dir,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            app_server_env=app_server_env,
            verbose=verbose,
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }
    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "codex",
            "task-runner",
            "service",
            *cleanup_args(sys.argv[3:]),
        ]
    )

    try:
        service_id = await _upsert_service(
            project_id=project_id,
            room=room,
            service_spec=spec,
        )
    except ConflictError:
        print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
        raise typer.Exit(code=1)
    else:
        print(f"[green]Deployed service:[/] {service_id}")
