import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.api import ParticipantToken, ApiScope, RemoteParticipant
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
    resolve_key,
)
from typing import List
from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.services import ServiceHost
from pathlib import Path
from meshagent.agents.config import RulesConfig
import logging

app = async_typer.AsyncTyper(help="Join a voicebot to a room")

logger = logging.getLogger("voicebot")

DEFAULT_TRANSCRIPT_PATH = "transcripts/{participant_name}/{date}/{time}.transcript"


def build_voicebot(
    *,
    agent_name: str,
    rules: list[str],
    rules_file: Optional[str] = None,
    toolkits: list[str],
    schemas: list[str],
    auto_greet_message: Optional[str] = None,
    auto_greet_prompt: Optional[str] = None,
    room_rules_paths: list[str],
    save_transcript_path: Optional[str],
):
    requirements = []

    for t in toolkits:
        requirements.append(RequiredToolkit(name=t))

    for t in schemas:
        requirements.append(RequiredSchema(name=t))

    if rules_file is not None:
        try:
            with open(Path(rules_file).resolve(), "r") as f:
                rules.extend(f.read().splitlines())
        except FileNotFoundError:
            print(f"[yellow]rules file not found at {rules_file}[/yellow]")

    try:
        from meshagent.livekit.agents.voice import VoiceBot
    except ImportError:

        class VoiceBot:
            def __init__(self, **kwargs):
                raise RoomException(
                    "meshagent.livekit module not found, voicebots are not available"
                )

    class CustomVoiceBot(VoiceBot):
        def __init__(self):
            super().__init__(
                auto_greet_message=auto_greet_message,
                auto_greet_prompt=auto_greet_prompt,
                name=agent_name,
                requires=requirements,
                rules=rules if len(rules) > 0 else None,
            )

            self._transcript_template_path = save_transcript_path
            self._transcript_doc = None
            self._transcript_handler = None
            self._transcript_path = None

        async def start(self, *, room: RoomClient):
            await super().start(room=room)

            if room_rules_paths is not None:
                for p in room_rules_paths:
                    await self._load_room_rules(room=room, path=p)

        async def _load_room_rules(
            self,
            *,
            room: RoomClient,
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

        async def get_rules(self, *, participant: RemoteParticipant):
            rules = [*self.rules] if self.rules is not None else []
            if room_rules_paths is not None:
                for p in room_rules_paths:
                    rules.extend(
                        await self._load_room_rules(
                            room=self.room, participant=participant, path=p
                        )
                    )

            logger.info(f"voicebot using rules {rules}")

            return rules

        async def on_session_created(
            self, *, session, context, participant, breakout_room
        ):
            if not self._transcript_template_path:
                return

            path = self._format_transcript_path(
                template=self._transcript_template_path,
                participant=participant,
                agent_name=self.name,
            )
            self._transcript_path = path

            try:
                self._transcript_doc = await self.room.sync.open(path=path, create=True)
            except Exception:
                logger.info("unable to open document for transcription")
                self._transcript_doc = None
                return

            self._transcript_handler = self._attach_transcript_logger(
                session=session,
                doc=self._transcript_doc,
                user_participant=participant,
                agent_name=self.name,  # this should be the agent name we pass in the build context
            )

        async def on_session_ended(
            self, *, session, context, participant, breakout_room
        ):
            if not self._transcript_doc or not self._transcript_path:
                return
            try:
                if session and self._transcript_handler:
                    session.off("conversation_item_added", self._transcript_handler)
            except Exception:
                pass

            # Close the MeshDocument
            try:
                await self.room.sync.close(path=self._transcript_path)
                logger.info("transcript saved at %s", self._transcript_path)
            except Exception as e:
                logger.warning("failed to close transcript doc: %s", e)

    return CustomVoiceBot


@app.async_command("join")
async def make_call(
    *,
    project_id: ProjectIdOption = None,
    room: RoomOption,
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
    auto_greet_message: Annotated[Optional[str], typer.Option()] = None,
    auto_greet_prompt: Annotated[Optional[str], typer.Option()] = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    save_transcript_path: Annotated[
        Optional[str],
        typer.Option(
            "--save-transcript-path",
            "-st",
            help=(
                "Save voice conversation transcript as a MeshDocument at this path template. "
                "Template supports {participant_name}, {participant_id}, "
                "{agent_name}, {date}, {time}."
            ),
        ),
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

        token.add_role_grant(role="agent")
        token.add_room_grant(room)

        CustomVoiceBot = build_voicebot(
            agent_name=agent_name,
            rules=rule,
            rules_file=rules_file,
            toolkits=toolkit,
            schemas=schema,
            auto_greet_message=auto_greet_message,
            auto_greet_prompt=auto_greet_prompt,
            room_rules_paths=room_rules,
            save_transcript_path=save_transcript_path,
        )

        jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)
        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room, base_url=meshagent_base_url()),
                token=jwt,
            )
        ) as client:
            bot = CustomVoiceBot()

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
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    save_transcript_path: Annotated[
        Optional[str],
        typer.Option(
            "--save-transcript-path",
            "-st",
            help=(
                "Save voice conversation transcript as a MeshDocument at this path template. "
                "Template supports {participant_name}, {participant_id}, "
                "{agent_name}, {date}, {time}."
            ),
        ),
    ] = None,
):
    CustomVoiceBot = build_voicebot(
        agent_name=agent_name,
        rules=rule,
        rules_file=rules_file,
        toolkits=toolkit,
        schemas=schema,
        auto_greet_message=auto_greet_message,
        auto_greet_prompt=auto_greet_prompt,
        room_rules_paths=room_rules,
        save_transcript_path=save_transcript_path,
    )

    service = ServiceHost(host=host, port=port)

    service.add_path(path, cls=CustomVoiceBot)

    await service.run()
