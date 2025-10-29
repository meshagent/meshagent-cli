import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.api import ParticipantToken, ApiScope
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)

from meshagent.api import RequiredSchema
from meshagent.api.services import ServiceHost

app = async_typer.AsyncTyper(help="Join a meeting transcriber to a room")


@app.async_command("join")
async def join(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent")],
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
):
    try:
        from meshagent.livekit.agents.meeting_transcriber import MeetingTranscriber
    except ImportError:

        class MeetingTranscriber:
            def __init__(self, **kwargs):
                raise RoomException(
                    "meshagent.livekit module not found, transcribers are not available"
                )

    key = await resolve_key(project_id=project_id, key=key)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        token = ParticipantToken(
            name=agent_name,
        )

        token.add_api_grant(ApiScope.agent_default())

        token.add_role_grant(role="agent")
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

            requirements.append(RequiredSchema(name="transcript"))

            bot = MeetingTranscriber(
                name=agent_name,
                requires=requirements,
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
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent")],
    host: Annotated[Optional[str], typer.Option()] = None,
    port: Annotated[Optional[int], typer.Option()] = None,
    path: Annotated[str, typer.Option()] = "/agent",
):
    try:
        from meshagent.livekit.agents.meeting_transcriber import MeetingTranscriber
    except ImportError:

        class MeetingTranscriber:
            def __init__(self, **kwargs):
                raise RoomException(
                    "meshagent.livekit module not found, voicebots are not available"
                )

    requirements = []

    requirements.append(RequiredSchema(name="transcript"))

    service = ServiceHost(host=host, port=port)

    @service.path(path=path)
    class CustomMeetingTranscriber(MeetingTranscriber):
        def __init__(self):
            super().__init__(
                name=agent_name,
                requires=requirements,
            )

    await service.run()
