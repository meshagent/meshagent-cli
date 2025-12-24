import typer
from meshagent.api import ParticipantToken
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import (
    ProjectIdOption,
    RoomOption,
)
from meshagent.tools import Toolkit
from meshagent.api import RoomClient, WebSocketClientProtocol, ApiScope
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)
from meshagent.openai import OpenAIResponsesAdapter
from meshagent.openai.tools.responses_adapter import ImageGenerationTool, LocalShellTool
from meshagent.api.services import ServiceHost

from meshagent.agents.config import RulesConfig

from typing import List
from pathlib import Path

from meshagent.api import RequiredToolkit, RequiredSchema, RoomException
from meshagent.openai.tools.responses_adapter import WebSearchTool

import logging

from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig

from meshagent.tools.storage import StorageToolkit

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
    require_storage: Optional[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_table_read: bool,
    require_table_write: bool,
    reply_all: bool,
    database_namespace: Optional[list[str]] = None,
):
    from meshagent.agents.mail import MailWorker

    if (require_storage or require_read_only_storage) and len(whitelist) == 0:
        logger.error(
            "you may only enable storage tools when you also provide a whitelist, storage will not be enabled"
        )
        require_storage = False
        require_read_only_storage = False

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
    if computer_use:
        raise ValueError("computer use is not yet supported for the mail agent")
    else:
        llm_adapter = OpenAIResponsesAdapter(model=model)

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
                whitelist=whitelist if len(whitelist) > 0 else None,
                reply_all=reply_all,
            )

        async def start(self, *, room: RoomClient):
            print(
                "[bold green]Configure and send an email interact with your mailbot[/bold green]"
            )
            await super().start(room=room)

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
            toolkits.append(thread_toolkit)
            return toolkits

    return CustomMailbot


@app.async_command("join")
async def make_call(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
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
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.2",
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
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
    ],
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
    reply_all: Annotated[bool, typer.Option()] = False,
):
    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        room = resolve_room(room)

        token = ParticipantToken(
            name=agent_name,
        )

        token.add_api_grant(ApiScope.agent_default())

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
            requirements = []

            for t in toolkit:
                requirements.append(RequiredToolkit(name=t))

            for t in schema:
                requirements.append(RequiredSchema(name=t))

            CustomMailbot = build_mailbot(
                computer_use=None,
                model=model,
                local_shell=require_local_shell,
                agent_name=agent_name,
                rule=rule,
                toolkit=toolkit,
                schema=schema,
                image_generation=None,
                web_search=require_web_search,
                rules_file=rules_file,
                queue=queue,
                email_address=email_address,
                toolkit_name=toolkit_name,
                room_rules_paths=room_rules,
                whitelist=whitelist,
                require_storage=require_storage,
                require_read_only_storage=require_read_only_storage,
                require_table_read=require_table_read,
                require_table_write=require_table_write,
                reply_all=reply_all,
                database_namespace=database_namespace,
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
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[str, typer.Option()] = "/agent",
    queue: Annotated[str, typer.Option(..., help="the name of the mail queue")],
    email_address: Annotated[
        str, typer.Option(..., help="the email address of the agent")
    ],
    toolkit_name: Annotated[
        Optional[str],
        typer.Option(..., help="the name of a toolkit to expose mail operations"),
    ],
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
    reply_all: Annotated[bool, typer.Option()] = False,
):
    print("[bold green]Connecting to room...[/bold green]", flush=True)

    service = ServiceHost(host=host, port=port)
    service.add_path(
        path=path,
        cls=build_mailbot(
            queue=queue,
            computer_use=None,
            model=model,
            local_shell=require_local_shell,
            web_search=require_web_search,
            agent_name=agent_name,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            image_generation=None,
            rules_file=rules_file,
            email_address=email_address,
            toolkit_name=toolkit_name,
            room_rules_paths=room_rules,
            whitelist=whitelist,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            reply_all=reply_all,
            database_namespace=database_namespace,
        ),
    )

    await service.run()
