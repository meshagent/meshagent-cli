import typer
from rich import print
from typing import Annotated, Optional, List, Type, Literal
from pathlib import Path
import logging
import os

from meshagent.tools.storage import (
    StorageToolMount,
    StorageToolkitBuilder,
)

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    cleanup_args,
    cleanup_args_strip_options,
    get_client,
    parse_shell_tool_mounts,
    parse_memory_selector,
    parse_storage_tool_mounts,
    resolve_key,
    resolve_project_id,
    resolve_room,
)

from meshagent.api import (
    ParticipantToken,
    RoomClient,
    WebSocketClientProtocol,
    ApiScope,
    RequiredToolkit,
    RequiredSchema,
    RoomException,
)

from meshagent.api.helpers import websocket_room_url

from meshagent.agents.config import RulesConfig
from meshagent.tools import (
    Toolkit,
    WebFetchTool,
    WebFetchToolkitBuilder,
    ContainerShellTool,
    MemoriesToolkit,
)
from meshagent.tools.storage import StorageToolkit
from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.uuid import UUIDToolkit
from meshagent.tools.script import get_script_tools
from meshagent.openai import OpenAIResponsesAdapter
from meshagent.anthropic import (
    AnthropicOpenAIResponsesStreamAdapter,
    WebFetchTool as AnthropicWebFetchTool,
    WebFetchToolkitBuilder as AnthropicWebFetchToolkitBuilder,
    WebSearchTool as AnthropicWebSearchTool,
    WebSearchToolkitBuilder as AnthropicWebSearchToolkitBuilder,
)


# Your Worker base (the one you pasted) + adapters
from meshagent.agents.worker import Worker  # adjust import
from meshagent.agents.adapter import LLMAdapter  # adjust import

from meshagent.openai.tools.responses_adapter import (
    WebSearchToolkitBuilder,
    MCPToolkitBuilder,
    WebSearchTool,
    ShellConfig,
    LocalShellConfig,
    ApplyPatchConfig,
    ApplyPatchTool,
    ApplyPatchToolkitBuilder,
    ShellToolkitBuilder,
    ShellTool,
    LocalShellToolkitBuilder,
    LocalShellTool,
    ImageGenerationToolkitBuilder,
    ImageGenerationTool,
)

from meshagent.cli.host import get_service, run_services, get_deferred, service_specs
from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ContainerMountSpec,
)

import yaml

import shlex
import sys

from meshagent.api.client import ConflictError

logger = logging.getLogger("worker_cli")

app = async_typer.AsyncTyper(help="Join a worker agent to a room")

ShellCopyEnvOption = Annotated[
    list[str],
    typer.Option(
        "--shell-copy-env",
        help=(
            "Copy local env vars into shell tool env. "
            "Accepts comma-separated names and can be repeated."
        ),
    ),
]

ShellSetEnvOption = Annotated[
    list[str],
    typer.Option(
        "--shell-set-env",
        help=("Set env vars in shell tool env as NAME=VALUE. Can be repeated."),
    ),
]

WORKING_DIR_HELP = "The default working directory for shell commands"

WorkingDirOption = Annotated[
    Optional[str],
    typer.Option(
        "--working-dir",
        help=WORKING_DIR_HELP,
    ),
]

WorkingDirectoryAliasOption = Annotated[
    Optional[str],
    typer.Option(
        "--working-directory",
        help="Alias for --working-dir",
        hidden=True,
    ),
]

ThreadingMode = Literal["auto", "manual", "none"]

ThreadingModeOption = Annotated[
    ThreadingMode,
    typer.Option(
        "--threading-mode",
        help=(
            "Threading mode: none (no persistence), manual (input path), "
            "or auto (LLM-selected thread path)"
        ),
    ),
]

ThreadDirOption = Annotated[
    str,
    typer.Option(
        "--thread-dir",
        help="Thread directory for auto mode; thread path is <thread_dir>/<name>.thread",
    ),
]


