import typer
from meshagent.cli import async_typer
from rich import print

from meshagent.api import ParticipantToken
from typing import Annotated, Optional
from meshagent.cli.common_options import (
    ProjectIdOption,
    RoomOption,
)
from meshagent.tools import Toolkit
from meshagent.api import RoomClient, WebSocketClientProtocol, ApiScope
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
    cleanup_args,
)
from meshagent.openai import OpenAIResponsesAdapter

from meshagent.agents.config import RulesConfig

from typing import List
from pathlib import Path

from meshagent.api import RequiredToolkit, RequiredSchema, RoomException

import logging

from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig

from meshagent.tools.storage import StorageToolkit
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.uuid import UUIDToolkit

from meshagent.openai.tools.responses_adapter import (
    WebSearchTool,
    ShellConfig,
    ApplyPatchConfig,
    ApplyPatchTool,
    ShellTool,
    LocalShellTool,
    ImageGenerationTool,
)

from meshagent.cli.host import get_service, run_services, get_deferred, service_specs
from meshagent.api.specs.service import AgentSpec, ANNOTATION_AGENT_TYPE

import yaml

import shlex
import sys

from meshagent.api.client import ConflictError
from meshagent.agents.adapter import MessageStreamLLMAdapter

logger = logging.getLogger("mailbot")

app = async_typer.AsyncTyper(help="Join a mailbot to a room")


def build_mailbot(
    *,
    model: str,
    agent_name: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    image_generation: Optional[str] = None,
    local_shell: bool,
    computer_use: bool,
    rules_file: Optional[str] = None,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    toolkit_name: Optional[str] = None,
    queue: str,
    email_address: str,
    room_rules_paths: list[str],
    whitelist=list[str],
    require_shell: Optional[bool] = None,
    require_apply_patch: Optional[bool] = None,
    require_storage: Optional[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    require_table_read: bool,
    require_table_write: bool,
    require_computer_use: bool,
    reply_all: bool,
    database_namespace: Optional[list[str]] = None,
    enable_attachments: bool,
    working_directory: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    shell_image: Optional[str] = None,
    llm_participant: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
):
    from meshagent.agents.mail import MailWorker

    if (require_storage or require_read_only_storage) and len(whitelist) == 0:
        logger.warning(
            "you have enabled storage tools without a whilelist, anyone who can send to this mailbox will be able to ask it about files"
        )

    requirements = []

    toolkits = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    if rules_file is not None:
        try:
            with open(Path(rules_file).resolve(), "r") as f:
                rule.extend(f.read().splitlines())
        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

    BaseClass = MailWorker
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

    parsed_whitelist = []
    if len(whitelist) > 0:
        for w in whitelist:
            for s in w.split(","):
                s = s.strip()
                if len(s) > 0:
                    parsed_whitelist.append(s)

    class CustomMailbot(BaseClass):
        def __init__(self):
            super().__init__(
                llm_adapter=llm_adapter,
                name=agent_name,
                requires=requirements,
                toolkits=toolkits,
                queue=queue,
                email_address=email_address,
                toolkit_name=toolkit_name,
                rules=rule if len(rule) > 0 else None,
                whitelist=parsed_whitelist if len(parsed_whitelist) > 0 else None,
                reply_all=reply_all,
                enable_attachments=enable_attachments,
                skill_dirs=skill_dirs,
            )

        async def start(self, *, room: RoomClient):
            print(
                "[bold green]Configure and send an email interact with your mailbot[/bold green]"
            )
            await super().start(room=room)
            if room_rules_paths is not None:
                for p in room_rules_paths:
                    await self._load_room_rules(path=p)

        async def get_rules(self):
            rules = [*await super().get_rules()]
            if room_rules_paths is not None:
                for p in room_rules_paths:
                    rules.extend(await self._load_room_rules(path=p))

            return rules

        async def _load_room_rules(
            self,
            *,
            path: str,
        ):
            rules = []
            try:
                room_rules = await self.room.storage.download(path=path)

                rules_txt = room_rules.data.decode()

                rules_config = RulesConfig.parse(rules_txt)

                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)

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

        async def get_thread_toolkits(self, *, thread_context):
            toolkits = await super().get_thread_toolkits(thread_context=thread_context)

            thread_toolkit = Toolkit(name="thread_toolkit", tools=[])

            if local_shell:
                thread_toolkit.tools.append(
                    LocalShellTool(thread_context=thread_context)
                )

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

            if image_generation is not None:
                print("adding openai image gen to thread", flush=True)
                thread_toolkit.tools.append(
                    ImageGenerationTool(
                        model=image_generation,
                        thread_context=thread_context,
                        partial_images=3,
                    )
                )

            if web_search:
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

                toolkits.append(computer_toolkit)

            toolkits.append(thread_toolkit)
            return toolkits

    return CustomMailbot


