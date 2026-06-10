import typer
import json
import os
from rich import print
from typing import Annotated, Literal, Optional
from meshagent.tools import (
    Toolkit,
    WebFetchTool,
    ContainerShellTool,
    MemoriesToolkit,
)
from meshagent.tools.storage import (
    StorageToolMount,
)
from meshagent.tools.document_tools import (
    DocumentAuthoringToolkit,
    DocumentTypeAuthoringToolkit,
)
from meshagent.agents.config import RulesConfig
from meshagent.agents.widget_schema import widget_schema

from meshagent.cli.common_options import (
    AllowGotoUrlOption,
    ProjectIdOption,
    RoomOption,
    ShellEmptyDirMountLegacyOption,
    ShellEmptyDirMountOption,
    ShellProjectMountLegacyOption,
    ShellProjectMountOption,
    ShellRoomMountLegacyOption,
    ShellRoomMountOption,
    StartingUrlOption,
)
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
    ApiScope,
    RoomException,
    RemoteParticipant,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    cleanup_args,
    cleanup_args_strip_options,
    DEPRECATED_REQUIRE_OPTION_ALIASES,
    DEFAULT_DATASET_NAMESPACE,
    DUPLICATE_REQUIRE_OPTION_NAMES,
    get_client,
    merge_option_lists,
    mint_participant_token_for_cli,
    normalize_required_tool_options,
    parse_shell_tool_mounts,
    parse_memory_selector,
    parse_storage_tool_mounts,
    resolve_dataset_namespace,
    resolve_shell_image,
    resolve_project_id,
    resolve_room,
    strip_command_options,
    supports_openai_shell_tool,
    upload_room_bytes_stream,
)

from meshagent.openai import OpenAIResponsesAdapter
from meshagent.anthropic import (
    AnthropicOpenAIResponsesStreamAdapter,
    WebFetchTool as AnthropicWebFetchTool,
    WebSearchTool as AnthropicWebSearchTool,
)

from typing import List
from pathlib import Path

from meshagent.openai.tools.responses_adapter import (
    WebSearchTool,
    ApplyPatchTool,
    ShellTool,
    ImageGenerationTool,
)

from meshagent.api.messaging import JsonContent, TextContent


from meshagent.cli.host import get_service, run_services, get_deferred, service_specs
from meshagent.tools.dataset import make_dataset_toolkit
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.script import get_script_tools
from meshagent.tools.uuid import UUIDToolkit
from meshagent.agents.adapter import MessageStreamLLMAdapter
from meshagent.agents.context import TaskContext
from meshagent.agents.skills import to_prompt

from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ContainerMountSpec,
)
import logging
import os.path

import yaml
import shlex
import sys

from meshagent.api.client import ConflictError
from meshagent.api.http import new_client_session

logger = logging.getLogger("taskrunner")

app = async_typer.AsyncTyper(help="Join a taskrunner to a room")
app.add_deprecated_option_aliases(
    {**DEPRECATED_REQUIRE_OPTION_ALIASES, "--database-namespace": "--dataset-namespace"}
)


def _require_storage_tool_mounts(
    *,
    room: RoomClient,
    local_paths: list[str],
    room_paths: list[str],
    default_room_mount: bool,
) -> list[StorageToolMount]:
    mounts = parse_storage_tool_mounts(
        room=room,
        local_paths=local_paths,
        room_paths=room_paths,
        default_room_mount=default_room_mount,
    )
    if mounts is None:
        raise RuntimeError("storage toolkit requires at least one configured mount")
    return mounts


ThreadingMode = Literal["auto", "manual", "none"]

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

