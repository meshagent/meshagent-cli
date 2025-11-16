import typer
from rich import print
from typing import Annotated, Optional
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolkitBuilder
from meshagent.agents.config import RulesConfig

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
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)

from meshagent.openai import OpenAIResponsesAdapter

from typing import List
from pathlib import Path

from meshagent.openai.tools.responses_adapter import (
    WebSearchToolkitBuilder,
    MCPToolkitBuilder,
    WebSearchTool,
    LocalShellConfig,
    WebSearchConfig,
)

from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.services import ServiceHost
import logging
import os.path

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
    computer_use: Optional[str] = None,
    web_search: Optional[str] = None,
    mcp: Optional[str] = None,
    storage: Optional[str] = None,
    require_image_generation: Optional[str] = None,
    require_local_shell: Optional[str] = None,
    require_computer_use: Optional[str] = None,
    require_web_search: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    rules_file: Optional[str] = None,
    room_rules_path: Optional[str] = None,
):
    from meshagent.agents.chat import ChatBot

    from meshagent.tools.storage import StorageToolkit

    from meshagent.agents.chat import (
        ChatBotThreadOpenAIImageGenerationToolkitBuilder,
        ChatBotThreadLocalShellToolkitBuilder,
        ChatBotThreadOpenAIImageGenerationTool,
        ChatBotThreadLocalShellTool,
        ImageGenerationConfig,
    )

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
                rule = rules_config.rules
                client_rules = rules_config.client_rules

        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

    BaseClass = ChatBot
    if computer_use:
        from meshagent.computers.agent import ComputerAgent

        if ComputerAgent is None:
            raise RuntimeError(
                "Computer use is enabled, but meshagent.computers is not installed."
            )
        BaseClass = ComputerAgent
        llm_adapter = OpenAIResponsesAdapter(
            model=model,
            response_options={
                "reasoning": {"generate_summary": "concise"},
                "truncation": "auto",
            },
        )
    else:
        llm_adapter = OpenAIResponsesAdapter(
            model=model,
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
            )

        async def get_rules(self, *, thread_context, participant):
            rules = await super().get_rules(
                thread_context=thread_context, participant=participant
            )

            if room_rules_path is not None:
                for p in room_rules_path:
                    try:
                        room_rules = await self.room.storage.download(path=p)

                        rules_txt = room_rules.data.decode()

                        rules_config = RulesConfig.parse(rules_txt)

                        if rules_config.rules is not None:
                            rules.extend(rules_config.rules)

                        client = participant.get_attribute("client")

                        if rules_config.client_rules is not None and client is not None:
                            cr = rules_config.client_rules.get(client)
                            if cr is not None:
                                rules.extend(cr)

                    except RoomException:
                        try:
                            logger.info("attempting to initialize rules file")
                            handle = await self.room.storage.open(
                                path=p, overwrite=False
                            )
                            await self.room.storage.write(
                                handle=handle,
                                data="# Add rules to this file to customize your agent's behavior, lines starting with # will be ignored.\n\n".encode(),
                            )
                            await self.room.storage.close(handle=handle)

                        except RoomException:
                            pass
                        logger.info(
                            f"unable to load rules from {room_rules_path}, continuing with default rules"
                        )
                        pass

            print(f"using rules {rules}", flush=True)

            return rules

        async def get_thread_toolkits(self, *, thread_context, participant):
            providers = []

            if require_image_generation:
                providers.append(
                    ChatBotThreadOpenAIImageGenerationTool(
                        thread_context=thread_context,
                        config=ImageGenerationConfig(
                            name="image_generation",
                            partial_images=3,
                        ),
                    )
                )

            if require_local_shell:
                providers.append(
                    ChatBotThreadLocalShellTool(
                        thread_context=thread_context,
                        config=LocalShellConfig(name="local_shell"),
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

            tk = await super().get_thread_toolkits(
                thread_context=thread_context, participant=participant
            )
            return [
                *(
                    [Toolkit(name="tools", tools=providers)]
                    if len(providers) > 0
                    else []
                ),
                *tk,
            ]

        async def get_thread_toolkit_builders(self, *, thread_context, participant):
            providers = []

            if image_generation:
                providers.append(
                    ChatBotThreadOpenAIImageGenerationToolkitBuilder(
                        thread_context=thread_context
                    )
                )

            if local_shell:
                providers.append(
                    ChatBotThreadLocalShellToolkitBuilder(thread_context=thread_context)
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
    project_id: ProjectIdOption = None,
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
    ] = "gpt-5.1",
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
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
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
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
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
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
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

            CustomChatbot = build_chatbot(
                computer_use=computer_use,
                model=model,
                local_shell=local_shell,
                agent_name=agent_name,
                rule=rule,
                toolkit=toolkit,
                schema=schema,
                rules_file=rules_file,
                image_generation=image_generation,
                web_search=web_search,
                mcp=mcp,
                storage=storage,
                require_web_search=require_web_search,
                require_local_shell=require_local_shell,
                require_image_generation=require_image_generation,
                require_mcp=require_mcp,
                require_storage=require_storage,
                room_rules_path=room_rules,
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
    ] = "gpt-5.1",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
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
        Optional[str], typer.Option(..., help="Name of an image gen model", hidden=True)
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
        typer.Option(..., help="Enable local shell tool calling", hidden=True),
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
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[str, typer.Option()] = "/agent",
):
    print("[bold green]Connecting to room...[/bold green]", flush=True)

    service = ServiceHost(host=host, port=port)
    service.add_path(
        path=path,
        cls=build_chatbot(
            computer_use=computer_use,
            model=model,
            local_shell=local_shell,
            agent_name=agent_name,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            web_search=web_search,
            image_generation=image_generation,
            mcp=mcp,
            storage=storage,
            require_web_search=require_web_search,
            require_local_shell=require_local_shell,
            require_image_generation=require_image_generation,
            require_mcp=require_mcp,
            require_storage=require_storage,
            room_rules_path=room_rules,
        ),
    )

    await service.run()
