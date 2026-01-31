import typer
import json
import os
from rich import print
from typing import Annotated, Optional
from meshagent.tools import Toolkit
from meshagent.tools.storage import (
    StorageToolMount,
    StorageToolkitBuilder,
)
from meshagent.tools.document_tools import (
    DocumentAuthoringToolkit,
    DocumentTypeAuthoringToolkit,
)
from meshagent.agents.config import RulesConfig
from meshagent.agents.widget_schema import widget_schema

from meshagent.cli.common_options import (
    ProjectIdOption,
    RoomOption,
)
from meshagent.api import (
    RoomClient,
    WebSocketClientProtocol,
    ParticipantToken,
    ApiScope,
    RoomException,
    RemoteParticipant,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    cleanup_args,
    get_client,
    parse_storage_tool_mounts,
    resolve_key,
    resolve_project_id,
    resolve_room,
)

from meshagent.openai import OpenAIResponsesAdapter
from meshagent.anthropic import AnthropicOpenAIResponsesStreamAdapter

from typing import List
from pathlib import Path

from meshagent.openai.tools.responses_adapter import (
    WebSearchToolkitBuilder,
    MCPToolkitBuilder,
    WebSearchTool,
    LocalShellConfig,
    ShellConfig,
    WebSearchConfig,
    ApplyPatchConfig,
    ApplyPatchTool,
    ApplyPatchToolkitBuilder,
    ShellToolkitBuilder,
    ShellTool,
    LocalShellToolkitBuilder,
    LocalShellTool,
    ImageGenerationConfig,
    ImageGenerationToolkitBuilder,
    ImageGenerationTool,
)

from meshagent.api.messaging import JsonResponse, TextResponse


from meshagent.cli.host import get_service, run_services, get_deferred, service_specs
from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.tools.script import get_script_tools
from meshagent.agents.adapter import MessageStreamLLMAdapter
from meshagent.agents.context import TaskContext

from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.specs.service import AgentSpec, ANNOTATION_AGENT_TYPE
import logging
import os.path

from urllib.request import urlopen

import yaml
import shlex
import sys

from meshagent.api.client import ConflictError

logger = logging.getLogger("taskrunner")

app = async_typer.AsyncTyper(help="Join a taskrunner to a room")


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