ThreadNameRuleOption = Annotated[
    list[str],
    typer.Option(
        "--thread-name-rule",
        help="Rule for generating auto thread names (repeatable)",
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


def read_task_runner_input(input_value: Optional[str]) -> str:
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


async def _load_remote_json(url: str) -> object:
    async with new_client_session() as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body}")
            return json.loads(await resp.read())


async def build_task_runner(
    *,
    client: RoomClient | None = None,
    api_key: str | None = None,
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    image_generation: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    web_search: Optional[str] = None,
    web_fetch: Optional[str] = None,
    discover_script_tools: Optional[bool] = None,
    storage: Optional[str] = None,
    storage_tool_local_paths: list[str] | None = None,
    storage_tool_room_paths: list[str] | None = None,
    default_room_storage_mount: bool = False,
    shell_tool_mounts: Optional[ContainerMountSpec] = None,
    allow_model_selection: bool = True,
    threading_mode: ThreadingMode = "none",
    thread_dir: str = ".threads",
    thread_name_rules: Optional[list[str]] = None,
    require_image_generation: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_web_search: Optional[str] = None,
    require_web_fetch: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    use_memory: Optional[str] = None,
    memory_model: Optional[str] = None,
    dataset_namespace: Optional[list[str]] = None,
    require_computer_use: bool = False,
    starting_url: Optional[str] = None,
    allow_goto_url: bool = False,
    rules_file: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    require_discovery: Optional[str] = None,
    require_document_authoring: Optional[str] = None,
    working_dir: Optional[str] = None,
    llm_participant: Optional[str] = None,
    output_schema_path: Optional[str] = None,
    output_schema_str: Optional[str] = None,
    annotations: list[dict[str, str]],
    title: Optional[str] = None,
    description: Optional[str] = None,
    shell_image: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    delegate_shell_token: Optional[bool] = None,
    shell_copy_env: Optional[list[str]] = None,
    shell_set_env: Optional[list[str]] = None,
    log_llm_requests: Optional[bool] = False,
):
    if storage_tool_local_paths is None:
        storage_tool_local_paths = []
    if storage_tool_room_paths is None:
        storage_tool_room_paths = []

    output_schema = None
    if output_schema_str is not None:
        output_schema = json.loads(output_schema_str)
    elif output_schema_path is not None:
        if output_schema_path.startswith("http://") or output_schema_path.startswith(
            "https://"
        ):
            output_schema = await _load_remote_json(output_schema_path)
        else:
            with open(Path(os.path.expanduser(output_schema_path)).resolve(), "r") as f:
                output_schema = json.loads(f.read())

    from meshagent.agents.llmrunner import LLMTaskRunner

    from meshagent.tools.storage import StorageToolkit

    requirements = []

    toolkits = []
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        schema=schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=None,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        storage=storage,
        require_storage=require_storage,
    )
    toolkit = normalized_tool_options["toolkit"]
    schema = normalized_tool_options["schema"]
    require_image_generation = normalized_tool_options["require_image_generation"]
    require_computer_use = normalized_tool_options["require_computer_use"]
    require_shell = normalized_tool_options["require_shell"]
    require_apply_patch = normalized_tool_options["require_apply_patch"]
    require_web_search = normalized_tool_options["require_web_search"]
    require_web_fetch = normalized_tool_options["require_web_fetch"]
    require_storage = normalized_tool_options["require_storage"]

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    client_rules = {}

    if rules_file is not None:
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

    is_claude_model = model.startswith("claude-")
    supports_openai_tools = llm_participant is None and not is_claude_model
    supports_openai_shell = supports_openai_shell_tool(
        model=model, llm_participant=llm_participant
    )
    base_shell_env = _copy_shell_env_vars(copy_env=shell_copy_env)
    base_shell_env.update(_set_shell_env_vars(set_env=shell_set_env))
    resolved_shell_image = resolve_shell_image(shell_image)
    memory_selection: Optional[tuple[str, Optional[list[str]]]] = None
    if use_memory is not None:
        memory_selection = parse_memory_selector(use_memory)

    if not supports_openai_tools:
        if image_generation or require_image_generation:
            print("image generation tool is only supported by openai models")
            raise typer.Exit(1)
        if apply_patch or require_apply_patch:
            print("apply patch tool is only supported by openai models")
            raise typer.Exit(1)
        if require_computer_use:
            print("computer use tool is currently only supported by openai models")
            raise typer.Exit(1)

    BaseClass = LLMTaskRunner
    if llm_participant:
        llm_adapter = MessageStreamLLMAdapter(
            participant_name=llm_participant,
        )
    else:
        if require_computer_use:
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                api_key=api_key,
                response_options={
                    "reasoning": {"summary": "concise"},
                },
                log_requests=log_llm_requests,
            )
        else:
            if model.startswith("claude-"):
                llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                    model=model,
                    api_key=api_key,
                    log_requests=log_llm_requests,
                )
            else:
                llm_adapter = OpenAIResponsesAdapter(
                    model=model,
                    api_key=api_key,
                    log_requests=log_llm_requests,
                )

    class CustomTaskRunner(BaseClass):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules,
                output_schema=output_schema,
                annotations=annotations,
                title=title,
                description=description,
                allow_model_selection=allow_model_selection,
                threading_mode=threading_mode,
                thread_dir=thread_dir,
                thread_name_rules=thread_name_rules,
            )
            self.shell_tool = None

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            env = dict(base_shell_env)
            if delegate_shell_token:
                env["MESHAGENT_TOKEN"] = self.room.protocol.token
                env["OPENAI_API_KEY"] = self.room.protocol.token
                env["ANTHROPIC_API_KEY"] = self.room.protocol.token

            if require_shell:
                if supports_openai_shell:
                    shell_kwargs = {
                        "working_dir": working_dir,
                        "name": "shell",
                        "image": resolved_shell_image,
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts
                    self.shell_tool = ShellTool(room=room, **shell_kwargs)
                else:
                    shell_kwargs = {
                        "image": resolved_shell_image,
                        "name": "shell",
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts
                    self.shell_tool = ContainerShellTool(room=room, **shell_kwargs)

            if room_rules_path is not None:
                for p in room_rules_path:
                    await self._load_room_rules(path=p)

        async def stop(self) -> None:
            try:
                pass
            finally:
                await super().stop()

        async def init_session(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_session()
            await init_context_from_spec(context)

            return context

        async def _load_room_rules(
            self,
            *,
            path: str,
            participant: Optional[RemoteParticipant] = None,
        ):
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
                        cr = rules_config.client_rules.get(client)
                        if cr is not None:
                            rules.extend(cr)

            except RoomException:
                try:
                    logger.info("attempting to initialize rules file")
                    await upload_room_bytes_stream(
                        room=self.room,
                        path=path,
                        data="# Add rules to this file to customize your agent's behavior, lines starting with # will be ignored.\n\n".encode(),
                        overwrite=False,
                    )

                except RoomException:
                    pass
                logger.info(
                    f"unable to load rules from {path}, continuing with default rules"
                )
                pass

            return rules

        def _get_skills_storage_toolkit(self) -> StorageToolkit | None:
            if require_storage:
                return StorageToolkit(
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    )
                )

            if require_read_only_storage:
                return StorageToolkit(
                    read_only=True,
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    ),
                )

            return None

        async def get_rules(self, *, context: TaskContext):
            rules = await super().get_rules(context=context)

            if skill_dirs is not None and len(skill_dirs) > 0:
                rules.append(
                    "You have access to to following skills which follow the agentskills spec:"
                )
                rules.append(
                    await to_prompt(
                        [*(Path(p) for p in skill_dirs)],
                        storage_toolkit=self._get_skills_storage_toolkit(),
                    )
                )
                rules.append(
                    "Use the shell or storage tool to find out more about skills and execute them when they are required"
                )

            if room_rules_path is not None:
                for p in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(path=p, participant=context.caller)
                    )

            logging.info(f"using rules {rules}")

            return rules

        async def get_context_toolkits(self, *, context: TaskContext):
            providers = []
            if require_image_generation:
                providers.append(
                    ImageGenerationTool(
                        model=require_image_generation,
                        partial_images=3,
                    )
                )

            if self.shell_tool is not None:
                providers.append(self.shell_tool)

            if require_apply_patch:
                providers.append(
                    ApplyPatchTool(
                        storage=StorageToolkit(
                            mounts=_require_storage_tool_mounts(
                                room=client or self.room,
                                local_paths=storage_tool_local_paths,
                                room_paths=storage_tool_room_paths,
                                default_room_mount=True,
                            )
                        )
                    )
                )

            if require_web_search:
                if is_claude_model:
                    providers.append(AnthropicWebSearchTool())
                else:
                    providers.append(WebSearchTool())

            if require_web_fetch:
                if is_claude_model:
                    providers.append(AnthropicWebFetchTool())
                else:
                    providers.append(WebFetchTool())

            if discover_script_tools:
                providers.extend(await get_script_tools(self.room))

            if require_storage:
                providers.extend(
                    StorageToolkit(
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        ),
                    ).tools
                )

            if len(require_table_read) > 0:
                providers.extend(
                    (
                        await make_dataset_toolkit(
                            room=self.room,
                            tables=require_table_read,
                            read_only=True,
                            namespace=dataset_namespace,
                        )
                    ).tools
                )

            if require_time:
                providers.extend(DatetimeToolkit().tools)

            if require_uuid:
                providers.extend((UUIDToolkit()).tools)

            if memory_selection is not None:
                memory_name, memory_namespace = memory_selection
                providers.extend(
                    MemoriesToolkit(
                        room=self.room,
                        memory_name=memory_name,
                        namespace=memory_namespace,
                        llm_model=memory_model,
                    ).tools
                )

            if len(require_table_write) > 0:
                providers.extend(
                    (
                        await make_dataset_toolkit(
                            room=self.room,
                            tables=require_table_write,
                            read_only=False,
                            namespace=dataset_namespace,
                        )
                    ).tools
                )

            if require_read_only_storage:
                providers.extend(
                    StorageToolkit(
                        read_only=True,
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        ),
                    ).tools
                )

            if require_document_authoring:
                providers.extend(DocumentAuthoringToolkit(room=self.room).tools)
                providers.extend(
                    DocumentTypeAuthoringToolkit(
                        schema=widget_schema,
                        document_type="widget",
                        room=self.room,
                    ).tools
                )

            if require_discovery:
                from meshagent.tools.discovery import DiscoveryToolkit

                providers.extend(DiscoveryToolkit(room=self.room).tools)

            tk = await super().get_context_toolkits(context=context)
            toolkits_out = [
                *(
                    [Toolkit(name="tools", tools=providers)]
                    if len(providers) > 0
                    else []
                ),
                *tk,
            ]
            if require_computer_use:
                from meshagent.computers.agent import ComputerToolkit

                toolkits_out.insert(
                    0,
                    ComputerToolkit(
                        room=self.room,
                        render_screen=None,
                        starting_url=starting_url,
                        include_goto_tool=allow_goto_url,
                    ),
                )
            return toolkits_out

    return CustomTaskRunner