@app.async_command("join")
async def make_call(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
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
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.2",
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    queue: Annotated[str, typer.Option(..., help="the name of the mail queue")],
    email_address: Annotated[
        str, typer.Option(..., help="the email address of the agent")
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="the name of a toolkit to expose mail operations"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    whitelist: Annotated[
        List[str],
        typer.Option(
            "--whitelist",
            help="an email to whitelist",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    reply_all: Annotated[
        bool, typer.Option(help="Reply-all when responding to emails")
    ] = False,
    enable_attachments: Annotated[
        bool, typer.Option(help="Allow downloading and processing email attachments")
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
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
            CustomMailbot = build_mailbot(
                computer_use=None,
                model=model,
                local_shell=require_local_shell,
                agent_name=agent_name,
                rule=rule,
                schema=require_schema + schema,
                toolkit=require_toolkit + toolkit,
                image_generation=None,
                web_search=require_web_search,
                rules_file=rules_file,
                queue=queue,
                email_address=email_address,
                toolkit_name=toolkit_name,
                room_rules_paths=room_rules,
                whitelist=whitelist,
                require_shell=require_shell,
                require_apply_patch=require_apply_patch,
                require_storage=require_storage,
                require_read_only_storage=require_read_only_storage,
                require_time=require_time,
                require_uuid=require_uuid,
                require_table_read=require_table_read,
                require_table_write=require_table_write,
                require_computer_use=require_computer_use,
                reply_all=reply_all,
                database_namespace=database_namespace,
                enable_attachments=enable_attachments,
                working_directory=working_directory,
                skill_dirs=skill_dir,
                shell_image=shell_image,
                llm_participant=llm_participant,
                log_llm_requests=log_llm_requests,
            )

            bot = CustomMailbot()

            await bot.start(room=client)
            try:
                print(
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
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
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
    queue: Annotated[str, typer.Option(..., help="the name of the mail queue")],
    email_address: Annotated[
        str, typer.Option(..., help="the email address of the agent")
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="the name of a toolkit to expose mail operations"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    whitelist: Annotated[
        List[str],
        typer.Option(
            "--whitelist",
            help="an email to whitelist",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    reply_all: Annotated[
        bool, typer.Option(help="Reply-all when responding to emails")
    ] = False,
    enable_attachments: Annotated[
        bool, typer.Option(help="Allow downloading and processing email attachments")
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
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

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_mailbot(
            queue=queue,
            computer_use=None,
            model=model,
            local_shell=require_local_shell,
            web_search=require_web_search,
            agent_name=agent_name,
            rule=rule,
            schema=require_schema + schema,
            toolkit=require_toolkit + toolkit,
            image_generation=None,
            rules_file=rules_file,
            email_address=email_address,
            toolkit_name=toolkit_name,
            room_rules_paths=room_rules,
            whitelist=whitelist,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            reply_all=reply_all,
            database_namespace=database_namespace,
            enable_attachments=enable_attachments,
            working_directory=working_directory,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            llm_participant=llm_participant,
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
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
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
    queue: Annotated[str, typer.Option(..., help="the name of the mail queue")],
    email_address: Annotated[
        str, typer.Option(..., help="the email address of the agent")
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="the name of a toolkit to expose mail operations"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    whitelist: Annotated[
        List[str],
        typer.Option(
            "--whitelist",
            help="an email to whitelist",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    reply_all: Annotated[
        bool, typer.Option(help="Reply-all when responding to emails")
    ] = False,
    enable_attachments: Annotated[
        bool, typer.Option(help="Allow downloading and processing email attachments")
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
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

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_mailbot(
            queue=queue,
            computer_use=None,
            model=model,
            local_shell=require_local_shell,
            web_search=require_web_search,
            agent_name=agent_name,
            rule=rule,
            schema=require_schema + schema,
            toolkit=require_toolkit + toolkit,
            image_generation=None,
            rules_file=rules_file,
            email_address=email_address,
            toolkit_name=toolkit_name,
            room_rules_paths=room_rules,
            whitelist=whitelist,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            reply_all=reply_all,
            database_namespace=database_namespace,
            enable_attachments=enable_attachments,
            working_directory=working_directory,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            llm_participant=llm_participant,
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
        ["meshagent", "mailbot", "service", *cleanup_args(sys.argv[2:])]
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
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
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
    queue: Annotated[str, typer.Option(..., help="the name of the mail queue")],
    email_address: Annotated[
        str, typer.Option(..., help="the email address of the agent")
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="the name of a toolkit to expose mail operations"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    whitelist: Annotated[
        List[str],
        typer.Option(
            "--whitelist",
            help="an email to whitelist",
        ),
    ] = [],
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
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
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use (requires computer-use-preview model)",
            hidden=True,
        ),
    ] = False,
    reply_all: Annotated[
        bool, typer.Option(help="Reply-all when responding to emails")
    ] = False,
    enable_attachments: Annotated[
        bool, typer.Option(help="Allow downloading and processing email attachments")
    ] = False,
    working_directory: Annotated[
        Optional[str],
        typer.Option(..., help="The default working directory for shell commands"),
    ] = None,
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
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

    service.agents.append(
        AgentSpec(name=agent_name, annotations={ANNOTATION_AGENT_TYPE: "ChatBot"})
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=build_mailbot(
            queue=queue,
            computer_use=None,
            model=model,
            local_shell=require_local_shell,
            web_search=require_web_search,
            agent_name=agent_name,
            rule=rule,
            schema=require_schema + schema,
            toolkit=require_toolkit + toolkit,
            image_generation=None,
            rules_file=rules_file,
            email_address=email_address,
            toolkit_name=toolkit_name,
            room_rules_paths=room_rules,
            whitelist=whitelist,
            require_shell=require_shell,
            require_apply_patch=require_apply_patch,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_computer_use=require_computer_use,
            reply_all=reply_all,
            database_namespace=database_namespace,
            enable_attachments=enable_attachments,
            working_directory=working_directory,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            llm_participant=llm_participant,
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
        ["meshagent", "mailbot", *cleanup_args(sys.argv[:2])]
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