def _copy_shell_env_vars(*, copy_env: Optional[list[str]]) -> dict[str, str]:
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
            raise typer.BadParameter(f"--shell-copy-env variable is not set: {name}")
        env[name] = value

    return env


def _set_shell_env_vars(*, set_env: Optional[list[str]]) -> dict[str, str]:
    if set_env is None:
        return {}

    env: dict[str, str] = {}
    for item in set_env:
        value = item.strip()
        if value == "":
            continue

        if "=" not in value:
            raise typer.BadParameter(
                f"--shell-set-env value must be NAME=VALUE, got: {item}"
            )

        name, assigned_value = value.split("=", 1)
        name = name.strip()
        if name == "":
            raise typer.BadParameter(
                f"--shell-set-env variable name cannot be empty: {item}"
            )

        env[name] = assigned_value

    return env


def _resolve_working_dir_option(
    *,
    working_dir: Optional[str],
    working_directory: Optional[str],
) -> Optional[str]:
    if (
        working_dir is not None
        and working_directory is not None
        and working_dir != working_directory
    ):
        raise typer.BadParameter(
            "Conflicting values for --working-dir and --working-directory"
        )
    return working_dir if working_dir is not None else working_directory


def build_worker(
    *,
    WorkerBase: Type[Worker],
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    queue: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    toolkits: Optional[list[Toolkit]] = None,
    rules_file: Optional[list[str]] = None,
    room_rules_paths: list[str] | None = None,
    # thread/tool controls (mirrors mailbot)
    image_generation: Optional[str] = None,
    local_shell: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    web_search: Optional[str] = None,
    web_fetch: Optional[str] = None,
    discover_script_tools: Optional[bool] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    storage_tool_mounts: Optional[list[StorageToolMount]] = None,
    shell_tool_mounts: Optional[ContainerMountSpec] = None,
    working_dir: Optional[str] = None,
    require_image_generation: Optional[str] = None,
    require_local_shell: bool = False,
    require_web_search: bool = False,
    require_web_fetch: bool = False,
    require_apply_patch: bool = False,
    require_shell: bool = False,
    require_storage: bool = False,
    require_read_only_storage: bool = False,
    require_time: bool = True,
    require_uuid: bool = False,
    use_memory: Optional[str] = None,
    memory_model: Optional[str] = None,
    database_namespace: Optional[list[str]] = None,
    require_table_read: list[str] | None = None,
    require_table_write: list[str] | None = None,
    require_computer_use: bool,
    toolkit_name: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    shell_image: Optional[str] = None,
    delegate_shell_token: Optional[bool] = None,
    shell_copy_env: Optional[list[str]] = None,
    shell_set_env: Optional[list[str]] = None,
    log_llm_requests: Optional[bool] = None,
    prompt: Optional[str] = None,
    threading_mode: ThreadingMode = "none",
    thread_dir: str = ".threads",
):
    """
    Returns a Worker subclass
    """

    requirements: list = []
    if require_table_read is None:
        require_table_read = []
    if require_table_write is None:
        require_table_write = []
    if toolkits is None:
        toolkits = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))
    for s in schema:
        requirements.append(RequiredSchema(name=s))

    # merge in rules file contents
    if rules_file is not None:
        for rules_path in rules_file:
            try:
                with open(Path(rules_path).resolve(), "r") as f:
                    rule.extend(f.read().splitlines())
            except FileNotFoundError:
                print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    is_claude_model = model.startswith("claude-")
    is_openai = not is_claude_model
    supports_openai_tools = not is_claude_model
    base_shell_env = _copy_shell_env_vars(copy_env=shell_copy_env)
    base_shell_env.update(_set_shell_env_vars(set_env=shell_set_env))
    memory_selection: Optional[tuple[str, Optional[list[str]]]] = None
    if use_memory is not None:
        memory_selection = parse_memory_selector(use_memory)

    if not supports_openai_tools:
        if image_generation or require_image_generation:
            print("image generation tool is only supported by openai models")
            raise typer.Exit(1)
        if local_shell or require_local_shell:
            print("local shell tool is only supported by openai models")
            raise typer.Exit(1)
        if apply_patch or require_apply_patch:
            print("apply patch tool is only supported by openai models")
            raise typer.Exit(1)
        if require_computer_use:
            print("computer use tool is currently only supported by openai models")
            raise typer.Exit(1)

    if require_computer_use:
        llm_adapter: LLMAdapter = OpenAIResponsesAdapter(
            model=model,
            response_options={
                "reasoning": {"summary": "concise"},
                "truncation": "auto",
            },
            log_requests=log_llm_requests,
        )
    else:
        if model.startswith("claude-"):
            llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                model=model,
                log_requests=log_llm_requests,
            )
        else:
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                log_requests=log_llm_requests,
            )

    class CustomWorker(WorkerBase):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                requires=requirements,
                toolkits=toolkits,
                queue=queue,
                title=title,
                description=description,
                rules=rule if len(rule) > 0 else None,
                toolkit_name=toolkit_name,
                skill_dirs=skill_dirs,
                threading_mode=threading_mode,
                thread_dir=thread_dir,
            )
            self._room_rules_paths = room_rules_paths or []
            self.shell_tool = None

        def get_prompt_for_message(self, *, message: dict) -> str:
            return prompt or super().get_prompt_for_message(message=message)

        async def init_session(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_session()
            await init_context_from_spec(context)

            return context

        async def start(self, *, room: RoomClient):
            print(
                "[bold green]Worker connected. It will consume queue messages.[/bold green]"
            )
            await super().start(room=room)

            env = dict(base_shell_env)
            if delegate_shell_token:
                env["MESHAGENT_TOKEN"] = self.room.protocol.token
                env["OPENAI_API_KEY"] = self.room.protocol.token
                env["ANTHROPIC_API_KEY"] = self.room.protocol.token

            if require_shell:
                if is_openai:
                    shell_kwargs = {
                        "working_dir": working_dir,
                        "config": ShellConfig(name="shell"),
                        "image": shell_image or "python:3.13",
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts
                    self.shell_tool = ShellTool(**shell_kwargs)
                else:
                    shell_kwargs = {
                        "image": shell_image or "python:3.13",
                        "name": "shell",
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts
                    self.shell_tool = ContainerShellTool(**shell_kwargs)

            if room_rules_paths is not None:
                for p in room_rules_paths:
                    await self._load_room_rules(path=p)

        async def get_rules(self):
            rules = [*await super().get_rules()]
            for p in self._room_rules_paths:
                rules.extend(await self._load_room_rules(path=p))
            return rules

        async def _load_room_rules(self, *, path: str):
            rules: list[str] = []
            try:
                room_rules = await self.room.storage.download(path=path)
                rules_txt = room_rules.data.decode()
                rules_config = RulesConfig.parse(rules_txt)
                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)

            except RoomException:
                # initialize rules file if missing (same behavior as mailbot)
                try:
                    logger.info("attempting to initialize rules file")
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

                logger.info(
                    f"unable to load rules from {path}, continuing with default rules"
                )
            return rules

        def get_toolkit_builders(self):
            providers = []

            if image_generation:
                providers.append(ImageGenerationToolkitBuilder())

            if apply_patch:
                providers.append(ApplyPatchToolkitBuilder())

            if local_shell:
                providers.append(
                    LocalShellToolkitBuilder(
                        working_dir=working_dir,
                    )
                )

            if shell:
                shell_builder_kwargs = {
                    "working_dir": working_dir,
                    "image": shell_image,
                    "env": base_shell_env or None,
                }
                if shell_tool_mounts is not None:
                    shell_builder_kwargs["mounts"] = shell_tool_mounts
                providers.append(ShellToolkitBuilder(**shell_builder_kwargs))

            if mcp:
                providers.append(MCPToolkitBuilder())

            if web_search:
                if is_claude_model:
                    providers.append(AnthropicWebSearchToolkitBuilder())
                else:
                    providers.append(WebSearchToolkitBuilder())

            if web_fetch:
                if is_claude_model:
                    providers.append(AnthropicWebFetchToolkitBuilder())
                else:
                    providers.append(WebFetchToolkitBuilder())

            if storage:
                providers.append(StorageToolkitBuilder(mounts=storage_tool_mounts))

            return providers

        async def get_message_toolkits(self, *, message: dict):
            """
            Optional hook if your WorkerBase supports thread contexts.
            If not, you can remove this; I left it to mirror mailbot's pattern.
            """
            toolkits_out = await super().get_message_toolkits(message=message)

            thread_toolkit = Toolkit(name="thread_toolkit", tools=[])

            if discover_script_tools:
                thread_toolkit.tools.extend(await get_script_tools(self.room))

            if require_local_shell:
                thread_toolkit.tools.append(
                    LocalShellTool(
                        working_dir=working_dir,
                        config=LocalShellConfig(name="local_shell"),
                    )
                )

            if self.shell_tool is not None:
                thread_toolkit.tools.append(self.shell_tool)

            if require_apply_patch:
                thread_toolkit.tools.append(
                    ApplyPatchTool(
                        config=ApplyPatchConfig(name="apply_patch"),
                    )
                )

            if require_image_generation is not None:
                thread_toolkit.tools.append(
                    ImageGenerationTool(
                        model=require_image_generation,
                        partial_images=3,
                    )
                )

            if require_web_search:
                if is_claude_model:
                    thread_toolkit.tools.append(AnthropicWebSearchTool())
                else:
                    thread_toolkit.tools.append(WebSearchTool())

            if require_web_fetch:
                if is_claude_model:
                    thread_toolkit.tools.append(AnthropicWebFetchTool())
                else:
                    thread_toolkit.tools.append(WebFetchTool())

            if require_storage:
                thread_toolkit.tools.extend(
                    StorageToolkit(mounts=storage_tool_mounts).tools
                )

            if require_read_only_storage:
                thread_toolkit.tools.extend(
                    StorageToolkit(read_only=True, mounts=storage_tool_mounts).tools
                )

            if len(require_table_read) > 0:
                thread_toolkit.tools.extend(
                    (
                        await DatabaseToolkitBuilder().make(
                            room=self.room,
                            model=model,
                            config=DatabaseToolkitConfig(
                                tables=require_table_read,
                                read_only=True,
                                namespace=database_namespace,
                            ),
                        )
                    ).tools
                )

            if len(require_table_write) > 0:
                thread_toolkit.tools.extend(
                    (
                        await DatabaseToolkitBuilder().make(
                            room=self.room,
                            model=model,
                            config=DatabaseToolkitConfig(
                                tables=require_table_write,
                                read_only=False,
                                namespace=database_namespace,
                            ),
                        )
                    ).tools
                )

            if require_time:
                thread_toolkit.tools.extend(DatetimeToolkit().tools)

            if require_uuid:
                thread_toolkit.tools.extend(UUIDToolkit().tools)

            if memory_selection is not None:
                memory_name, memory_namespace = memory_selection
                thread_toolkit.tools.extend(
                    MemoriesToolkit(
                        memory_name=memory_name,
                        namespace=memory_namespace,
                        llm_model=memory_model,
                    ).tools
                )

            if require_computer_use:
                from meshagent.computers.agent import ComputerToolkit

                computer_toolkit = ComputerToolkit(room=self.room, render_screen=None)

                toolkits_out.append(computer_toolkit)

            toolkits_out.append(thread_toolkit)
            return toolkits_out

    return CustomWorker


@app.async_command("join", help="Join a room and run a worker agent.")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the worker agent")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="the name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="the name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="the name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use")
    ] = "gpt-5.2",
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Require web fetch tool")
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    key: Annotated[
        str, typer.Option("--key", help="an api key to sign the token with")
    ] = None,
    queue: Annotated[str, typer.Option(..., help="the queue to consume")],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="optional toolkit name to expose worker operations"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="path(s) in room storage to load rules from",
        ),
    ] = [],
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_tool_project_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_read_only_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable read only storage toolkit")
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Use a specific database namespace (JSON list or dotted)"
        ),
    ] = None,
    require_table_read: Annotated[
        list[str], typer.Option(..., help="Enable table read tools for these tables")
    ] = [],
    require_table_write: Annotated[
        list[str], typer.Option(..., help="Enable table write tools for these tables")
    ] = [],
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
        ),
    ] = False,
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    prompt: Annotated[
        Optional[str],
        typer.Option(..., help="a prompt to use for the worker"),
    ] = None,
):
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
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
            token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))
            token.add_role_grant(role=role)
            token.add_room_grant(room_name)

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        # Plug in your specific worker implementation here:
        # from meshagent.agents.some_worker import SomeWorker
        # WorkerBase = SomeWorker
        from meshagent.agents.worker import Worker as WorkerBase  # default; replace

        storage_tool_mounts = parse_storage_tool_mounts(
            local_paths=storage_tool_local_path,
            room_paths=storage_tool_room_path,
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=shell_tool_room_path,
            project_paths=shell_tool_project_path,
            image_paths=shell_image_mount,
        )

        CustomWorker = build_worker(
            WorkerBase=WorkerBase,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_paths=room_rules,
            queue=queue,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_dir=working_dir,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            prompt=prompt,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
        )

        worker = CustomWorker()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((worker, jwt))
        else:
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room_name),
                    token=jwt,
                )
            ) as client:
                await worker.start(room=client)
                try:
                    await client.protocol.wait_for_close()
                except KeyboardInterrupt:
                    await worker.stop()

    finally:
        await account_client.close()