@app.async_command("join", help="Join a room and run a task-runner agent.")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.5",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
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
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option("--time", help="Enable time/datetime tools"),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option("--uuid", help="Enable UUID generation tools"),
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
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Dataset namespace (e.g. foo::bar)"),
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            "--computer-use",
            help="Enable computer use",
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
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
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use"),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use"),
    ] = None,
    annotations: Annotated[
        str,
        typer.Option(
            "--annotations", "-a", help='annotations in json format {"name":"value"}'
        ),
    ] = '{"meshagent.task-runner.attachment-format":"tar"}',
    title: Annotated[
        Optional[str], typer.Option(..., help="a friendly name for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="a description for the task runner")
    ] = None,
    allow_model_selection: Annotated[
        Optional[bool], typer.Option(..., help="a description for the task runner")
    ] = True,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    thread_name_rule: ThreadNameRuleOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    resolved_dataset_namespace = resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    )
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

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

            jwt = await mint_participant_token_for_cli(
                project_id=project_id,
                name=agent_name,
                room_name=room,
                role=role,
                api_scope=ApiScope.agent_default(tunnels=require_computer_use),
                key=key,
            )

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        requirements = []

        for t in toolkit:
            requirements.append(RequiredToolkit(name=t))

        for t in schema:
            requirements.append(RequiredSchema(name=t))

        default_room_storage_mount = bool(
            storage or require_storage or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            project_paths=merge_option_lists(
                shell_project_mount,
                shell_tool_project_path,
            ),
            empty_dir_paths=merge_option_lists(
                shell_empty_dir_mount,
                shell_tool_empty_dir,
            ),
            image_paths=shell_image_mount,
        )
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )

        CustomTaskRunner = await build_task_runner(
            client=client,
            api_key=jwt,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_name_rules=thread_name_rule if len(thread_name_rule) > 0 else None,
            log_llm_requests=log_llm_requests,
            model=model,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            discover_script_tools=discover_script_tools,
            storage=storage,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_apply_patch=require_apply_patch,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_image_generation=require_image_generation,
            require_storage=require_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            dataset_namespace=resolved_dataset_namespace,
            require_computer_use=require_computer_use,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            working_dir=working_dir,
            shell_image=shell_image,
            skill_dirs=skill_dir,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
        )

        bot = CustomTaskRunner()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with client:
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