def build_task_runner(
    *,
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    image_generation: Optional[str] = None,
    local_shell: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    web_search: Optional[str] = None,
    discover_script_tools: Optional[bool] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    storage_tool_mounts: Optional[list[StorageToolMount]] = None,
    allow_model_selection: bool = True,
    allow_thread_selection: bool = False,
    require_image_generation: Optional[str] = None,
    require_local_shell: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_web_search: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    rules_file: Optional[str] = None,
    room_rules_path: Optional[list[str]] = None,
    require_discovery: Optional[str] = None,
    require_document_authoring: Optional[str] = None,
    working_directory: Optional[str] = None,
    llm_participant: Optional[str] = None,
    output_schema_path: Optional[str] = None,
    output_schema_str: Optional[str] = None,
    annotations: list[dict[str, str]],
    title: Optional[str] = None,
    description: Optional[str] = None,
    shell_image: Optional[str] = None,
    delegate_shell_token: Optional[bool] = None,
    log_llm_requests: Optional[bool] = False,
):
    output_schema = None
    if output_schema_str is not None:
        output_schema = json.loads(output_schema_str)
    elif output_schema_path is not None:
        if output_schema_path.startswith("http://") or output_schema_path.startswith(
            "https://"
        ):
            with urlopen(output_schema_path) as r:
                output_schema = json.loads(r.read())
        else:
            with open(Path(os.path.expanduser(rules_file)).resolve(), "r") as f:
                output_schema = json.loads(f.read())

    from meshagent.agents.llmrunner import LLMTaskRunner

    from meshagent.tools.storage import StorageToolkit

    requirements = []

    toolkits = []
    storage_enabled = bool(storage) or storage_tool_mounts is not None

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    client_rules = {}

    if rules_file is not None:
        try:
            with open(Path(os.path.expanduser(rules_file)).resolve(), "r") as f:
                rules_config = RulesConfig.parse(f.read())
                rule.extend(rules_config.rules)
                client_rules = rules_config.client_rules

        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

    BaseClass = LLMTaskRunner
    if llm_participant:
        llm_adapter = MessageStreamLLMAdapter(
            participant_name=llm_participant,
        )
    else:
        if model.startswith("claude-"):
            llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                model=model, log_requests=log_llm_requests
            )
        else:
            llm_adapter = OpenAIResponsesAdapter(
                model=model, log_requests=log_llm_requests
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
                input_path=allow_thread_selection,
            )

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_path is not None:
                for p in room_rules_path:
                    await self._load_room_rules(path=p)

        async def init_chat_context(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_chat_context()
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
                    handle = await self.room.storage.open(path=path, overwrite=False)
                    await self.room.storage.write(
                        handle=handle,
                        data="# Add rules to this file to customize your agent's behavior, lines starting with # will be ignored.\n\n".encode(),
                    )
                    await self.room.storage.close(handle=handle)

                except RoomException:
                    pass
                logger.info(
                    f"unable to load rules from {path}, continuing with default rules"
                )
                pass

            return rules

        async def get_rules(self, *, context: TaskContext):
            rules = await super().get_rules(context=context)

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
                        config=ImageGenerationConfig(
                            name="image_generation",
                            partial_images=3,
                        ),
                    )
                )

            if require_local_shell:
                providers.append(
                    LocalShellTool(
                        working_directory=working_directory,
                        config=LocalShellConfig(name="local_shell"),
                    )
                )

            env = {}
            if delegate_shell_token:
                env["MESHAGENT_TOKEN"] = self.room.protocol.token

            if require_shell:
                providers.append(
                    ShellTool(
                        working_directory=working_directory,
                        config=ShellConfig(name="shell"),
                        image=shell_image or "python:3.13",
                        env=env,
                    )
                )

            if require_apply_patch:
                providers.append(
                    ApplyPatchTool(
                        config=ApplyPatchConfig(name="apply_patch"),
                    )
                )

            if require_mcp:
                raise Exception(
                    "mcp tool cannot be required by cli currently, use 'optional' instead"
                )

            if require_web_search:
                providers.append(
                    WebSearchTool(config=WebSearchConfig(name="web_search"))
                )

            if discover_script_tools:
                providers.extend(await get_script_tools(self.room))

            if require_storage:
                providers.extend(StorageToolkit(mounts=storage_tool_mounts).tools)

            if len(require_table_read) > 0:
                providers.extend(
                    (
                        await DatabaseToolkitBuilder().make(
                            room=self.room,
                            model=model,
                            config=DatabaseToolkitConfig(
                                tables=require_table_read, read_only=True
                            ),
                        )
                    ).tools
                )

            if len(require_table_write) > 0:
                providers.extend(
                    (
                        await DatabaseToolkitBuilder().make(
                            room=self.room,
                            model=model,
                            config=DatabaseToolkitConfig(
                                tables=require_table_write, read_only=False
                            ),
                        )
                    ).tools
                )

            if require_read_only_storage:
                providers.extend(
                    StorageToolkit(read_only=True, mounts=storage_tool_mounts).tools
                )

            if require_document_authoring:
                providers.extend(DocumentAuthoringToolkit().tools)
                providers.extend(
                    DocumentTypeAuthoringToolkit(
                        schema=widget_schema, document_type="widget"
                    ).tools
                )

            if require_discovery:
                from meshagent.tools.discovery import DiscoveryToolkit

                providers.extend(DiscoveryToolkit().tools)

            tk = await super().get_context_toolkits(context=context)
            return [
                *(
                    [Toolkit(name="tools", tools=providers)]
                    if len(providers) > 0
                    else []
                ),
                *tk,
            ]

        def get_toolkit_builders(self):
            providers = []

            if image_generation:
                providers.append(ImageGenerationToolkitBuilder())

            if apply_patch:
                providers.append(ApplyPatchToolkitBuilder())

            if local_shell:
                providers.append(
                    LocalShellToolkitBuilder(
                        working_directory=working_directory,
                    )
                )

            if shell:
                providers.append(
                    ShellToolkitBuilder(
                        working_directory=working_directory,
                    )
                )

            if mcp:
                providers.append(MCPToolkitBuilder())

            if web_search:
                providers.append(WebSearchToolkitBuilder())

            if storage_enabled:
                providers.append(StorageToolkitBuilder(mounts=storage_tool_mounts))

            return providers

    return CustomTaskRunner


