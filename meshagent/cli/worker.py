import typer
from rich import print
from typing import Annotated, Optional, List, Type
from pathlib import Path
import logging

from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
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
from meshagent.api.services import ServiceHost

from meshagent.agents.config import RulesConfig
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolkit
from meshagent.tools.database import DatabaseToolkitBuilder, DatabaseToolkitConfig
from meshagent.openai import OpenAIResponsesAdapter
from meshagent.openai.tools.responses_adapter import (
    LocalShellTool,
    ImageGenerationTool,
    WebSearchTool,
)

# Your Worker base (the one you pasted) + adapters
from meshagent.agents.worker import Worker  # adjust import
from meshagent.agents.adapter import LLMAdapter, ToolResponseAdapter  # adjust import

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
    local_shell: bool = False,
    web_search: bool = False,
    require_storage: bool = False,
    require_read_only_storage: bool = False,
    database_namespace: Optional[list[str]] = None,
    require_table_read: list[str] | None = None,
    require_table_write: list[str] | None = None,
    toolkit_name: Optional[str] = None,
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

    llm_adapter: LLMAdapter = OpenAIResponsesAdapter(model=model)

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
            )
            self._room_rules_paths = room_rules_paths or []

        async def start(self, *, room: RoomClient):
            print(
                "[bold green]Worker connected. It will consume queue messages.[/bold green]"
            )
            await super().start(room=room)

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
            # keep your base behavior unless you want to add defaults here
            return super().get_toolkit_builders()

        async def get_thread_toolkits(self, *, thread_context):
            """
            Optional hook if your WorkerBase supports thread contexts.
            If not, you can remove this; I left it to mirror mailbot's pattern.
            """
            toolkits_out = []
            thread_toolkit = Toolkit(name="thread_toolkit", tools=[])

            if local_shell:
                thread_toolkit.tools.append(
                    LocalShellTool(thread_context=thread_context)
                )

            if image_generation is not None:
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

            toolkits_out.append(thread_toolkit)
            return toolkits_out

    return CustomWorker


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the worker agent")],
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
        str, typer.Option(..., help="Name of the LLM model to use")
    ] = "gpt-5.2",
    require_local_shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable local shell tool calling")
    ] = False,
    require_web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
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
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    require_read_only_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable read only storage toolkit")
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
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
):
    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room_name = resolve_room(room)

        token = ParticipantToken(name=agent_name)
        token.add_api_grant(ApiScope.agent_default())
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
                toolkit=toolkit,
                schema=schema,
                rules_file=rules_file,
                room_rules_paths=room_rules,
                queue=queue,
                local_shell=require_local_shell,
                web_search=require_web_search,
                toolkit_name=toolkit_name,
                require_storage=require_storage,
                require_read_only_storage=require_read_only_storage,
                require_table_read=require_table_read,
                require_table_write=require_table_write,
                database_namespace=[database_namespace] if database_namespace else None,
                title=title,
                description=description,
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
    toolkit: Annotated[
        List[str],
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[str, typer.Option(...)] = "gpt-5.2",
    require_local_shell: Annotated[Optional[bool], typer.Option(...)] = False,
    require_web_search: Annotated[Optional[bool], typer.Option(...)] = False,
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[str, typer.Option()] = "/worker",
    queue: Annotated[str, typer.Option(..., help="the queue to consume")],
    toolkit_name: Annotated[Optional[str], typer.Option(...)] = None,
    room_rules: Annotated[List[str], typer.Option("--room-rules", "-rr")] = [],
    require_storage: Annotated[Optional[bool], typer.Option(...)] = False,
    require_read_only_storage: Annotated[Optional[bool], typer.Option(...)] = False,
    database_namespace: Annotated[Optional[str], typer.Option(...)] = None,
    require_table_read: Annotated[list[str], typer.Option(...)] = [],
    require_table_write: Annotated[list[str], typer.Option(...)] = [],
    title: Annotated[
        Optional[str],
        typer.Option(..., help="a display name for the agent"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option(..., help="a description for the agent"),
    ] = None,
):
    print("[bold green]Starting worker service...[/bold green]", flush=True)

    service = ServiceHost(host=host, port=port)

    # Plug in your specific worker implementation here:
    from meshagent.agents.worker import (
        Worker as WorkerBase,
    )  # replace with your concrete worker class

    service.add_path(
        path=path,
        cls=build_worker(
            WorkerBase=WorkerBase,
            model=model,
            agent_name=agent_name,
            rule=rule,
            toolkit=toolkit,
            schema=schema,
            rules_file=rules_file,
            room_rules_paths=room_rules,
            queue=queue,
            local_shell=require_local_shell,
            web_search=require_web_search,
            toolkit_name=toolkit_name,
            require_storage=require_storage,
            require_read_only_storage=require_read_only_storage,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            database_namespace=[database_namespace] if database_namespace else None,
            title=title,
            description=description,
        ),
    )

    await service.run()