@app.async_command("run", help="Join a room, run the task-runner, and wait for tasks.")
async def run(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.5",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
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
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option("--time", help="Enable time/datetime tools"),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option("--uuid", help="Enable UUID generation tools"),
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
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Dataset namespace (e.g. foo::bar)"),
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            "--computer-use",
            help="Enable computer use",
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
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
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ...,
            help="Delegate LLM interactions to a remote participant",
        ),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use"),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use"),
    ] = None,
    annotations: Annotated[
        str,
        typer.Option(
            "--annotations", "-a", help='annotations in json format {"name":"value"}'
        ),
    ] = '{"meshagent.task-runner.attachment-format":"tar"}',
    title: Annotated[
        Optional[str], typer.Option(..., help="a friendly name for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="a description for the task runner")
    ] = None,
    allow_model_selection: Annotated[
        Optional[bool], typer.Option(..., help="a description for the task runner")
    ] = True,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    thread_name_rule: ThreadNameRuleOption = [],
    input: Annotated[
        Optional[str],
        typer.Option(..., help="json input for the task runner, or '-' for stdin"),
    ] = None,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Enable verbose logging and disable default log suppression",
        ),
    ] = False,
    with_caller: Annotated[
        bool,
        typer.Option(
            help="whether to invoke the tool with the currently logged in user as the caller"
        ),
    ] = os.getenv("MESHAGENT_TOKEN") is None,
):
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    if not verbose and not log_llm_requests:
        root = logging.getLogger()
        root.setLevel(logging.ERROR)

    resolved_dataset_namespace = resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    )
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            if agent_name is None:
                print(
                    "[bold red]--agent-name must be specified when the MESHAGENT_TOKEN environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            jwt = await mint_participant_token_for_cli(
                project_id=project_id,
                name=agent_name,
                room_name=room,
                role=role,
                api_scope=ApiScope.agent_default(tunnels=require_computer_use),
                key=key,
            )

        requirements = []

        for t in toolkit:
            requirements.append(RequiredToolkit(name=t))

        for t in schema:
            requirements.append(RequiredSchema(name=t))

        default_room_storage_mount = bool(
            storage or require_storage or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            project_paths=merge_option_lists(
                shell_project_mount,
                shell_tool_project_path,
            ),
            empty_dir_paths=merge_option_lists(
                shell_empty_dir_mount,
                shell_tool_empty_dir,
            ),
            image_paths=shell_image_mount,
        )
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )

        CustomTaskRunner = await build_task_runner(
            client=client,
            api_key=jwt,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_name_rules=thread_name_rule if len(thread_name_rule) > 0 else None,
            log_llm_requests=log_llm_requests,
            model=model,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            image_generation=image_generation,
            web_search=web_search,
            discover_script_tools=discover_script_tools,
            storage=storage,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_apply_patch=require_apply_patch,
            require_web_search=require_web_search,
            require_shell=require_shell,
            require_image_generation=require_image_generation,
            require_storage=require_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            dataset_namespace=resolved_dataset_namespace,
            require_computer_use=require_computer_use,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            working_dir=working_dir,
            shell_image=shell_image,
            skill_dirs=skill_dir,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
        )

        bot = CustomTaskRunner()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:
            async with client:
                try:
                    input_payload = read_task_runner_input(input)

                    if with_caller:
                        connection = await account_client.connect_room(
                            project_id=project_id, room=room
                        )
                        async with RoomClient(
                            protocol_factory=WebSocketClientProtocol(
                                url=websocket_room_url(room_name=room),
                                token=connection.jwt,
                            ).create_factory(),
                        ) as user_client:
                            result = await bot.run(
                                room=client,
                                arguments=json.loads(input_payload),
                                attachment=None,
                                caller=user_client.local_participant,
                            )

                    else:
                        result = await bot.run(
                            room=client,
                            arguments=json.loads(input_payload),
                            attachment=None,
                        )

                    if isinstance(result, JsonContent):
                        print(result.json)
                    elif isinstance(result, TextContent):
                        print(result.text)
                    else:
                        print(result)

                except KeyboardInterrupt:
                    await bot.stop()

    finally:
        await account_client.close()


