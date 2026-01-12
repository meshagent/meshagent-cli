import typer
from rich import print
from typing import Annotated, Optional, List, Type
from pathlib import Path
import logging

from meshagent.tools.storage import StorageToolkitBuilder

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
    cleanup_args,
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

from meshagent.api.helpers import meshagent_base_url, websocket_room_url

from meshagent.agents.config import RulesConfig
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolkit
from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.uuid import UUIDToolkit
from meshagent.openai import OpenAIResponsesAdapter


# Your Worker base (the one you pasted) + adapters
from meshagent.agents.worker import Worker  # adjust import
from meshagent.agents.adapter import LLMAdapter, ToolResponseAdapter  # adjust import

from meshagent.openai.tools.responses_adapter import (
    WebSearchToolkitBuilder,
    MCPToolkitBuilder,
    WebSearchTool,
    ShellConfig,
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
from meshagent.api.specs.service import AgentSpec, ANNOTATION_AGENT_TYPE

import yaml

import shlex
import sys

from meshagent.api.client import ConflictError

logger = logging.getLogger("worker_cli")

app = async_typer.AsyncTyper(help="Join a worker agent to a room")


def build_worker(
    *,
    WorkerBase: Type[Worker],
    model: str,
    agent_name: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    queue: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tool_adapter: Optional[ToolResponseAdapter] = None,
    toolkits: Optional[list[Toolkit]] = None,
    rules_file: Optional[str] = None,
    room_rules_paths: list[str] | None = None,
    # thread/tool controls (mirrors mailbot)
    image_generation: Optional[str] = None,
    local_shell: Optional[str] = None,
    shell: Optional[str] = None,
    apply_patch: Optional[str] = None,
    web_search: Optional[str] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    working_directory: Optional[str] = None,
    require_image_generation: Optional[str] = None,
    require_local_shell: bool = False,
    require_web_search: bool = False,
    require_apply_patch: bool = False,
    require_shell: bool = False,
    require_storage: bool = False,
    require_read_only_storage: bool = False,
    require_time: bool = True,
    require_uuid: bool = False,
    database_namespace: Optional[list[str]] = None,
    require_table_read: list[str] | None = None,
    require_table_write: list[str] | None = None,
    require_computer_use: bool,
    toolkit_name: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    shell_image: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
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
        try:
            with open(Path(rules_file).resolve(), "r") as f:
                rule.extend(f.read().splitlines())
        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

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
        llm_adapter: LLMAdapter = OpenAIResponsesAdapter(
            model=model,
            log_requests=log_llm_requests,
        )

    class CustomWorker(WorkerBase):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                tool_adapter=tool_adapter,
                name=agent_name,
                requires=requirements,
                toolkits=toolkits,
                queue=queue,
                title=title or agent_name,
                description=description,
                rules=rule if len(rule) > 0 else None,
                toolkit_name=toolkit_name,
                skill_dirs=skill_dirs,
            )
            self._room_rules_paths = room_rules_paths or []

        async def start(self, *, room: RoomClient):
            print(
                "[bold green]Worker connected. It will consume queue messages.[/bold green]"
            )
            await super().start(room=room)
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

        async def get_message_toolkits(self, *, message: dict):
            """
            Optional hook if your WorkerBase supports thread contexts.
            If not, you can remove this; I left it to mirror mailbot's pattern.
            """
            toolkits_out = await super().get_message_toolkits(message=message)

            thread_toolkit = Toolkit(name="thread_toolkit", tools=[])

            if require_local_shell:
                thread_toolkit.tools.append(LocalShellTool())

            if require_shell:
                thread_toolkit.tools.append(
                    ShellTool(
                        working_directory=working_directory,
                        config=ShellConfig(name="shell"),
                        image=shell_image or "python:3.13",
                    )
                )

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
                thread_toolkit.tools.append(WebSearchTool())

            if require_storage:
                thread_toolkit.tools.extend(StorageToolkit().tools)

            if require_read_only_storage:
                thread_toolkit.tools.extend(StorageToolkit(read_only=True).tools)

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

            if require_computer_use:
                from meshagent.computers.agent import ComputerToolkit

                computer_toolkit = ComputerToolkit(room=self.room, render_screen=None)

                toolkits_out.append(computer_toolkit)

            toolkits_out.append(thread_toolkit)
            return toolkits_out

    return CustomWorker


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
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
        str, typer.Option(..., help="Name of the LLM model to use")
    ] = "gpt-5.2",
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
            hidden=True,
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
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
    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token = ParticipantToken(name=agent_name)
        token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))
        token.add_role_grant(role=role)
        token.add_room_grant(room_name)

        jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(
                    room_name=room_name, base_url=meshagent_base_url()
                ),
                token=jwt,
            )
        ) as client:
            # Plug in your specific worker implementation here:
            # from meshagent.agents.some_worker import SomeWorker
            # WorkerBase = SomeWorker
            from meshagent.agents.worker import Worker as WorkerBase  # default; replace

            CustomWorker = build_worker(
                WorkerBase=WorkerBase,
                model=model,
                agent_name=agent_name,
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
                mcp=mcp,
                storage=storage,
                require_local_shell=require_local_shell,
                require_web_search=require_web_search,
                require_shell=require_shell,
                require_apply_patch=require_apply_patch,
                toolkit_name=toolkit_name,
                require_storage=require_storage,
                require_read_only_storage=require_read_only_storage,
                require_time=require_time,
                require_uuid=require_uuid,
                require_table_read=require_table_read,
                require_table_write=require_table_write,
                require_computer_use=require_computer_use,
                database_namespace=[database_namespace] if database_namespace else None,
                title=title,
                description=description,
                working_directory=working_directory,
                skill_dirs=skill_dir,
                shell_image=shell_image,
                log_llm_requests=log_llm_requests,
            )

            worker = CustomWorker()
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
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
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
            hidden=True,
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
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
    service = get_service(host=host, port=port)

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
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            agent_name=agent_name,
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
            mcp=mcp,
            storage=storage,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_directory=working_directory,
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
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
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
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
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
            hidden=True,
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
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
    service = get_service(host=host, port=port)

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
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            agent_name=agent_name,
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
            mcp=mcp,
            storage=storage,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_directory=working_directory,
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
        ["meshagent", "worker", "service", *cleanup_args(sys.argv[2:])]
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
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
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
        str,
        typer.Option(..., help="Name of the LLM model to use"),
    ] = "gpt-5.2",
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
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Require local shell tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Require web search tool")
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
            hidden=True,
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
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
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

    service = get_service(host=host, port=port)

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
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            agent_name=agent_name,
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
            mcp=mcp,
            storage=storage,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_local_shell=require_local_shell,
            require_web_search=require_web_search,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
            working_directory=working_directory,
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
        ["meshagent", "worker", "service", *cleanup_args(sys.argv[2:])]
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