@app.async_command("service")
async def service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="the name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="the name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="the name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Require web fetch tool")
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    shell_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_tool_project_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    queue: Annotated[str, typer.Option(..., help="the queue to consume")],
    toolkit_name: Annotated[
        Optional[str], typer.Option(..., help="Toolkit name to expose (optional)")
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="Path(s) to rules files inside the room",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require storage toolkit")
    ] = False,
    require_read_only_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require read-only storage toolkit")
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Database namespace (e.g. foo::bar)"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Require table read tool for table (repeatable)"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Require table write tool for table (repeatable)"),
    ] = [],
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
        ),
    ] = False,
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    prompt: Annotated[
        Optional[str],
        typer.Option(..., help="a prompt to use for the worker"),
    ] = None,
):
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    service = get_service(host=host, port=port)
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=shell_tool_room_path,
        project_paths=shell_tool_project_path,
        image_paths=shell_image_mount,
    )

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    # Plug in your specific worker implementation here:
    from meshagent.agents.worker import (
        Worker as WorkerBase,
    )  # replace with your concrete worker class

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_paths=room_rules,
            queue=queue,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_dir=working_dir,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            prompt=prompt,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
        ),
    )

    if not get_deferred():
        await run_services()


@app.async_command("spec", help="Generate a service spec for deploying a worker.")
async def spec(
    *,
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="the name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="the name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="the name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Require web fetch tool")
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    shell_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_tool_project_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    queue: Annotated[str, typer.Option(..., help="the queue to consume")],
    toolkit_name: Annotated[
        Optional[str], typer.Option(..., help="Toolkit name to expose (optional)")
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="Path(s) to rules files inside the room",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require storage toolkit")
    ] = False,
    require_read_only_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require read-only storage toolkit")
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Database namespace (e.g. foo::bar)"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Require table read tool for table (repeatable)"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Require table write tool for table (repeatable)"),
    ] = [],
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
        ),
    ] = False,
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    prompt: Annotated[
        Optional[str],
        typer.Option(..., help="a prompt to use for the worker"),
    ] = None,
):
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    service = get_service(host=None, port=None)
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=shell_tool_room_path,
        project_paths=shell_tool_project_path,
        image_paths=shell_image_mount,
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    # Plug in your specific worker implementation here:
    from meshagent.agents.worker import (
        Worker as WorkerBase,
    )  # replace with your concrete worker class

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_paths=room_rules,
            queue=queue,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_dir=working_dir,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            prompt=prompt,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
        ),
    )

    spec = service_specs(token_identity=agent_name)[0]
    spec.ports = []
    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }

    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "worker",
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy", help="Deploy a worker service to a project or room.")
async def deploy(
    *,
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    require_toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    require_schema: Annotated[
        List[str],
        typer.Option(
            "--require-schema", "-rs", help="the name or url of a required schema"
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--toolkit", "-t", help="the name or url of a required toolkit", hidden=True
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option(
            "--schema", "-s", help="the name or url of a required schema", hidden=True
        ),
    ] = [],
    model: Annotated[
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Require web fetch tool")
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    shell_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-room-path",
            help="Mount room storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_tool_project_path: Annotated[
        List[str],
        typer.Option(
            "--shell-tool-project-path",
            help="Mount project storage as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    queue: Annotated[str, typer.Option(..., help="the queue to consume")],
    toolkit_name: Annotated[
        Optional[str], typer.Option(..., help="Toolkit name to expose (optional)")
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="Path(s) to rules files inside the room",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require storage toolkit")
    ] = False,
    require_read_only_storage: Annotated[
        Optional[bool], typer.Option(..., help="Require read-only storage toolkit")
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            ...,
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Database namespace (e.g. foo::bar)"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Require table read tool for table (repeatable)"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Require table write tool for table (repeatable)"),
    ] = [],
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
        ),
    ] = False,
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    prompt: Annotated[
        Optional[str],
        typer.Option(..., help="a prompt to use for the worker"),
    ] = None,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
):
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    project_id = await resolve_project_id(project_id=project_id)

    service = get_service(host=None, port=None)
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=shell_tool_room_path,
        project_paths=shell_tool_project_path,
        image_paths=shell_image_mount,
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    # Plug in your specific worker implementation here:
    from meshagent.agents.worker import (
        Worker as WorkerBase,
    )  # replace with your concrete worker class

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "Worker"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            room_rules_paths=room_rules,
            queue=queue,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_dir=working_dir,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            prompt=prompt,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
        ),
    )

    spec = service_specs(token_identity=agent_name)[0]
    spec.ports = []
    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }

    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "worker",
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
    )

    client = await get_client()
    try:
        id = None
        try:
            if id is None:
                if room is None:
                    services = await client.list_services(project_id=project_id)
                else:
                    services = await client.list_room_services(
                        project_id=project_id, room_name=room
                    )

                for s in services:
                    if s.metadata.name == spec.metadata.name:
                        id = s.id

            if id is None:
                if room is None:
                    id = await client.create_service(
                        project_id=project_id, service=spec
                    )
                else:
                    id = await client.create_room_service(
                        project_id=project_id, service=spec, room_name=room
                    )

            else:
                spec.id = id
                if room is None:
                    await client.update_service(
                        project_id=project_id, service_id=id, service=spec
                    )
                else:
                    await client.update_room_service(
                        project_id=project_id,
                        service_id=id,
                        service=spec,
                        room_name=room,
                    )

        except ConflictError:
            print(f"[red]Service name already in use: {spec.metadata.name}[/red]")
            raise typer.Exit(code=1)
        else:
            print(f"[green]Deployed service:[/] {id}")

    finally:
        await client.close()