@app.async_command("join")
async def join(
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
    rules_file: Optional[str] = None,
    toolkit: Annotated[
        List[str],
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.2",
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
    ] = None,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
    ] = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling", hidden=True),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling", hidden=True),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling", hidden=True),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling", hidden=True)
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit", hidden=True)
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table read tools for a specific table", hidden=True
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table write tools for a specific table", hidden=True
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit", hidden=True),
    ] = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable MeshDocument authoring", hidden=True),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools", hidden=True),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Delegate LLM interactions to a remote participant", hidden=True
        ),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use", hidden=True),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use", hidden=True),
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
    allow_thread_selection: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="allow selecting a thread via input path for task context"
        ),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    key = await resolve_key(project_id=project_id, key=key)
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

            token = ParticipantToken(
                name=agent_name,
            )

            token.add_api_grant(ApiScope.agent_default())

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        requirements = []

        for t in toolkit:
            requirements.append(RequiredToolkit(name=t))

        for t in schema:
            requirements.append(RequiredSchema(name=t))

        storage_tool_mounts = parse_storage_tool_mounts(
            local_paths=storage_tool_local_path,
            room_paths=storage_tool_room_path,
        )

        CustomTaskRunner = build_task_runner(
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            allow_thread_selection=allow_thread_selection,
            log_llm_requests=log_llm_requests,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            image_generation=image_generation,
            web_search=web_search,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            require_apply_patch=require_apply_patch,
            require_web_search=require_web_search,
            require_local_shell=require_local_shell,
            require_shell=require_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_read_only_storage=require_read_only_storage,
            room_rules_path=room_rules,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            working_directory=working_directory,
            delegate_shell_token=delegate_shell_token,
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
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
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


@app.async_command("run")
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
    rules_file: Optional[str] = None,
    toolkit: Annotated[
        List[str],
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.2",
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
    ] = None,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
    ] = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling", hidden=True),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling", hidden=True),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling", hidden=True),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling", hidden=True)
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit", hidden=True)
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table read tools for a specific table", hidden=True
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table write tools for a specific table", hidden=True
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit", hidden=True),
    ] = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable MeshDocument authoring", hidden=True),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools", hidden=True),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Delegate LLM interactions to a remote participant", hidden=True
        ),
    ] = None,
    output_schema: Annotated[
        Optional[str],
        typer.Option(..., help="an output schema to use", hidden=True),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use", hidden=True),
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
    allow_thread_selection: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="allow selecting a thread via input path for task context"
        ),
    ] = False,
    input: Annotated[
        Optional[str],
        typer.Option(..., help="json input for the task runner, or '-' for stdin"),
    ] = None,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    with_caller: Annotated[
        bool,
        typer.Option(
            help="whether to invoke the tool with the currently logged in user as the caller"
        ),
    ] = os.getenv("MESHAGENT_TOKEN") is None,
):
    key = await resolve_key(project_id=project_id, key=key)
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

            token = ParticipantToken(
                name=agent_name,
            )

            token.add_api_grant(ApiScope.agent_default())

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        requirements = []

        for t in toolkit:
            requirements.append(RequiredToolkit(name=t))

        for t in schema:
            requirements.append(RequiredSchema(name=t))

        storage_tool_mounts = parse_storage_tool_mounts(
            local_paths=storage_tool_local_path,
            room_paths=storage_tool_room_path,
        )

        CustomTaskRunner = build_task_runner(
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            allow_thread_selection=allow_thread_selection,
            log_llm_requests=log_llm_requests,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            image_generation=image_generation,
            web_search=web_search,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            require_apply_patch=require_apply_patch,
            require_web_search=require_web_search,
            require_local_shell=require_local_shell,
            require_shell=require_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_read_only_storage=require_read_only_storage,
            room_rules_path=room_rules,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            working_directory=working_directory,
            delegate_shell_token=delegate_shell_token,
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
            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
                    token=jwt,
                )
            ) as client:
                try:
                    input_payload = read_task_runner_input(input)

                    if with_caller:
                        connection = await account_client.connect_room(
                            project_id=project_id, room=room
                        )
                        async with RoomClient(
                            protocol=WebSocketClientProtocol(
                                url=websocket_room_url(room_name=room),
                                token=connection.jwt,
                            ),
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

                    if isinstance(result, JsonResponse):
                        print(result.json)
                    elif isinstance(result, TextResponse):
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
    rules_file: Optional[str] = None,
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
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.2",
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
    ] = None,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
    ] = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling", hidden=True),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool", hidden=True)
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling", hidden=True),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling", hidden=True)
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit", hidden=True)
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table read tools for a specific table", hidden=True
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table write tools for a specific table", hidden=True
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit", hidden=True),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring", hidden=True),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools", hidden=True),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Delegate LLM interactions to a remote participant", hidden=True
        ),
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
        typer.Option(..., help="an output schema to use", hidden=True),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use", hidden=True),
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
    allow_thread_selection: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="allow selecting a thread via input path for task context"
        ),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    print("[bold green]Connecting to room...[/bold green]", flush=True)

    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
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
        cls=build_task_runner(
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            allow_thread_selection=allow_thread_selection,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            require_web_search=require_web_search,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            room_rules_path=room_rules,
            working_directory=working_directory,
            delegate_shell_token=delegate_shell_token,
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


@app.async_command("spec")
async def spec(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[str] = None,
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
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.2",
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
    ] = None,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
    ] = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling", hidden=True),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool", hidden=True)
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling", hidden=True),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling", hidden=True)
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit", hidden=True)
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table read tools for a specific table", hidden=True
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table write tools for a specific table", hidden=True
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit", hidden=True),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring", hidden=True),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools", hidden=True),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Delegate LLM interactions to a remote participant", hidden=True
        ),
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
        typer.Option(..., help="an output schema to use", hidden=True),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use", hidden=True),
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
    allow_thread_selection: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="allow selecting a thread via input path for task context"
        ),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
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
        cls=build_task_runner(
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            allow_thread_selection=allow_thread_selection,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            require_web_search=require_web_search,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            room_rules_path=room_rules,
            working_directory=working_directory,
            delegate_shell_token=delegate_shell_token,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }

    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = (
        "us-central1-docker.pkg.dev/meshagent-public/images/cli:{SERVER_VERSION}-esgz"
    )
    spec.container.command = shlex.join(
        ["meshagent", "task-runner", "service", *cleanup_args(sys.argv[2:])]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy")
async def deploy(
    *,
    service_name: Annotated[str, typer.Option("--service-name", help="service name")],
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[str] = None,
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
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the task runner")
    ] = "gpt-5.2",
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
    ] = None,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
    ] = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling", hidden=True),
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool", hidden=True)
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling", hidden=True),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling", hidden=True)
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit", hidden=True)
    ] = False,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table read tools for a specific table", hidden=True
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            ..., help="Enable table write tools for a specific table", hidden=True
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit", hidden=True),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="Delegate the room token to shell tools"),
    ] = False,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring", hidden=True),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools", hidden=True),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(
            ..., help="Delegate LLM interactions to a remote participant", hidden=True
        ),
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
        typer.Option(..., help="an output schema to use", hidden=True),
    ] = None,
    output_schema_path: Annotated[
        Optional[str],
        typer.Option(..., help="the path or url to output schema to use", hidden=True),
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
    allow_thread_selection: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="allow selecting a thread via input path for task context"
        ),
    ] = False,
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
    project_id = await resolve_project_id(project_id=project_id)

    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
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
        cls=build_task_runner(
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            title=title,
            description=description,
            allow_model_selection=allow_model_selection,
            allow_thread_selection=allow_thread_selection,
            log_llm_requests=log_llm_requests,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            require_web_search=require_web_search,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            room_rules_path=room_rules,
            working_directory=working_directory,
            delegate_shell_token=delegate_shell_token,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            output_schema_str=output_schema,
            output_schema_path=output_schema_path,
            annotations=json.loads(annotations) if annotations != "" else {},
        ),
    )

    spec = service_specs()[0]
    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }

    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = (
        "us-central1-docker.pkg.dev/meshagent-public/images/cli:{SERVER_VERSION}-esgz"
    )
    spec.container.command = shlex.join(
        ["meshagent", "task-runner", "service", *cleanup_args(sys.argv[2:])]
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
