import typer
from rich import print
from typing import Annotated, Optional
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolkitBuilder
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
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
    cleanup_args,
)

from meshagent.openai import OpenAIResponsesAdapter

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

from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.agents.adapter import MessageStreamLLMAdapter

from meshagent.api import RequiredToolkit, RequiredSchema
import logging
import os.path

from meshagent.api.specs.service import AgentSpec, ANNOTATION_AGENT_TYPE

from meshagent.cli.host import get_service, run_services, get_deferred, service_specs

import yaml

import shlex
import sys

from meshagent.api.client import ConflictError

logger = logging.getLogger("chatbot")

app = async_typer.AsyncTyper(help="Join a chatbot to a room")


def build_chatbot(
    *,
    model: str,
    agent_name: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    image_generation: Optional[str] = None,
    local_shell: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    computer_use: Optional[str] = None,
    web_search: Optional[str] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    require_image_generation: Optional[str] = None,
    require_local_shell: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_computer_use: Optional[str] = None,
    require_web_search: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    rules_file: Optional[str] = None,
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
        try:
            with open(Path(os.path.expanduser(rules_file)).resolve(), "r") as f:
                rules_config = RulesConfig.parse(f.read())
                rule.extend(rules_config.rules)
                client_rules = rules_config.client_rules

        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

    BaseClass = ChatBot
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
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                log_requests=log_llm_requests,
            )

    class CustomChatbot(BaseClass):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                name=agent_name,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules,
                always_reply=always_reply,
                skill_dirs=skill_dirs,
            )

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_path is not None:
                for p in room_rules_path:
                    await self._load_room_rules(path=p)

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

            if require_shell:
                providers.append(
                    ShellTool(
                        working_directory=working_directory,
                        config=ShellConfig(name="shell"),
                        image=shell_image or "python:3.13",
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

            if require_storage:
                providers.extend(StorageToolkit().tools)

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
                providers.extend(StorageToolkit(read_only=True).tools)

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
                providers.append(
                    ShellToolkitBuilder(
                        working_directory=working_directory,
                        image=shell_image,
                    )
                )

            if mcp:
                providers.append(MCPToolkitBuilder())

            if web_search:
                providers.append(WebSearchToolkitBuilder())

            if storage:
                providers.append(StorageToolkitBuilder())

            return providers

    return CustomChatbot


@app.async_command("join")
async def make_call(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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

        token = ParticipantToken(
            name=agent_name,
        )

        token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))

        token.add_role_grant(role=role)
        token.add_room_grant(room)

        jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        ) as client:
            CustomChatbot = build_chatbot(
                computer_use=computer_use,
                require_computer_use=require_computer_use,
                model=model,
                agent_name=agent_name,
                rule=rule,
                toolkit=require_toolkit + toolkit,
                schema=require_schema + schema,
                rules_file=rules_file,
                local_shell=local_shell,
                shell=shell,
                apply_patch=apply_patch,
                image_generation=image_generation,
                web_search=web_search,
                mcp=mcp,
                storage=storage,
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
                log_llm_requests=log_llm_requests,
            )

            bot = CustomChatbot()

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
    rules_file: Optional[str] = None,
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

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
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            agent_name=agent_name,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            database_namespace=database_namespace,
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
            log_llm_requests=log_llm_requests,
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

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
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            agent_name=agent_name,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            database_namespace=database_namespace,
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
            log_llm_requests=log_llm_requests,
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
        ["meshagent", "chatbot", "service", *cleanup_args(sys.argv[2:])]
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = None,
):
    project_id = await resolve_project_id(project_id=project_id)

    if database_namespace is not None:
        database_namespace = database_namespace.split("::")

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
        cls=build_chatbot(
            computer_use=computer_use,
            require_computer_use=require_computer_use,
            model=model,
            local_shell=local_shell,
            shell=shell,
            apply_patch=apply_patch,
            agent_name=agent_name,
            rule=rule,
            toolkit=require_toolkit + toolkit,
            schema=require_schema + schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            database_namespace=database_namespace,
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
            log_llm_requests=log_llm_requests,
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
