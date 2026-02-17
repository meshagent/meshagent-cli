import typer
from rich import print
from typing import Annotated, Optional, List
from meshagent.tools import (
    Toolkit,
    ToolkitConfig,
    WebFetchTool,
    WebFetchToolkitBuilder,
    ContainerShellTool,
)
from meshagent.tools.storage import (
    StorageToolMount,
    StorageToolkitConfig,
    StorageToolkitBuilder,
)
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.uuid import UUIDToolkit
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
    parse_shell_tool_mounts,
    parse_storage_tool_mounts,
    resolve_key,
    resolve_project_id,
    resolve_room,
)

from meshagent.openai import OpenAIResponsesAdapter
from meshagent.anthropic import (
    AnthropicOpenAIResponsesStreamAdapter,
    WebFetchTool as AnthropicWebFetchTool,
    WebFetchToolkitBuilder as AnthropicWebFetchToolkitBuilder,
    WebSearchTool as AnthropicWebSearchTool,
    WebSearchToolkitBuilder as AnthropicWebSearchToolkitBuilder,
)

from pathlib import Path

from meshagent.tools.script import ScriptToolkitBuilder, get_script_tools

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

from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.agents.adapter import MessageStreamLLMAdapter

from meshagent.api import RequiredToolkit, RequiredSchema
import logging
import os.path
import os

from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ContainerMountSpec,
)

from meshagent.cli.host import get_service, run_services, get_deferred, service_specs

import yaml

import shlex
import sys

import asyncio
from datetime import datetime, timezone


from meshagent.api.client import ConflictError

logger = logging.getLogger("chatbot")

app = async_typer.AsyncTyper(help="Join a chatbot to a room")


