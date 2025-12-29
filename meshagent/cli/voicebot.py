import typer
from rich import print
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.api import RoomClient, WebSocketClientProtocol, RoomException
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.api import ParticipantToken, ApiScope, RemoteParticipant, MeshDocument
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
import re
from collections import defaultdict
from datetime import datetime, timezone
from livekit.agents.llm import ChatMessage
from livekit.agents.voice import ConversationItemAddedEvent 
from meshagent.livekit.agents.voice import VoiceConnection
from meshagent.tools import ToolContext
from livekit.agents import (
    AgentSession,
    RoomInputOptions,
    RoomOutputOptions,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip,
)

app = async_typer.AsyncTyper(help="Join a voicebot to a room")

logger = logging.getLogger("voicebot")

DEFAULT_TRANSCRIPT_PATH= "transcripts/{participant_name}/{date}/{time}.transcript"

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
    save_transcript: bool = False,
    transcript_path: str = DEFAULT_TRANSCRIPT_PATH,
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
            self._save_transcript=save_transcript
            self._transcript_path_template=transcript_path or DEFAULT_TRANSCRIPT_PATH

            super().__init__(
                auto_greet_message=auto_greet_message,
                auto_greet_prompt=auto_greet_prompt,
                name=agent_name,
                requires=requirements,
                rules=rules if len(rules) > 0 else None,
            )

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
        
        @staticmethod
        def _sanitize_for_path(value: str) -> str:
            return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
        
        @staticmethod
        def _iso_utc_from_unix(ts: float) -> str:
            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        
        def _format_transcript_path(self, *, participant: RemoteParticipant) -> str:
            now = datetime.now(timezone.utc)

            placeholders = defaultdict(
                str,
                {
                    "participant_name": self._sanitize_for_path(
                        participant.get_attribute("name") or participant.id
                    ),
                    "participant_id": self._sanitize_for_path(participant.id),
                    "agent_name": self._sanitize_for_path(self.name or "agent"),
                    "date": now.strftime("%Y-%m-%d"),
                    "time": now.strftime("%H-%M-%S"),
                },
            )

            template = self._transcript_path_template or DEFAULT_TRANSCRIPT_PATH
            try:
                return template.format_map(placeholders)
            except Exception:
                logger.warning(
                    "unable to format transcript path using template '%s', falling back to default",
                    template,
                )
                return DEFAULT_TRANSCRIPT_PATH.format_map(placeholders)
        
        def _attach_transcript_logger(
            self,
            *,
            session: AgentSession,
            doc: MeshDocument,
            user_participant: RemoteParticipant,
            agent_name: str,
            agent_participant_id: str = "agent",
        ):
            """
            Attach a listener to AgentSession that adds all user+assistant ChatMessages
            into the Mesh transcript document.
            """

            def _append_segment(*, role: str, text: str, created_at: float):
                if not text:
                    return

                if role == "user":
                    participant_id = user_participant.id
                    # Try to fetch a friendly name from attributes; fall back to id if missing.
                    participant_name = (
                        user_participant.get_attribute("name")
                        or user_participant.id
                    )
                elif role == "assistant":
                    participant_id = agent_participant_id
                    participant_name = agent_name
                else:
                    # skip system / developer / function_call, etc
                    return

                segments = doc.root
                segments.append_child(
                    "segment",
                    {
                        "text": text,
                        "participant_name": participant_name,
                        "participant_id": participant_id,
                        "time": self._iso_utc_from_unix(created_at),
                    },
                )

            def _on_conversation_item(event: ConversationItemAddedEvent):
                item = event.item
                # Only care about ChatMessages (ignore tool calls, etc.)
                if not isinstance(item, ChatMessage):
                    return

                text = item.text_content
                if text is None:
                    return

                # item.role is Literal["developer", "system", "user", "assistant"]
                _append_segment(role=item.role, text=text, created_at=item.created_at)

            # Event name is the "type" defined on ConversationItemAddedEvent
            session.on("conversation_item_added", _on_conversation_item)
            return _on_conversation_item  # so we can detach later if we want
        
        async def run_voice_agent(self, *, participant:RemoteParticipant, breakout_room:str):
            """
            If transcript saving is disabled, fall back to base VoiceBot behavior.
            If enabled, run the voice agent and capture transcript to a MeshDocument.
            """

            if not self._save_transcript:
                return await super().run_voice_agent(participant=participant, breakout_room=breakout_room)

            transcript_path = self._format_transcript_path(participant=participant)
            # Open MeshDocument Transcript
            try: 
                doc = await self.room.sync.open(
                    path=transcript_path,
                    create=True,
                )
            except Exception as e:
                logger.error(
                    "unable to initialize transcript at %s: %s; falling back to base VoiceBot",
                    transcript_path,
                    e,
                    exc_info=e,
                )
                return await super().run_voice_agent(participant=participant, breakout_room=breakout_room)
            
            session = None
            transcript_handler = None

            try:
                async with VoiceConnection(room=self.room, breakout_room=breakout_room) as connection:
                    logger.info("starting voice agent with transcript at %s", transcript_path)

                    context = ToolContext(
                        room=self.room,
                        caller=self.room.local_participant,
                        on_behalf_of=participant,
                    )

                    session = self.create_session(context=context)
                    agent = await self.create_agent(context=context, session=session)

                    transcript_handler = self._attach_transcript_logger(
                        session=session,
                        doc=doc,
                        user_participant=participant,
                        agent_name=self.title or self.name or "Agent",
                        agent_participant_id="agent",  # or self.room.local_participant.id
                    )

                    background_audio = BackgroundAudioPlayer(
                        thinking_sound=[
                            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.3),
                            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.4),
                        ],
                    )
                    await background_audio.start(
                        room=connection.livekit_room,
                        agent_session=session,
                    )

                    await session.start(
                        agent=agent,
                        room=connection.livekit_room,
                        room_input_options=RoomInputOptions(
                            text_enabled=False,
                            delete_room_on_close=False,
                        ),
                        room_output_options=RoomOutputOptions(
                            transcription_enabled=True,
                            audio_enabled=True,
                        ),
                    )

                    if self.auto_greet_prompt is not None:
                        session.generate_reply(user_input=self.auto_greet_prompt)

                    if self.auto_greet_message is not None:
                        session.say(self.auto_greet_message)

                    logger.info("started voice agent")
                    await self._wait_for_disconnect(room=connection.livekit_room)

            finally:
                # detach handler
                if session is not None and transcript_handler is not None:
                    try:
                        session.off("conversation_item_added", transcript_handler)
                    except Exception:
                        pass
                # close transcript doc
                try:
                    await self.room.sync.close(path=transcript_path)
                    logger.info("transcript saved at %s", transcript_path)
                except Exception as close_error:
                    logger.warning(
                        "failed to close transcript %s: %s",
                        transcript_path,
                        close_error,
                        exc_info=close_error,
                    )
                    
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
    save_transcript: Annotated[
        bool,
        typer.Option(
            "--save-transcript/--no-save-transcript",
            help="Save voice conversation transcript as a MeshDocument",
        ),
    ] = False,
    transcript_path: Annotated[
        str,
        typer.Option(
            "--transcript-path-template",
            "-tpt",
            help=(
                "Path template for transcript MeshDocument. Supports "
                "{participant_name}, {participant_id}, {agent_name}, {date}, {time}"
            ),
        ),
    ] = DEFAULT_TRANSCRIPT_PATH,
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
            save_transcript=save_transcript,
            transcript_path=transcript_path
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
    save_transcript: Annotated[
        bool,
        typer.Option(
            "--save-transcript/--no-save-transcript",
            help="Save voice conversation transcript as a MeshDocument",
        ),
    ] = False,
    transcript_path: Annotated[
        str,
        typer.Option(
            "--transcript-path-template",
            "-tpt",
            help=(
                "Path template for transcript MeshDocument. Supports "
                "{participant_name}, {participant_id}, {agent_name}, {date}, {time}"
            ),
        ),
    ] = DEFAULT_TRANSCRIPT_PATH,
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
        save_transcript=save_transcript,
        transcript_path=transcript_path
    )

    service = ServiceHost(host=host, port=port)

    service.add_path(path, cls=CustomVoiceBot)

    await service.run()