@app.async_command("service")
async def service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.5",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
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
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option("--time", help="Enable time/datetime tools"),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option("--uuid", help="Enable UUID generation tools"),
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
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Dataset namespace (e.g. foo::bar)"),
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            "--computer-use",
            help="Enable computer use",
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
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
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use"),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use"),
    ] = None,
    annotations: Annotated[
        str,
        typer.Option(
            "--annotations", "-a", help='annotations in json format {"name":"value"}'
        ),
    ] = '{"meshagent.task-runner.attachment-format":"tar"}',
    title: Annotated[
        Optional[str], typer.Option(..., help="a friendly name for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="a description for the task runner")
    ] = None,
    allow_model_selection: Annotated[
        Optional[bool], typer.Option(..., help="a description for the task runner")
    ] = True,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    thread_name_rule: ThreadNameRuleOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    print("[bold green]Connecting to room...[/bold green]", flush=True)
    resolved_dataset_namespace = resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    )

    default_room_storage_mount = bool(
        storage or require_storage or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
        image_paths=shell_image_mount,
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
        cls=await build_task_runner(
            client=None,
            model=model,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_name_rules=thread_name_rule if len(thread_name_rule) > 0 else None,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            image_generation=image_generation,
            storage=storage,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_image_generation=require_image_generation,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            dataset_namespace=resolved_dataset_namespace,
            require_computer_use=require_computer_use,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            working_dir=working_dir,
            shell_image=shell_image,
            skill_dirs=skill_dir,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
        ),
    )

    if not get_deferred():
        await run_services()