def build_chatbot(
    *,
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    image_generation: Optional[str] = None,
    local_shell: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    computer_use: Optional[str] = None,
    web_search: Optional[str] = None,
    web_fetch: Optional[str] = None,
    script_tool: Optional[bool] = None,
    discover_script_tools: Optional[bool] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    storage_tool_mounts: Optional[list[StorageToolMount]] = None,
    shell_tool_mounts: Optional[ContainerMountSpec] = None,
    require_image_generation: Optional[str] = None,
    require_local_shell: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_computer_use: Optional[str] = None,
    require_web_search: Optional[str] = None,
    require_web_fetch: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    rules_file: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    require_discovery: Optional[str] = None,
    require_document_authoring: Optional[str] = None,
    working_directory: Optional[str] = None,
    llm_participant: Optional[str] = None,
    database_namespace: Optional[list[str]] = None,
    always_reply: Optional[bool] = None,
    skill_dirs: Optional[list[str]] = None,
    shell_image: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
    delegate_shell_token: Optional[bool] = None,
):
    from meshagent.agents.chat import ChatBot

    from meshagent.tools.storage import StorageToolkit

    requirements = []

    toolkits = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    client_rules = {}

    if rules_file is not None:
        for rules_path in rules_file:
            try:
                logger.info(f"loading rules from {rules_path}")
                with open(Path(os.path.expanduser(rules_path)).resolve(), "r") as f:
                    rules_config = RulesConfig.parse(f.read())
                    if rules_config.rules is not None:
                        rule.extend(rules_config.rules)
                    if rules_config.client_rules is not None:
                        client_rules.update(rules_config.client_rules)

            except FileNotFoundError:
                print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    is_claude_model = model.startswith("claude-")
    is_openai = not is_claude_model
    supports_openai_tools = llm_participant is None and not is_claude_model
    if not supports_openai_tools:
        if image_generation or require_image_generation:
            print("[red]image generation tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if local_shell or require_local_shell:
            print("[red]local shell tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if apply_patch or require_apply_patch:
            print("[red]apply patch tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if computer_use or require_computer_use:
            print(
                "[red]computer use tool is currently only supported by openai models[/red]"
            )
            raise typer.Exit(1)

    BaseClass = ChatBot
    decision_model = None
    if llm_participant:
        llm_adapter = MessageStreamLLMAdapter(
            participant_name=llm_participant,
        )
    else:
        if computer_use or require_computer_use:
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                response_options={
                    "reasoning": {"summary": "concise"},
                    "truncation": "auto",
                },
                log_requests=log_llm_requests,
            )
        else:
            if is_claude_model:
                llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                    model=model,
                    log_requests=log_llm_requests,
                )
                decision_model = model
            else:
                llm_adapter = OpenAIResponsesAdapter(
                    model=model,
                    log_requests=log_llm_requests,
                )

    class CustomChatbot(BaseClass):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules,
                always_reply=always_reply,
                skill_dirs=skill_dirs,
                decision_model=decision_model,
            )

            self.shell_tool = None

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            env = {}

            if delegate_shell_token:
                env["MESHAGENT_TOKEN"] = self.room.protocol.token
                env["OPENAI_API_KEY"] = self.room.protocol.token
                env["ANTHROPIC_API_KEY"] = self.room.protocol.token

            if require_shell:
                if is_openai:
                    shell_kwargs = {
                        "working_directory": working_directory,
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

        async def get_rules(self, *, thread_context, participant):
            rules = await super().get_rules(
                thread_context=thread_context, participant=participant
            )

            if room_rules_path is not None:
                for p in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(path=p, participant=participant)
                    )

            logging.info(f"using rules {rules}")

            return rules

        async def get_thread_toolkits(self, *, thread_context, participant):
            providers = []

            if discover_script_tools:
                providers.extend(await get_script_tools(self.room))

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

            if require_apply_patch:
                providers.append(
                    ApplyPatchTool(
                        config=ApplyPatchConfig(name="apply_patch"),
                    )
                )

            if self.shell_tool is not None:
                providers.append(self.shell_tool)
            if require_mcp:
                raise Exception(
                    "mcp tool cannot be required by cli currently, use 'optional' instead"
                )

            if require_web_search:
                if is_claude_model:
                    providers.append(AnthropicWebSearchTool())
                else:
                    providers.append(
                        WebSearchTool(config=WebSearchConfig(name="web_search"))
                    )

            if require_web_fetch:
                if is_claude_model:
                    providers.append(AnthropicWebFetchTool())
                else:
                    providers.append(WebFetchTool())

            if require_storage:
                providers.extend(StorageToolkit(mounts=storage_tool_mounts).tools)

            if len(require_table_read) > 0:
                providers.extend(
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

            if require_time:
                providers.extend((DatetimeToolkit()).tools)

            if require_uuid:
                providers.extend((UUIDToolkit()).tools)

            if len(require_table_write) > 0:
                providers.extend(
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

            tk = await super().get_thread_toolkits(
                thread_context=thread_context, participant=participant
            )

            if require_computer_use:
                from meshagent.computers.agent import ComputerToolkit

                def render_screen(image_bytes: bytes):
                    for participant in thread_context.participants:
                        self.room.messaging.send_message_nowait(
                            to=participant,
                            type="computer_screen",
                            message={},
                            attachment=image_bytes,
                        )

                computer_toolkit = ComputerToolkit(
                    room=self.room, render_screen=render_screen
                )

                tk.append(computer_toolkit)

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
                shell_builder_kwargs = {
                    "working_directory": working_directory,
                    "image": shell_image,
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

            if script_tool:
                providers.append(ScriptToolkitBuilder())

            if storage:
                providers.append(StorageToolkitBuilder(mounts=storage_tool_mounts))

            return providers

    return CustomChatbot


@app.async_command("join", help="Join a room and run a chatbot agent.")
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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.2",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="Enable computer use (requires computer-use-preview model)"
        ),
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
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling"),
    ] = False,
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
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Use a specific database namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Enable table read tools for a specific table"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Enable table write tools for a specific table"),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit"),
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
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools"),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
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
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

    key = await resolve_key(project_id=project_id, key=key)
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

            token = ParticipantToken(
                name=agent_name,
            )

            token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        storage_tool_mounts = parse_storage_tool_mounts(
            local_paths=storage_tool_local_path,
            room_paths=storage_tool_room_path,
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=shell_tool_room_path,
            project_paths=shell_tool_project_path,
            image_paths=shell_image_mount,
        )

        CustomChatbot = build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            image_generation=image_generation,
            web_search=web_search,
            web_fetch=web_fetch,
            script_tool=script_tool,
            discover_script_tools=discover_script_tools,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            require_apply_patch=require_apply_patch,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_local_shell=require_local_shell,
            require_shell=require_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            room_rules_path=room_rules,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            working_directory=working_directory,
            llm_participant=llm_participant,
            always_reply=always_reply,
            database_namespace=database_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            log_llm_requests=log_llm_requests,
        )

        bot = CustomChatbot()

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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
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
    computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="Enable computer use (requires computer-use-preview model)"
        ),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling"),
    ] = False,
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
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Use a specific database namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Enable table read tools for a specific table"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Enable table write tools for a specific table"),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit"),
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools"),
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
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
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
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

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

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            script_tool=script_tool,
            discover_script_tools=discover_script_tools,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            database_namespace=database_namespace,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            room_rules_path=room_rules,
            working_directory=working_directory,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            always_reply=always_reply,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            log_llm_requests=log_llm_requests,
        ),
    )

    if not get_deferred():
        await run_services()


@app.async_command("spec", help="Generate a service spec for deploying a chatbot.")
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
    rules_file: Optional[list[str]] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
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
    computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="Enable computer use (requires computer-use-preview model)"
        ),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling"),
    ] = False,
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
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Use a specific database namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Enable table read tools for a specific table"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Enable table write tools for a specific table"),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit"),
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools"),
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
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
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
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

    service = get_service(host=host, port=port)
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=shell_tool_room_path,
        project_paths=shell_tool_project_path,
    )

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
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            script_tool=script_tool,
            discover_script_tools=discover_script_tools,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            database_namespace=database_namespace,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            room_rules_path=room_rules,
            working_directory=working_directory,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            always_reply=always_reply,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            log_llm_requests=log_llm_requests,
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
        ["meshagent", "chatbot", "service", *cleanup_args(sys.argv[2:])]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy", help="Deploy a chatbot service to a project or room.")
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
    rules_file: Optional[list[str]] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
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
    computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="Enable computer use (requires computer-use-preview model)"
        ),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling"),
    ] = False,
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
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Use a specific database namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Enable table read tools for a specific table"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Enable table write tools for a specific table"),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit"),
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools"),
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
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
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
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
):
    project_id = await resolve_project_id(project_id=project_id)

    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

    service = get_service(host=host, port=port)
    storage_tool_mounts = parse_storage_tool_mounts(
        local_paths=storage_tool_local_path,
        room_paths=storage_tool_room_path,
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=shell_tool_room_path,
        project_paths=shell_tool_project_path,
    )

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
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            web_fetch=web_fetch,
            script_tool=script_tool,
            discover_script_tools=discover_script_tools,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            storage_tool_mounts=storage_tool_mounts,
            shell_tool_mounts=shell_tool_mounts,
            database_namespace=database_namespace,
            require_web_search=require_web_search,
            require_web_fetch=require_web_fetch,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            require_table_write=require_table_write,
            require_table_read=require_table_read,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            room_rules_path=room_rules,
            working_directory=working_directory,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            llm_participant=llm_participant,
            always_reply=always_reply,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            log_llm_requests=log_llm_requests,
        ),
    )

    spec = service_specs()[0]

    for port in spec.ports:
        port

    spec.metadata.annotations = {
        "meshagent.service.id": service_name,
    }

    spec.metadata.name = service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        ["meshagent", "chatbot", "service", *cleanup_args(sys.argv[2:])]
    )

    project_id = await resolve_project_id(project_id)

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


