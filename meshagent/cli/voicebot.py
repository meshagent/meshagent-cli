import typer
from rich import print
from typing import Annotated, Optional
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
    resolve_token_jwt,
    resolve_room,
)
from typing import List

from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.services import ServiceHost

try:
    from meshagent.livekit.agents.voice import VoiceBot

except ImportError:

    class VoiceBot:
        def __init__(self, **kwargs):
            raise RoomException(
                "meshagent.livekit module not found, voicebots are not available"
            )


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
    toolkit: Annotated[
        List[str],
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    auto_greet_message: Annotated[Optional[str], typer.Option()] = None,
    auto_greet_prompt: Annotated[Optional[str], typer.Option()] = None,
    token_path: Annotated[Optional[str], typer.Option()] = None,
):
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id, api_key_id)

        room = resolve_room(room)
        jwt = await resolve_token_jwt(
            project_id=project_id,
            api_key_id=api_key_id,
            token_path=token_path,
            name=name,
            role=role,
            room=room,
        )

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

            bot = VoiceBot(
                auto_greet_message=auto_greet_message,
                auto_greet_prompt=auto_greet_prompt,
                name=agent_name,
                requires=requirements,
                rules=rule if len(rule) > 0 else None,
            )

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
    project_id: str = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    toolkit: Annotated[
        List[str],
        typer.Option("--toolkit", "-t", help="the name or url of a required toolkit"),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    auto_greet_message: Annotated[Optional[str], typer.Option()] = None,
    auto_greet_prompt: Annotated[Optional[str], typer.Option()] = None,
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[str, typer.Option()] = "/agent",
):
    requirements = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    service = ServiceHost(host=host, port=port)

    @service.path(path=path)
    class CustomVoiceBot(VoiceBot):
        def __init__(self):
            super().__init__(
                auto_greet_message=auto_greet_message,
                auto_greet_prompt=auto_greet_prompt,
                name=agent_name,
                requires=requirements,
                rules=rule if len(rule) > 0 else None,
            )

    await service.run()
