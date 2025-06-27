
import typer
from rich import print
from typing import Annotated, Optional
from meshagent.tools import Toolkit
from meshagent.api import RoomClient, ParticipantToken, WebSocketClientProtocol, RoomException
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import get_client, resolve_project_id, resolve_api_key, resolve_token_jwt, resolve_room
from meshagent.agents.chat import ChatBot
from meshagent.openai import OpenAIResponsesAdapter
from meshagent.openai.tools.responses_adapter import LocalShellTool
from meshagent.api.services import ServiceHost

from meshagent.agents.chat import ChatBotThreadOpenAIImageGenerationTool

from typing import List

from meshagent.api import RequiredToolkit, RequiredSchema

app = async_typer.AsyncTyper()

@app.async_command("join")
async def make_call(
    *,
    project_id: str = None,
    room: Annotated[Optional[str], typer.Option()] = None,
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
    name: Annotated[str, typer.Option(..., help="Participant name")] = "cli",
    role: str = "agent",
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    toolkit: Annotated[List[str], typer.Option("--toolkit", "-t", help="the name or url of a required toolkit")] = [],
    schema: Annotated[List[str], typer.Option("--schema", "-s", help="the name or url of a required schema")] = [],
    token_path: Annotated[Optional[str], typer.Option()] = None, 
    image_generation: Annotated[Optional[str], typer.Option(..., help="Name of an image gen provider (openai)")] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        room = resolve_room(room)
        jwt = await resolve_token_jwt(project_id=project_id, api_key_id=api_key_id, token_path=token_path, name=name, role=role, room=room)
        
        print("[bold green]Connecting to room...[/bold green]")
        async with RoomClient(
            protocol=WebSocketClientProtocol(url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                                            token=jwt)
        ) as client:
            
            requirements = []
            
            toolkits = []

            for t in toolkit:
                requirements.append(RequiredToolkit(name=t))
            
            for t in schema:
                requirements.append(RequiredSchema(name=t))

            class CustomChatbot(ChatBot):

                async def get_thread_toolkits(self, *, thread_context, participant):
                    toolkits = await super().get_thread_toolkits(thread_context=thread_context, participant=participant)
                    
                    thread_toolkit = Toolkit(name="thread_toolkit", tools=[])
                    if image_generation != None:
                        if image_generation == "openai":
                            print("adding openai image gen to thread")
                            thread_toolkit.tools.append(ChatBotThreadOpenAIImageGenerationTool(thread_context=thread_context, partial_images=3))
                        else:
                            raise Exception("image-generation must be openai")
                    toolkits.append(thread_toolkit)
                    return toolkits
                
                
            bot = CustomChatbot(
                llm_adapter=OpenAIResponsesAdapter(),
                name=agent_name,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None
            )

            await bot.start(room=client)
            try:
                await client.protocol.wait_for_close()
            except KeyboardInterrupt:
                await bot.stop()
        
    finally:
        await account_client.close()



@app.async_command("service")
async def service(
    *,
    room: Annotated[Optional[str], typer.Option()] = None,
   
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    toolkit: Annotated[List[str], typer.Option("--toolkit", "-t", help="the name or url of a required toolkit")] = [],
    schema: Annotated[List[str], typer.Option("--schema", "-s", help="the name or url of a required schema")] = [],
    image_generation: Annotated[Optional[str], typer.Option(..., help="Name of an image gen provider (openai)")] = None,

    host: Annotated[Optional[str], typer.Option()] = None, 
    port: Annotated[Optional[int], typer.Option()] = None, 
    path: Annotated[str, typer.Option()] = "/agent",
):
    
    room = resolve_room(room)
    
    print("[bold green]Connecting to room...[/bold green]")

    requirements = []
    
    toolkits = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))
    
    for t in schema:
        requirements.append(RequiredSchema(name=t))

    class CustomChatbot(ChatBot):

        async def get_thread_toolkits(self, *, thread_context, participant):
            toolkits = await super().get_thread_toolkits(thread_context=thread_context, participant=participant)
            
            thread_toolkit = Toolkit(name="thread_toolkit", tools=[])
            if image_generation != None:
                if image_generation == "openai":
                    print("adding openai image gen to thread")
                    thread_toolkit.tools.append(ChatBotThreadOpenAIImageGenerationTool(thread_context=thread_context, partial_images=3))
                else:
                    raise Exception("image-generation must be openai")
            toolkits.append(thread_toolkit)
            return toolkits
        

    service = ServiceHost(
        host=host,
        port=port
    )

    @service.path(path=path)
    class CustomChatbot(ChatBot):
        def __init__(self):
            super().__init__(
                llm_adapter=OpenAIResponsesAdapter(),
                name=agent_name,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None
            )

    await service.run()