async def chat_with(
    *,
    participant_name: str,
    project_id: str,
    room: str,
    thread_path: Optional[str],
    message: Optional[str] = None,
    use_web_search: bool = False,
    use_image_gen: bool = False,
    use_storage: bool = False,
):
    from meshagent.agents.chat import ChatBotClient

    try:
        from textual._context import active_app
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, VerticalScroll
        from textual.widgets import Static, TextArea
        from rich.align import Align
        from rich.console import Group
        from rich.console import RenderableType
        from rich.markdown import Markdown
        from rich.padding import Padding
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text
    except ImportError as exc:
        print(
            "[bold red]Textual is required for chatbot UI. Install meshagent-cli dependencies and retry.[/bold red]"
        )
        raise typer.Exit(1) from exc

    def build_tools() -> list[ToolkitConfig]:
        tools: list[ToolkitConfig] = []
        if use_web_search:
            tools.append(WebSearchConfig())
        elif use_image_gen:
            tools.append(ImageGenerationConfig())
        elif use_storage:
            tools.append(StorageToolkitConfig())
        return tools

    def _suppress_textual_debug_features() -> None:
        raw_features = os.environ.get("TEXTUAL")
        if raw_features is None or raw_features.strip() == "":
            return

        parsed = [
            value.strip() for value in raw_features.split(",") if value.strip() != ""
        ]
        if len(parsed) == 0:
            return

        filtered = [
            value for value in parsed if value.lower() not in ("debug", "devtools")
        ]
        if len(filtered) == len(parsed):
            return

        if len(filtered) == 0:
            os.environ.pop("TEXTUAL", None)
        else:
            os.environ["TEXTUAL"] = ",".join(filtered)

    caret = "›"

    class RoomConnectTextualApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            align: center middle;
            padding: 0 2;
        }
        #connect-title {
            content-align: center middle;
            width: 100%;
        }
        #connect-title-gap-top {
            width: 100%;
            height: 1;
        }
        #connect-title-divider {
            width: 100%;
            content-align: center middle;
        }
        #connect-title-gap-bottom {
            width: 100%;
            height: 1;
        }
        #connect-header {
            content-align: center middle;
            width: 100%;
            padding: 0 0 1 0;
        }
        #connect-status {
            content-align: center middle;
            width: 100%;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "cancel_connect", "Cancel", priority=True),
        ]

        def __init__(
            self,
            *,
            room_name: str,
            status_queue: "asyncio.Queue[tuple[str, str]]",
            connect_task: "asyncio.Task[RoomClient]",
        ) -> None:
            super().__init__()
            self._room_name = room_name
            self._status_queue = status_queue
            self._connect_task = connect_task
            self._divider_view: Static | None = None
            self._header_view: Static | None = None
            self._status_view: Static | None = None
            self._consume_task: asyncio.Task | None = None
            self._watch_task: asyncio.Task | None = None
            self._spinner_timer = None
            self._spinner_frames = (
                "⠋",
                "⠙",
                "⠹",
                "⠸",
                "⠼",
                "⠴",
                "⠦",
                "⠧",
                "⠇",
                "⠏",
            )
            self._spinner_frame = 0
            self._divider_pulse_position = 0
            self._divider_pulse_direction = 1
            self._statuses: list[tuple[str, str]] = []
            self._connect_complete = False
            self._connect_failed = False

        def compose(self) -> ComposeResult:
            yield Static(Text("MeshAgent", style="bold green"), id="connect-title")
            yield Static(" ", id="connect-title-gap-top")
            yield Static(Rule(style="bright_black"), id="connect-title-divider")
            yield Static(" ", id="connect-title-gap-bottom")
            yield Static("", id="connect-header")
            yield Static("", id="connect-status")

        async def on_mount(self) -> None:
            self._divider_view = self.query_one("#connect-title-divider", Static)
            self._header_view = self.query_one("#connect-header", Static)
            self._status_view = self.query_one("#connect-status", Static)
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._consume_task = asyncio.create_task(self._consume_statuses())
            self._watch_task = asyncio.create_task(self._watch_connect_task())
            self._render_title_divider()
            self._render_statuses()

        async def on_unmount(self) -> None:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            if self._consume_task is not None:
                if not self._consume_task.done():
                    self._consume_task.cancel()
                await asyncio.gather(self._consume_task, return_exceptions=True)
                self._consume_task = None
            if self._watch_task is not None:
                if not self._watch_task.done():
                    self._watch_task.cancel()
                await asyncio.gather(self._watch_task, return_exceptions=True)
                self._watch_task = None

        async def action_cancel_connect(self) -> None:
            if not self._connect_task.done():
                self._connect_task.cancel()
            self.exit()

        async def _consume_statuses(self) -> None:
            try:
                while True:
                    status, message = await self._status_queue.get()
                    self._push_status(status=status, message=message)
            except asyncio.CancelledError:
                return

        async def _watch_connect_task(self) -> None:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                return
            except Exception as ex:
                self._connect_failed = True
                self._push_status(status="error", message=str(ex))
                await asyncio.sleep(0.75)
            else:
                self._connect_complete = True
                await asyncio.sleep(0.2)
            finally:
                self.exit()

        def _on_spinner_tick(self) -> None:
            if self._connect_complete or self._connect_failed:
                return
            if len(self._spinner_frames) == 0:
                return
            self._spinner_frame = (self._spinner_frame + 1) % len(self._spinner_frames)
            self._advance_title_divider_pulse()
            self._advance_title_divider_pulse()
            self._render_title_divider()
            self._render_statuses()

        def _title_divider_length(self) -> int:
            available_width = max(self.size.width - 4, 1)
            return max(8, int(round(available_width * 0.3)))

        def _advance_title_divider_pulse(self) -> None:
            divider_length = self._title_divider_length()
            pulse_width = max(2, min(5, divider_length // 6))
            max_position = max(0, divider_length - pulse_width)

            next_position = self._divider_pulse_position + self._divider_pulse_direction
            if next_position >= max_position:
                self._divider_pulse_position = max_position
                self._divider_pulse_direction = -1
                return
            if next_position <= 0:
                self._divider_pulse_position = 0
                self._divider_pulse_direction = 1
                return

            self._divider_pulse_position = next_position

        def _render_title_divider(self) -> None:
            if self._divider_view is None:
                return

            divider_length = self._title_divider_length()
            pulse_width = max(2, min(5, divider_length // 6))
            max_position = max(0, divider_length - pulse_width)
            pulse_start = min(max(self._divider_pulse_position, 0), max_position)
            pulse_end = min(divider_length, pulse_start + pulse_width)

            divider_text = Text(
                "─" * divider_length, style="bright_black", justify="center"
            )
            if pulse_end > pulse_start:
                divider_text.stylize("bold green", pulse_start, pulse_end)
            self._divider_view.update(divider_text)

        def _push_status(self, *, status: str, message: str) -> None:
            normalized_status = status.strip() if isinstance(status, str) else ""
            normalized_message = message.strip() if isinstance(message, str) else ""
            if normalized_message == "":
                normalized_message = normalized_status.replace("_", " ").strip()
            if normalized_message == "":
                normalized_message = "connecting to room"

            if len(self._statuses) > 0 and self._statuses[-1][0] == normalized_status:
                self._statuses[-1] = (normalized_status, normalized_message)
            else:
                self._statuses.append((normalized_status, normalized_message))

            if len(self._statuses) > 12:
                self._statuses = self._statuses[-12:]
            self._render_statuses()

        def _render_statuses(self) -> None:
            if self._header_view is None or self._status_view is None:
                return

            self._header_view.update(
                Text(
                    f"Connecting to room '{self._room_name}'...",
                    style="bold",
                    justify="center",
                )
            )

            if len(self._statuses) == 0:
                lines = Text(justify="center")
                lines.append(
                    f"{self._spinner_frames[self._spinner_frame]} ", style="cyan"
                )
                lines.append("connecting to room")
                self._status_view.update(lines)
                return

            body = Text(justify="center")
            last_index = len(self._statuses) - 1
            for index, (status, message) in enumerate(self._statuses):
                active = index == last_index and not self._connect_complete
                prefix_style = "dim"
                prefix = "• "
                if index == last_index:
                    if self._connect_failed:
                        prefix = "✖ "
                        prefix_style = "bold red"
                    elif self._connect_complete:
                        prefix = "✓ "
                        prefix_style = "bold green"
                    elif active:
                        prefix = f"{self._spinner_frames[self._spinner_frame]} "
                        prefix_style = "cyan"

                body.append(prefix, style=prefix_style)
                body.append(message)

                status_label = status.replace("_", " ").strip()
                if status_label != "" and status_label != message:
                    body.append(f" ({status_label})", style="dim")

                if index < last_index:
                    body.append("\n")

            self._status_view.update(body)

    class ChatWithTextualApp(App[None]):
        CSS = """
        Screen {
            layout: grid;
            grid-size: 1 2;
            grid-rows: 1fr auto;
            padding: 0;
        }
        #messages-scroll {
            height: 1fr;
            padding: 0;
            align: left bottom;
        }
        #messages {
            content-align: left bottom;
            width: 100%;
        }
        #input-row {
            margin: 0;
            background: #2f2f2f;
            padding: 1 0 1 0;
        }
        #input-prompt {
            width: 2;
            height: auto;
            content-align: center top;
            color: $text-muted;
            background: #2f2f2f;
        }
        #chat-input {
            width: 1fr;
            height: 1;
            min-height: 1;
            max-height: 6;
            border: none;
            outline: none;
            padding: 0;
            margin: 0;
            color: white;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #chat-input:focus {
            border: none;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #chat-input .text-area--cursor-line {
            background: #2f2f2f;
        }
        #chat-input .text-area--gutter {
            background: #2f2f2f;
        }
        #chat-input .text-area--cursor-gutter {
            background: #2f2f2f;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit_app", "Quit", priority=True),
            Binding("enter", "submit_chat_input", "Send", priority=True),
            Binding("escape", "cancel_turn", "Cancel"),
            Binding("ctrl+l", "clear_thread", "Clear thread"),
        ]

        def __init__(
            self,
            *,
            chat_client: ChatBotClient,
            participant_name: str,
            local_user_name: str,
        ) -> None:
            super().__init__()
            self._chat_client = chat_client
            self._participant_name = participant_name
            self._local_user_name = local_user_name
            self._messages_view: Static | None = None
            self._messages_scroll: VerticalScroll | None = None
            self._chat_input: TextArea | None = None
            self._chat_input_height = 1
            self._doc_watch_task: asyncio.Task | None = None
            self._doc_changed = asyncio.Event()
            self._spinner_frames = (
                "⠋",
                "⠙",
                "⠹",
                "⠸",
                "⠼",
                "⠴",
                "⠦",
                "⠧",
                "⠇",
                "⠏",
            )
            self._spinner_frame = 0
            self._spinner_timer = None
            self._has_active_events = False

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="messages-scroll"):
                yield Static("", id="messages")
            with Horizontal(id="input-row"):
                yield Static(caret, id="input-prompt")
                yield TextArea(
                    "",
                    id="chat-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                )

        async def on_mount(self) -> None:
            self._messages_view = self.query_one("#messages", Static)
            self._messages_scroll = self.query_one("#messages-scroll", VerticalScroll)
            self._chat_input = self.query_one("#chat-input", TextArea)
            self._chat_input.focus()
            self._resize_chat_input(self._chat_input)
            self._bind_thread_document_events()
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._doc_watch_task = asyncio.create_task(self._watch_thread_document())
            self._doc_changed.set()

        async def on_unmount(self) -> None:
            self._stop_spinner_timer()
            await self._stop_doc_watch_loop()

        async def action_clear_thread(self) -> None:
            await self._chat_client.clear()

        async def action_cancel_turn(self) -> None:
            await self._chat_client.cancel()

        async def action_quit_app(self) -> None:
            self._stop_spinner_timer()
            await self._stop_doc_watch_loop()
            self.exit()

        def _stop_spinner_timer(self) -> None:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None

        def _is_active_event_state(self, state: str) -> bool:
            normalized = state.strip().lower()
            return normalized in ("queued", "in_progress", "running", "pending")

        def _event_spinner(self) -> str:
            count = len(self._spinner_frames)
            if count == 0:
                return ""
            return self._spinner_frames[self._spinner_frame % count]

        def _on_spinner_tick(self) -> None:
            if not self._has_active_events:
                return
            count = len(self._spinner_frames)
            if count == 0:
                return
            self._spinner_frame = (self._spinner_frame + 1) % count
            self._render_from_thread_document()

        async def action_submit_chat_input(self) -> None:
            if self._chat_input is None or self.focused is not self._chat_input:
                return

            user_input = self._chat_input.text.strip()
            self._chat_input.load_text("")
            self._resize_chat_input(self._chat_input)

            if not user_input:
                return

            if user_input in {"/exit", "/quit"}:
                await self.action_quit_app()
                return

            if user_input == "/clear":
                await self.action_clear_thread()
                return
            if user_input == "/cancel":
                await self.action_cancel_turn()
                return

            await self._chat_client.send(text=user_input, tools=build_tools())

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if self._chat_input is None or event.text_area is not self._chat_input:
                return
            self._resize_chat_input(event.text_area)

        def _resize_chat_input(self, chat_input: TextArea) -> None:
            target_height = max(1, min(6, chat_input.virtual_size.height))
            if target_height == self._chat_input_height:
                return
            self._chat_input_height = target_height
            chat_input.styles.height = target_height
            # Reflow message area after input height changes to keep feed visible.
            self._render_from_thread_document()

        def _bind_thread_document_events(self) -> None:
            doc = getattr(self._chat_client, "_doc", None)
            if doc is None:
                return

            @doc.on("inserted")
            def _on_inserted(_):
                self._doc_changed.set()

            @doc.on("updated")
            def _on_updated(_, __):
                self._doc_changed.set()

            @doc.on("deleted")
            def _on_deleted(_):
                self._doc_changed.set()

        async def _watch_thread_document(self) -> None:
            try:
                while True:
                    await self._doc_changed.wait()
                    self._doc_changed.clear()
                    self._render_from_thread_document()
            except asyncio.CancelledError:
                return

        async def _stop_doc_watch_loop(self) -> None:
            if self._doc_watch_task is None:
                return
            if not self._doc_watch_task.done():
                self._doc_watch_task.cancel()
            await asyncio.gather(self._doc_watch_task, return_exceptions=True)
            self._doc_watch_task = None

        def _render_from_thread_document(self) -> None:
            if self._messages_view is None:
                return

            doc = getattr(self._chat_client, "_doc", None)
            if doc is None:
                self._messages_view.update("")
                return

            message_nodes = doc.root.get_children_by_tag_name("messages")
            if len(message_nodes) == 0:
                self._messages_view.update("")
                return

            self._has_active_events = False
            rendered_items: list[RenderableType] = []
            for item in message_nodes[0].get_children():
                for renderable in self._render_thread_item(item):
                    rendered_items.append(renderable)

            if len(rendered_items) == 0:
                self._messages_view.update("")
            else:
                self._messages_view.update(Group(*rendered_items))

            if self._messages_scroll is not None:
                self._messages_scroll.scroll_end(animate=False)

        def _render_thread_item(self, item) -> list[RenderableType]:
            tag_name = getattr(item, "tag_name", None)
            if tag_name == "message":
                return [self._render_message_item(item)]
            if tag_name == "event":
                return [self._render_event_item(item)]
            if tag_name == "reasoning":
                summary = item.get_attribute("summary")
                if not isinstance(summary, str) or summary.strip() == "":
                    return []
                return [self._render_reasoning_item(item)]
            if tag_name == "exec":
                return [self._render_exec_item(item)]
            if tag_name == "ui":
                return [self._render_ui_item(item)]
            return []

        def _render_message_item(self, item) -> RenderableType:
            author = item.get_attribute("author_name") or "unknown"
            text = item.get_attribute("text") or ""
            relative_time = self._relative_time_label(item.get_attribute("created_at"))
            sender_prefix = (
                author if relative_time == "" else f"{author} ({relative_time})"
            )
            is_local = author == self._local_user_name
            header_style = "bold white" if is_local else "bold"
            body_style = "white" if is_local else ""
            row_style = "on #3f3f3f" if is_local else ""

            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            def add_message_row(
                content: RenderableType, *, left_padding: str = "  "
            ) -> None:
                left = Text(left_padding, style=row_style if row_style != "" else "")
                right = Text("  ", style=row_style if row_style != "" else "")
                if row_style != "":
                    table.add_row(left, content, right, style=row_style)
                else:
                    table.add_row(left, content, right)

            # Top padding row for every message block.
            add_message_row(Text(" "))

            add_message_row(Text(sender_prefix, style=header_style))

            markdown_text = text if text.strip() != "" else " "
            add_message_row(Markdown(markdown_text), left_padding="  ")

            for child in item.get_children():
                if getattr(child, "tag_name", None) == "file":
                    path = child.get_attribute("path")
                    if path is not None and path != "":
                        attachment_line = f"[attachment] {path}"
                        if body_style != "":
                            add_message_row(Text(attachment_line, style=body_style))
                        else:
                            add_message_row(Text(attachment_line, style="dim"))

            # Bottom padding row for every message block.
            add_message_row(Text(" "))

            return table

        def _relative_time_label(self, created_at: str | None) -> str:
            if created_at is None or created_at == "":
                return ""

            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return ""

            seconds = int((datetime.now(timezone.utc) - parsed).total_seconds())
            if seconds < 0:
                seconds = 0

            if seconds < 10:
                return "just now"
            if seconds < 60:
                return f"{seconds}s ago"

            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m ago"

            hours = minutes // 60
            if hours < 24:
                return f"{hours}h ago"

            days = hours // 24
            if days < 7:
                return f"{days}d ago"

            weeks = days // 7
            if weeks < 5:
                return f"{weeks}w ago"

            months = days // 30
            if months < 12:
                return f"{months}mo ago"

            years = days // 365
            return f"{years}y ago"

        def _render_event_item(self, item) -> RenderableType:
            kind = item.get_attribute("kind") or "event"
            state = item.get_attribute("state") or "info"
            active = self._is_active_event_state(state)
            if active:
                self._has_active_events = True
            headline = (
                item.get_attribute("headline")
                or item.get_attribute("summary")
                or item.get_attribute("name")
                or ""
            )
            if not isinstance(headline, str):
                headline = ""
            headline = headline.strip()

            summary = item.get_attribute("summary") or ""
            if not isinstance(summary, str):
                summary = ""
            summary = summary.strip()

            if headline == "":
                headline = summary

            if headline == "":
                headline = "event"

            if active:
                headline_text = f"{self._event_spinner()} {headline}"
            else:
                headline_text = headline

            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            # Top padding row.
            table.add_row(Text("  "), Text(" "), Text("  "))
            table.add_row(
                Text("  "), Text(headline_text, style="bold magenta"), Text("  ")
            )

            if summary != "" and summary.casefold() != headline.casefold():
                table.add_row(Text("  "), Text(summary, style="dim"), Text("  "))

            detail_lines = self._event_detail_lines(item)
            if len(detail_lines) > 0:
                table.add_row(Text("  "), Text(" "), Text("  "))
                for line in detail_lines:
                    detail_text = Text("  ")
                    detail_text.append_text(
                        self._render_event_detail_line(kind=kind, line=line)
                    )
                    table.add_row(Text("  "), detail_text, Text("  "))
                table.add_row(Text("  "), Text(" "), Text("  "))
            # Bottom padding row.
            table.add_row(Text("  "), Text(" "), Text("  "))

            return table

        def _event_detail_lines(self, item) -> list[str]:
            details = item.get_attribute("details") or ""
            if not isinstance(details, str) or details.strip() == "":
                details = item.get_attribute("data") or ""
            if not isinstance(details, str) or details.strip() == "":
                return []
            return details.splitlines()

        def _render_event_detail_line(self, *, kind: str, line: str) -> Text:
            if kind == "diff":
                return self._render_diff_line(line)
            return Text(line, style="dim")

        def _render_diff_line(self, line: str) -> Text:
            text = Text(line)
            if line.startswith("@@"):
                text.stylize("bold cyan")
            elif line.startswith("+++ ") or line.startswith("--- "):
                text.stylize("bold yellow")
            elif line.startswith("+"):
                text.stylize("green")
            elif line.startswith("-"):
                text.stylize("red")
            elif line.startswith("diff ") or line.startswith("index "):
                text.stylize("bold blue")
            elif line.strip().startswith("```"):
                text.stylize("dim")
            return text

        def _render_reasoning_item(self, item) -> RenderableType:
            summary = item.get_attribute("summary") or ""
            if not isinstance(summary, str):
                summary = ""

            markdown_text = summary if summary.strip() != "" else " "
            return Group(
                Text(" "),
                Rule(style="bright_black"),
                Text(" "),
                Padding(Markdown(markdown_text), (0, 2)),
                Text(" "),
            )

        def _render_exec_item(self, item) -> RenderableType:
            command = item.get_attribute("command") or ""
            outcome = item.get_attribute("outcome") or ""
            stdout = item.get_attribute("stdout") or ""
            stderr = item.get_attribute("stderr") or ""
            parts = []
            if command != "":
                parts.append(f"$ {command}")
            if outcome != "":
                parts.append(f"outcome: {outcome}")
            if stdout != "":
                parts.append(stdout)
            if stderr != "":
                parts.append(stderr)
            text = "\n".join(parts).strip() or "exec"
            return Align.center(Panel(Text(text), border_style="yellow", title="exec"))

        def _render_ui_item(self, item) -> RenderableType:
            widget = item.get_attribute("widget") or "ui"
            renderer = item.get_attribute("renderer") or "unknown"
            data = item.get_attribute("data") or ""
            if data != "":
                text = f"{widget} via {renderer}\n{data}"
            else:
                text = f"{widget} via {renderer}"
            return Align.center(Panel(Text(text), border_style="blue", title="ui"))

    account_client = None
    user_client: RoomClient | None = None
    chat_client: ChatBotClient | None = None

    def _queue_status(
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
        *,
        status: str,
        message: str,
    ) -> None:
        if status_queue is None:
            return
        status_queue.put_nowait((status, message))

    async def _close_chat_client(client: ChatBotClient | None) -> None:
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _close_user_client(client: RoomClient | None) -> None:
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _open_user_client(
        *,
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
    ) -> RoomClient:
        nonlocal account_client

        if account_client is None:
            _queue_status(
                status_queue,
                status="initializing",
                message="initializing account client",
            )
            account_client = await get_client()

        _queue_status(status_queue, status="starting_room", message="starting room")
        connection = await account_client.connect_room(project_id=project_id, room=room)
        _queue_status(status_queue, status="connecting", message="connecting to room")

        connecting_client = RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ),
        )

        def _on_room_status(**kwargs) -> None:
            status = kwargs.get("status", "")
            message = kwargs.get("message", "")
            if not isinstance(status, str):
                status = str(status)
            if not isinstance(message, str):
                message = str(message)
            _queue_status(status_queue, status=status, message=message)

        connecting_client.on("room.status", _on_room_status)

        try:
            await connecting_client.__aenter__()
        except asyncio.CancelledError:
            try:
                await connecting_client.__aexit__(None, None, None)
            except Exception:
                pass
            raise
        except Exception:
            try:
                await connecting_client.__aexit__(None, None, None)
            except Exception:
                pass
            raise

        _queue_status(status_queue, status="connected", message="connected to room")
        return connecting_client

    async def _prepare_chat_session(
        *,
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
    ) -> tuple[RoomClient, ChatBotClient, str]:
        prepared_user_client = await _open_user_client(status_queue=status_queue)
        prepared_chat_client: ChatBotClient | None = None
        try:
            _queue_status(
                status_queue,
                status="syncing",
                message="initializing room state",
            )
            await prepared_user_client.messaging.enable()

            local_user_name = prepared_user_client.local_participant.get_attribute(
                "name"
            )
            resolved_thread_path = thread_path
            if resolved_thread_path is None:
                resolved_thread_path = (
                    f".threads/{participant_name}/{local_user_name}.thread"
                )

            _queue_status(
                status_queue,
                status="opening_thread",
                message="opening chat thread",
            )
            prepared_chat_client = ChatBotClient(
                room=prepared_user_client,
                participant_name=participant_name,
                thread_path=resolved_thread_path,
            )
            await prepared_chat_client.__aenter__()

            _queue_status(
                status_queue,
                status="starting_ui",
                message="starting chat ui",
            )
            return prepared_user_client, prepared_chat_client, local_user_name
        except asyncio.CancelledError:
            await _close_chat_client(prepared_chat_client)
            await _close_user_client(prepared_user_client)
            raise
        except Exception:
            await _close_chat_client(prepared_chat_client)
            await _close_user_client(prepared_user_client)
            raise

    try:
        if message is None:
            _suppress_textual_debug_features()
            status_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
            connect_task = asyncio.create_task(
                _prepare_chat_session(status_queue=status_queue)
            )

            connect_app = RoomConnectTextualApp(
                room_name=room,
                status_queue=status_queue,
                connect_task=connect_task,
            )
            connect_token = active_app.set(connect_app)
            try:
                await connect_app.run_async()
            except KeyboardInterrupt:
                if not connect_task.done():
                    connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                return
            finally:
                active_app.reset(connect_token)

            if not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                return

            try:
                user_client, chat_client, local_user_name = await connect_task
            except asyncio.CancelledError:
                return
            except Exception as ex:
                print(f"[bold red]Unable to connect to room: {ex}[/bold red]")
                return

            app = ChatWithTextualApp(
                chat_client=chat_client,
                participant_name=participant_name,
                local_user_name=local_user_name,
            )
            token = active_app.set(app)
            try:
                await app.run_async()
            except KeyboardInterrupt:
                return
            finally:
                active_app.reset(token)
        else:
            user_client = await _open_user_client(status_queue=None)
            await user_client.messaging.enable()

            local_user_name = user_client.local_participant.get_attribute("name")
            resolved_thread_path = thread_path
            if resolved_thread_path is None:
                resolved_thread_path = (
                    f".threads/{participant_name}/{local_user_name}.thread"
                )

            chat_client = ChatBotClient(
                room=user_client,
                participant_name=participant_name,
                thread_path=resolved_thread_path,
            )
            await chat_client.__aenter__()
            await chat_client.send(text=message, tools=build_tools())
            response = await chat_client.receive()
            print(response)
            return

    except asyncio.CancelledError:
        pass

    finally:
        await _close_chat_client(chat_client)
        await _close_user_client(user_client)
        if account_client is not None:
            await account_client.close()


@app.async_command("run", help="Join a room, run the chatbot, and wait for messages.")
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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.2",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ..., help="Enable computer use (requires computer-use-preview model)"
        ),
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
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
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
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable local shell tool calling"),
    ] = False,
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
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    database_namespace: Annotated[
        Optional[str],
        typer.Option(..., help="Use a specific database namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(..., help="Enable table read tools for a specific table"),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(..., help="Enable table write tools for a specific table"),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable read only storage toolkit"),
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
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable discovery of agents and tools"),
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
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
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
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
    thread_path: Annotated[
        Optional[str],
        typer.Option(..., help="log all requests to the llm"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="the input message to use"),
    ] = None,
    use_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="request the web search tool"),
    ] = None,
    use_image_gen: Annotated[
        Optional[bool],
        typer.Option(..., help="request the image gen tool"),
    ] = None,
    use_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="request the storage tool"),
    ] = None,
):
    if not verbose:
        root = logging.getLogger()
        root.setLevel(logging.ERROR)

    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

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

            token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        storage_tool_mounts = parse_storage_tool_mounts(
            local_paths=storage_tool_local_path,
            room_paths=storage_tool_room_path,
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=shell_tool_room_path,
            project_paths=shell_tool_project_path,
        )

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            )
        ) as client:
            CustomChatbot = build_chatbot(
                computer_use=computer_use,
                require_computer_use=require_computer_use,
                model=model,
                rule=rule,
                toolkit=require_toolkit + toolkit,
                schema=require_schema + schema,
                rules_file=rules_file,
                local_shell=local_shell,
                shell=shell,
                apply_patch=apply_patch,
                image_generation=image_generation,
                web_search=web_search,
                web_fetch=web_fetch,
                script_tool=script_tool,
                discover_script_tools=discover_script_tools,
                mcp=mcp,
                storage=storage,
                storage_tool_mounts=storage_tool_mounts,
                shell_tool_mounts=shell_tool_mounts,
                require_apply_patch=require_apply_patch,
                require_web_search=require_web_search,
                require_web_fetch=require_web_fetch,
                require_local_shell=require_local_shell,
                require_shell=require_shell,
                require_image_generation=require_image_generation,
                require_mcp=require_mcp,
                require_storage=require_storage,
                require_table_read=require_table_read,
                require_table_write=require_table_write,
                require_read_only_storage=require_read_only_storage,
                require_time=require_time,
                require_uuid=require_uuid,
                room_rules_path=room_rules,
                require_document_authoring=require_document_authoring,
                require_discovery=require_discovery,
                working_directory=working_directory,
                llm_participant=llm_participant,
                always_reply=always_reply,
                database_namespace=database_namespace,
                skill_dirs=skill_dir,
                shell_image=shell_image,
                delegate_shell_token=delegate_shell_token,
                log_llm_requests=log_llm_requests,
            )

            bot = CustomChatbot()

            await bot.start(room=client)

            _, pending = await asyncio.wait(
                [
                    asyncio.create_task(client.protocol.wait_for_close()),
                    asyncio.create_task(
                        chat_with(
                            participant_name=client.local_participant.get_attribute(
                                "name"
                            ),
                            room=room,
                            project_id=project_id,
                            thread_path=thread_path,
                            message=message,
                            use_web_search=use_web_search,
                            use_image_gen=use_image_gen,
                            use_storage=use_storage,
                        )
                    ),
                ],
                return_when="FIRST_COMPLETED",
            )

            for t in pending:
                t.cancel()

    except asyncio.CancelledError:
        return

    finally:
        await account_client.close()


@app.async_command(
    "use", help="Send a one-shot or interactive message to a running chatbot."
)
async def use(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    thread_path: Annotated[
        Optional[str],
        typer.Option(..., help="log all requests to the llm"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="the input message to use"),
    ] = None,
    use_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="request the web search tool"),
    ] = None,
    use_image_gen: Annotated[
        Optional[bool],
        typer.Option(..., help="request the image gen tool"),
    ] = None,
    use_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="request the storage tool"),
    ] = None,
):
    root = logging.getLogger()
    root.setLevel(logging.ERROR)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        await chat_with(
            participant_name=agent_name,
            room=room,
            project_id=project_id,
            thread_path=thread_path,
            message=message,
            use_web_search=use_web_search,
            use_image_gen=use_image_gen,
            use_storage=use_storage,
        )

    except asyncio.CancelledError:
        return

    finally:
        await account_client.close()