@app.async_command("spec", help="Generate a service spec for deploying a task-runner.")
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
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.5",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
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
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option("--time", help="Enable time/datetime tools"),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option("--uuid", help="Enable UUID generation tools"),
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
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Dataset namespace (e.g. foo::bar)"),
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            "--computer-use",
            help="Enable computer use",
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
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
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use"),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use"),
    ] = None,
    annotations: Annotated[
        str,
        typer.Option(
            "--annotations", "-a", help='annotations in json format {"name":"value"}'
        ),
    ] = '{"meshagent.task-runner.attachment-format":"tar"}',
    title: Annotated[
        Optional[str], typer.Option(..., help="a friendly name for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="a description for the task runner")
    ] = None,
    allow_model_selection: Annotated[
        Optional[bool], typer.Option(..., help="a description for the task runner")
    ] = True,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    thread_name_rule: ThreadNameRuleOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    resolved_dataset_namespace = resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    )
    default_room_storage_mount = bool(
        storage or require_storage or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
        image_paths=shell_image_mount,
    )

    service = get_service(host=None, port=None)
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
        cls=await build_task_runner(
            client=None,
            model=model,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_name_rules=thread_name_rule if len(thread_name_rule) > 0 else None,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            image_generation=image_generation,
            storage=storage,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_image_generation=require_image_generation,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            dataset_namespace=resolved_dataset_namespace,
            require_computer_use=require_computer_use,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            working_dir=working_dir,
            shell_image=shell_image,
            skill_dirs=skill_dir,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
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
            "task-runner",
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy", help="Deploy a task-runner service to a project or room.")
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
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    toolkit: Annotated[
        List[str],
        typer.Option(
            "--require-toolkit", "-rt", help="the name or url of a required toolkit"
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.5",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
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
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option("--time", help="Enable time/datetime tools"),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option("--uuid", help="Enable UUID generation tools"),
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
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Dataset namespace (e.g. foo::bar)"),
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            "--computer-use",
            help="Enable computer use",
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
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
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use"),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use"),
    ] = None,
    annotations: Annotated[
        str,
        typer.Option(
            "--annotations", "-a", help='annotations in json format {"name":"value"}'
        ),
    ] = '{"meshagent.task-runner.attachment-format":"tar"}',
    title: Annotated[
        Optional[str], typer.Option(..., help="a friendly name for the task runner")
    ] = None,
    description: Annotated[
        Optional[str], typer.Option(..., help="a description for the task runner")
    ] = None,
    allow_model_selection: Annotated[
        Optional[bool], typer.Option(..., help="a description for the task runner")
    ] = True,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = ".threads",
    thread_name_rule: ThreadNameRuleOption = [],
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    project_id = await resolve_project_id(project_id=project_id)
    resolved_dataset_namespace = resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=DEFAULT_DATASET_NAMESPACE,
    )

    default_room_storage_mount = bool(
        storage or require_storage or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
    )

    service = get_service(host=None, port=None)
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
        cls=await build_task_runner(
            client=None,
            model=model,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_name_rules=thread_name_rule if len(thread_name_rule) > 0 else None,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            image_generation=image_generation,
            storage=storage,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_image_generation=require_image_generation,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            dataset_namespace=resolved_dataset_namespace,
            require_computer_use=require_computer_use,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            working_dir=working_dir,
            shell_image=shell_image,
            skill_dirs=skill_dir,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
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
            "task-runner",
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


_REMOVED_TOOLKIT_OPTION_NAMES = DUPLICATE_REQUIRE_OPTION_NAMES

strip_command_options(app, option_names=_REMOVED_TOOLKIT_OPTION_NAMES)
