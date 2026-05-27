from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional
from urllib.parse import urlparse

import typer
import yaml
from aiohttp import web
from rich import print

from meshagent.agents import (
    MessagingChatChannel,
    SingleRoomAgent,
    WebSocketChatChannel,
)
from meshagent.agents.messages import (
    AGENT_EVENT_MODEL_CHANGED,
    AGENT_EVENT_THREAD_LOADED,
    AGENT_EVENT_THREAD_LISTED,
    AGENT_EVENT_THREAD_STARTED,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_MESSAGE_THREAD_OPEN,
    AGENT_MESSAGE_THREAD_LIST,
    AGENT_MESSAGE_MODELS_RESPONSE,
    AgentMessage,
    AgentModelChanged,
    AgentModelInfo,
    AgentProviderInfo,
    AgentTextContentDelta,
    AgentThreadListEntry,
    ListThreads,
    ModelsResponse,
    OpenThread,
    StartThread,
    ThreadCreated,
    ThreadDeleted,
    ThreadLoaded,
    ThreadStarted,
    ThreadUpdated,
    ThreadsListed,
)
from meshagent.agents.chat_client import (
    ChatThreadSession,
    LocalChatClient,
    MessagingChatClient,
    WebSocketChatClient,
)
from meshagent.agents.process import Message
from meshagent.api import (
    ApiScope,
    Participant,
    ParticipantToken,
    RoomClient,
    RoomException,
    WebSocketClientProtocol,
)
from meshagent.api.client import ConflictError
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import (
    cleanup_args,
    cleanup_args_strip_options,
    get_client,
    resolve_key,
    resolve_project_id,
    resolve_room,
)
from meshagent.cli.host import get_deferred, get_service, run_services, service_specs
from meshagent.cli.thread_sidebar import (
    ThreadSidebar,
    sort_thread_entries,
    thread_list_entry_from_agent_entry,
    thread_list_event_from_agent_payload,
)
from meshagent.codex import AppServerConfig, CodexAgentSupervisor, DEFAULT_CODEX_MODEL

logger = logging.getLogger("codex_cli")

app = async_typer.AsyncTyper(help="Join a Codex-backed agent to a room")

CodexConfigOption = Annotated[
    list[str],
    typer.Option(
        "--codex-config",
        help=(
            "Pass a Codex config override as key=value. "
            "Can be repeated and is forwarded to codex app-server."
        ),
    ),
]

CodexBinOption = Annotated[
    Optional[str],
    typer.Option(
        "--codex-bin",
        help="Path to a Codex binary. Defaults to the bundled pinned runtime.",
    ),
]

WorkingDirOption = Annotated[
    Optional[str],
    typer.Option(
        "--working-dir",
        help="Working directory for Codex turns.",
    ),
]

CodexChannelOption = Annotated[
    list[str],
    typer.Option(
        "--channel",
        help=(
            "Attach a channel to the Codex agent. "
            "Can be repeated. Currently supported: chat, websocket:PORT, "
            "websocket://HOST:PORT."
        ),
    ),
]

ThreadDirOption = Annotated[
    Optional[str],
    typer.Option(
        "--thread-dir",
        help="Thread directory annotation for chat and websocket channels.",
    ),
]
WebSocketAuthMode = Literal["jwt", "iap", "none"]


@dataclass(frozen=True, slots=True)
class _WebSocketChannelConfig:
    host: str
    port: int


@dataclass(slots=True)
class _WebSocketChannelServer:
    runner: web.AppRunner

    async def stop(self) -> None:
        await self.runner.cleanup()


class _LocalEventCodexSupervisor(CodexAgentSupervisor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._local_event_queues: list[asyncio.Queue[Message]] = []

    def subscribe_local_events(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self._local_event_queues.append(queue)
        return queue

    def unsubscribe_local_events(self, queue: asyncio.Queue[Message]) -> None:
        if queue in self._local_event_queues:
            self._local_event_queues.remove(queue)

    def _send_to_local_event_queues(self, message: Message) -> None:
        for queue in [*self._local_event_queues]:
            queue.put_nowait(message)

    def _send_to_channels(self, message: Message) -> None:
        if isinstance(message.data, ThreadsListed):
            self._send_to_local_event_queues(message)
        elif message.source is None and message.data.type in {
            AGENT_EVENT_MODEL_CHANGED,
            AGENT_EVENT_THREAD_STATUS,
        }:
            self._send_to_local_event_queues(message)
        super()._send_to_channels(message)

    async def _route(self, message: Message) -> None:
        if message.data.type == AGENT_MESSAGE_THREAD_LIST:
            list_threads = ListThreads.model_validate(
                message.data.model_dump(mode="python")
            )
            page = await self.list_threads(
                list_threads=list_threads, sender=message.sender
            )
            response = ThreadsListed(
                type=AGENT_EVENT_THREAD_LISTED,
                source_message_id=list_threads.message_id,
                threads=[
                    AgentThreadListEntry(
                        path=entry.path,
                        name=entry.name,
                        created_at=entry.created_at,
                        modified_at=entry.modified_at,
                    )
                    for entry in page.threads
                ],
                total=page.total,
                offset=page.offset,
                limit=page.limit,
            )
            response_message = Message(data=response, sender=message.sender)
            self._send_to_local_event_queues(response_message)
            super()._send_to_channels(response_message)
            return
        await super()._route(message)

    def send(self, message: Message) -> None:
        if message.source is not None:
            self._send_to_local_event_queues(message)
        super().send(message)

    def _send_thread_list_event(
        self,
        *,
        payload: ThreadCreated | ThreadUpdated | ThreadDeleted,
        sender: Participant | None,
    ) -> None:
        self._send_to_local_event_queues(Message(data=payload, sender=sender))
        super()._send_thread_list_event(payload=payload, sender=sender)

    def _emit_thread_started(
        self,
        *,
        start_thread: StartThread,
        sender: Participant | None,
        thread_id: str,
        realtime_connection: Any = None,
    ) -> None:
        thread_started = ThreadStarted(
            type=AGENT_EVENT_THREAD_STARTED,
            source_message_id=start_thread.message_id,
            thread_id=thread_id,
            realtime_connection=realtime_connection,
        )
        self._send_to_local_event_queues(Message(data=thread_started, sender=sender))
        self._send_to_channels(Message(data=thread_started, sender=sender))


def _codex_config(
    *,
    codex_bin: str | None,
    working_dir: str | None,
    codex_config: list[str],
) -> AppServerConfig:
    return AppServerConfig(
        codex_bin=codex_bin,
        cwd=working_dir,
        config_overrides=tuple(item for item in codex_config if item.strip() != ""),
        client_name="meshagent_codex",
        client_title="MeshAgent Codex",
    )


def _parse_websocket_channel(*, channel: str) -> _WebSocketChannelConfig:
    if channel[:10].casefold() != "websocket:":
        raise typer.BadParameter(f"unsupported websocket channel: {channel}")

    host = "0.0.0.0"
    port: int | None = None
    if channel[:12].casefold() == "websocket://":
        parsed = urlparse(channel)
        host = parsed.hostname or host
        if parsed.path not in ("", "/"):
            raise typer.BadParameter(
                "websocket channels must be passed as --channel=websocket:PORT "
                "or --channel=websocket://HOST:PORT"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise typer.BadParameter(
                f"invalid websocket channel port: {channel}"
            ) from exc
    else:
        port_text = channel[10:].strip()
        if port_text == "":
            raise typer.BadParameter(
                "websocket channels must be passed as --channel=websocket:PORT "
                "or --channel=websocket://HOST:PORT"
            )
        try:
            port = int(port_text)
        except ValueError as exc:
            raise typer.BadParameter(
                f"invalid websocket channel port: {port_text}"
            ) from exc

    if port is None:
        raise typer.BadParameter(
            "websocket channels must include a port, for example "
            "--channel=websocket:8080"
        )
    if port < 1 or port > 65535:
        raise typer.BadParameter("websocket channel port must be between 1 and 65535")

    return _WebSocketChannelConfig(host=host, port=port)


def _websocket_channel_key(config: _WebSocketChannelConfig) -> str:
    host = config.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"websocket://{host}:{config.port}"


def _is_websocket_channel(channel: str) -> bool:
    return channel[:12].casefold() == "websocket://"


def _has_chat_channel(*, channels: list[str]) -> bool:
    return "chat" in channels


def _resolved_codex_channels(*, channel: list[str]) -> list[str]:
    channels: list[str] = []
    seen_channels: set[str] = set()
    for item in channel or []:
        normalized = item.strip()
        if normalized == "":
            continue
        if normalized.casefold() == "chat":
            if "chat" not in seen_channels:
                seen_channels.add("chat")
                channels.append("chat")
            continue
        if normalized[:10].casefold() == "websocket:":
            websocket_config = _parse_websocket_channel(channel=normalized)
            channel_key = _websocket_channel_key(websocket_config)
            if channel_key not in seen_channels:
                seen_channels.add(channel_key)
                channels.append(channel_key)
            continue
        raise typer.BadParameter(
            "codex only supports chat and websocket channels; "
            f"unsupported channel: {normalized}"
        )
    return channels


def _require_codex_channels(*, channels: list[str]) -> None:
    if len(channels) > 0:
        return
    print("[bold red]at least one channel is required for codex agents[/bold red]")
    raise typer.Exit(1)


def _require_resolved_room(room: str | None) -> str:
    if room is None or room.strip() == "":
        print("[bold red]--room is required (or set MESHAGENT_ROOM)[/bold red]")
        raise typer.Exit(1)
    return room.strip()


def _codex_run_websocket_channel(
    *,
    channels: list[str],
) -> _WebSocketChannelConfig | None:
    if _has_chat_channel(channels=channels):
        return None
    for channel in channels:
        if _is_websocket_channel(channel):
            return _parse_websocket_channel(channel=channel)
    return None


def _normalize_codex_use_websocket_url(websocket_url: str) -> str:
    normalized = websocket_url.strip()
    if normalized == "":
        raise typer.BadParameter("--websocket-url cannot be empty")
    parsed = urlparse(normalized)
    if parsed.scheme not in ("ws", "wss") or parsed.netloc == "":
        raise typer.BadParameter("--websocket-url must be a ws:// or wss:// URL")
    return normalized


def _websocket_client_host(host: str) -> str:
    normalized = host.strip()
    if normalized in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return normalized


def _websocket_client_url(config: _WebSocketChannelConfig) -> str:
    host = _websocket_client_host(config.host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"ws://{host}:{config.port}/messages"


def _websocket_iap_cookie_headers(*, token: str | None) -> dict[str, str]:
    if token is None or token.strip() == "":
        raise typer.BadParameter(
            "a room participant token is required for --websocket-auth=iap"
        )
    return {"Cookie": f"__meshagent_iap={token.strip()}"}


def _local_codex_participant_name(room: RoomClient) -> str | None:
    local_participant = room.local_participant
    name = local_participant.get_attribute("name")
    if isinstance(name, str) and name.strip() != "":
        return name.strip()
    if isinstance(local_participant.id, str) and local_participant.id.strip() != "":
        return local_participant.id.strip()
    return None


def _codex_run_websocket_headers(
    *,
    room: RoomClient,
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
) -> dict[str, str]:
    normalized_user = user.strip()
    if normalized_user == "":
        raise typer.BadParameter("--user cannot be empty")
    if websocket_auth == "none":
        return {}
    if websocket_auth == "iap":
        return _websocket_iap_cookie_headers(token=iap_token)

    secret = os.getenv("MESHAGENT_SECRET")
    if secret is None or secret == "":
        raise typer.BadParameter(
            "MESHAGENT_SECRET is required for --websocket-auth=jwt"
        )
    local_agent_name = _local_codex_participant_name(room)
    if local_agent_name is None:
        raise typer.BadParameter("local codex participant name is unavailable")
    token = ParticipantToken(name=normalized_user)
    token.add_agent_grant(local_agent_name)
    return {"Authorization": f"Bearer {token.to_jwt(token=secret)}"}


def _codex_use_websocket_headers(
    *,
    agent_name: str,
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
) -> dict[str, str]:
    normalized_user = user.strip()
    if normalized_user == "":
        raise typer.BadParameter("--user cannot be empty")
    normalized_agent_name = agent_name.strip()
    if normalized_agent_name == "":
        raise typer.BadParameter("--agent-name cannot be empty")
    if websocket_auth == "none":
        return {}
    if websocket_auth == "iap":
        return _websocket_iap_cookie_headers(token=iap_token)

    secret = os.getenv("MESHAGENT_SECRET")
    if secret is None or secret == "":
        raise typer.BadParameter(
            "MESHAGENT_SECRET is required for --websocket-auth=jwt"
        )
    token = ParticipantToken(name=normalized_user)
    token.add_agent_grant(normalized_agent_name)
    return {"Authorization": f"Bearer {token.to_jwt(token=secret)}"}


def _token_from_websocket_protocol_header(request: web.Request) -> str | None:
    protocols = request.headers.get("Sec-WebSocket-Protocol")
    if protocols is None:
        return None
    for protocol in protocols.split(","):
        normalized = protocol.strip()
        if normalized[:16].casefold() == "meshagent-agent.":
            return normalized[16:]
        if normalized[:16].casefold() == "meshagent-token.":
            return normalized[16:]
        if normalized[:7].casefold() == "bearer.":
            return normalized[7:]
    return None


def _participant_token_from_websocket_request(request: web.Request) -> str:
    auth_header = request.headers.get("Authorization")
    if auth_header is not None:
        auth_header = auth_header.strip()
        if auth_header[:7].casefold() == "bearer " and auth_header[7:].strip() != "":
            return auth_header[7:].strip()
        raise web.HTTPUnauthorized(text="websocket Authorization must use Bearer token")

    token = request.query.get("token")
    if token is not None and token.strip() != "":
        return token.strip()

    token = _token_from_websocket_protocol_header(request)
    if token is not None and token.strip() != "":
        return token.strip()

    raise web.HTTPUnauthorized(text="websocket participant token is required")


def _has_matching_agent_grant(
    *,
    token: ParticipantToken,
    agent_name: str,
) -> bool:
    for grant in token.grants:
        if grant.name == "agent" and grant.scope == agent_name:
            return True
    return False


def _authorize_codex_websocket_request(
    *,
    request: web.Request,
    room: RoomClient,
    websocket_auth: WebSocketAuthMode,
) -> Participant:
    if websocket_auth == "none":
        return Participant(
            id="websocket-user",
            attributes={"name": "websocket-user", "role": "user"},
        )
    if websocket_auth == "iap":
        user_name = request.headers.get("X-MESHAGENT-USER")
        if user_name is None or user_name.strip() == "":
            raise web.HTTPUnauthorized(text="X-MESHAGENT-USER header is required")
        user_name = user_name.strip()
        return Participant(id=user_name, attributes={"name": user_name, "role": "user"})

    secret = os.getenv("MESHAGENT_SECRET")
    if secret is None or secret == "":
        raise web.HTTPServiceUnavailable(text="MESHAGENT_SECRET is required")

    local_agent_name = _local_codex_participant_name(room)
    if local_agent_name is None:
        raise web.HTTPServiceUnavailable(text="local participant name is unavailable")

    try:
        token = ParticipantToken.from_jwt(
            token=_participant_token_from_websocket_request(request),
            secret=secret,
        )
    except Exception as exc:
        raise web.HTTPUnauthorized(text="invalid websocket participant token") from exc

    if not _has_matching_agent_grant(token=token, agent_name=local_agent_name):
        raise web.HTTPForbidden(text="token is missing the required agent grant")

    return Participant(
        id=token.name, attributes={"name": token.name, "role": token.role}
    )


async def _codex_websocket_channel_healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def _start_codex_websocket_channel_server(
    *,
    config: _WebSocketChannelConfig,
    channel: WebSocketChatChannel,
) -> _WebSocketChannelServer:
    app = web.Application()
    app.router.add_get("/healthz", _codex_websocket_channel_healthz)
    app.router.add_get("/messages", channel.websocket_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, config.host, config.port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    return _WebSocketChannelServer(runner=runner)


def _build_codex_agent(
    *,
    model: str | None,
    codex_bin: str | None,
    codex_config: list[str],
    working_dir: str | None,
    channels: list[str],
    websocket_auth: WebSocketAuthMode = "jwt",
    thread_dir: str | None = None,
):
    class CodexRoomAgent(SingleRoomAgent):
        def __init__(self) -> None:
            super().__init__()
            self._supervisor: _LocalEventCodexSupervisor | None = None
            self._chat_channel: MessagingChatChannel | None = None
            self._websocket_channels: list[WebSocketChatChannel] = []
            self._websocket_channel_servers = []

        async def start(self, *, room: RoomClient) -> None:
            if self._room is not None:
                raise RuntimeError("agent is already started")

            self._room = room
            started_servers = []
            supervisor: _LocalEventCodexSupervisor | None = None
            try:
                await self.install_requirements()
                if _has_chat_channel(channels=channels):
                    self._chat_channel = MessagingChatChannel(
                        room=room,
                        threading_mode="default-new",
                        thread_dir=thread_dir,
                    )
                self._websocket_channels = []
                for channel_spec in channels:
                    if not _is_websocket_channel(channel_spec):
                        continue
                    self._websocket_channels.append(
                        WebSocketChatChannel(
                            room=room,
                            authorize=lambda request: (
                                _authorize_codex_websocket_request(
                                    request=request,
                                    room=room,
                                    websocket_auth=websocket_auth,
                                )
                            ),
                            threading_mode="default-new",
                            thread_dir=thread_dir,
                        )
                    )

                supervisor = _LocalEventCodexSupervisor(
                    participant=room.local_participant,
                    config=_codex_config(
                        codex_bin=codex_bin,
                        working_dir=working_dir,
                        codex_config=codex_config,
                    ),
                    default_model=model,
                )
                if self._chat_channel is not None:
                    supervisor.add_channel(self._chat_channel)
                for websocket_channel in self._websocket_channels:
                    supervisor.add_channel(websocket_channel)
                await supervisor.start()
                self._supervisor = supervisor

                for channel_spec, websocket_channel in zip(
                    [item for item in channels if _is_websocket_channel(item)],
                    self._websocket_channels,
                ):
                    websocket_config = _parse_websocket_channel(channel=channel_spec)
                    server = await _start_codex_websocket_channel_server(
                        config=websocket_config,
                        channel=websocket_channel,
                    )
                    started_servers.append(server)
                    print(
                        "[bold green]WebSocket channel listening on "
                        f"ws://{websocket_config.host}:{websocket_config.port}[/bold green]",
                        flush=True,
                    )
                self._websocket_channel_servers = started_servers
            except Exception:
                for server in reversed(started_servers):
                    await server.stop()
                if supervisor is not None:
                    with contextlib.suppress(Exception):
                        await supervisor.stop()
                self._supervisor = None
                self._chat_channel = None
                self._websocket_channels = []
                self._room = None
                raise

        async def stop(self) -> None:
            for server in reversed(self._websocket_channel_servers):
                await server.stop()
            self._websocket_channel_servers = []
            supervisor = self._supervisor
            self._supervisor = None
            if supervisor is not None:
                await supervisor.stop()
            self._chat_channel = None
            self._websocket_channels = []
            await super().stop()

    return CodexRoomAgent


def _codex_agent_annotations(
    *,
    thread_dir: str | None,
    channels: list[str],
) -> dict[str, str]:
    annotations = {ANNOTATION_AGENT_TYPE: "Codex"}
    if _has_chat_channel(channels=channels):
        annotations["meshagent.chatbot.threading"] = "default-new"
        if thread_dir is not None and thread_dir.strip() != "":
            normalized_thread_dir = thread_dir.strip().rstrip("/")
            annotations["meshagent.chatbot.thread-dir"] = normalized_thread_dir
            annotations["meshagent.chatbot.thread-list"] = (
                f"{normalized_thread_dir}/index.threadl"
            )
    return annotations


async def _connect_agent_room(
    *,
    project_id: str | None,
    room: str,
    role: str,
    agent_name: str | None,
    key: str | None,
    token_from_env: str | None = None,
) -> tuple[Any, str, str]:
    resolved_key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    project = await resolve_project_id(project_id=project_id)
    token_env = token_from_env or "MESHAGENT_TOKEN"
    jwt = os.getenv(token_env)
    if jwt is None:
        if token_from_env:
            print(f"[bold red]{token_env} environment variable is not set[/bold red]")
            await account_client.close()
            raise typer.Exit(1)
        if agent_name is None:
            print(
                f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
            )
            await account_client.close()
            raise typer.Exit(1)
        token = ParticipantToken(name=agent_name)
        token.add_api_grant(ApiScope.agent_default())
        token.add_role_grant(role=role)
        token.add_room_grant(room)
        jwt = token.to_jwt(api_key=resolved_key)
    return account_client, project, jwt


async def _await_cleanup(awaitable: Awaitable[Any], *, label: str) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("Codex cleanup failed: %s", label)


async def _run_agent_room_session(
    *,
    client: RoomClient,
    bot: Any,
    runner: Callable[[RoomClient], Awaitable[None]],
) -> None:
    client_entered = False
    bot_started = False
    try:
        await client.__aenter__()
        client_entered = True
        await bot.start(room=client)
        bot_started = True
        await runner(client)
    finally:
        if bot_started:
            await _await_cleanup(bot.stop(), label="agent stop")
        if client_entered:
            await _await_cleanup(
                client.__aexit__(None, None, None),
                label="room client close",
            )


def _codex_model_changed(
    *,
    model: str,
    thread_id: str,
) -> AgentModelChanged:
    return AgentModelChanged(
        type=AGENT_EVENT_MODEL_CHANGED,
        thread_id=thread_id,
        provider="codex",
        model=model,
        source_message_id=None,
        output_modalities=["text"],
    )


def _codex_models_response(*, model: str) -> ModelsResponse:
    return ModelsResponse(
        type=AGENT_MESSAGE_MODELS_RESPONSE,
        source_message_id="configured-codex-models",
        providers=[
            AgentProviderInfo(
                name="codex",
                friendly_name="Codex",
                default_model=model,
                models=[
                    AgentModelInfo(
                        name=model,
                        friendly_name=model,
                        modalities=["text"],
                        active=True,
                    )
                ],
            )
        ],
    )


async def _open_codex_run_websocket_chat_session(
    *,
    room: RoomClient,
    websocket_config: _WebSocketChannelConfig,
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
    thread_path: str | None,
) -> ChatThreadSession:
    chat_client = WebSocketChatClient(
        url=_websocket_client_url(websocket_config),
        headers=_codex_run_websocket_headers(
            room=room,
            user=user,
            websocket_auth=websocket_auth,
            iap_token=iap_token,
        ),
    )
    try:
        await chat_client.__aenter__()
        local_participant_name = user.strip()
        if thread_path is None or thread_path.strip() == "":
            return ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
            )
        return ChatThreadSession(
            client=chat_client,
            thread_path=thread_path.strip(),
            local_participant_name=local_participant_name,
            close_client_on_close=True,
        )
    except Exception:
        await chat_client.__aexit__(None, None, None)
        raise


async def _open_codex_use_chat_session(
    *,
    account_client: Any,
    project_id: str,
    room: str,
    participant_name: str,
    thread_path: str | None,
) -> tuple[RoomClient, ChatThreadSession]:
    connection = await account_client.connect_room(project_id=project_id, room=room)
    user_client = RoomClient(
        protocol_factory=WebSocketClientProtocol(
            url=websocket_room_url(room_name=room),
            token=connection.jwt,
        ).create_factory(),
    )
    chat_client: MessagingChatClient | None = None
    chat_session: ChatThreadSession | None = None
    try:
        await user_client.__aenter__()
        local_participant_name = user_client.local_participant.get_attribute("name")
        chat_client = MessagingChatClient(
            room=user_client,
            participant_name=participant_name,
        )
        await chat_client.__aenter__()
        if thread_path is None or thread_path.strip() == "":
            chat_session = ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=(
                    local_participant_name
                    if isinstance(local_participant_name, str)
                    else None
                ),
                close_client_on_close=True,
            )
        else:
            chat_session = ChatThreadSession(
                client=chat_client,
                thread_path=thread_path.strip(),
                local_participant_name=(
                    local_participant_name
                    if isinstance(local_participant_name, str)
                    else None
                ),
                close_client_on_close=True,
            )
        return user_client, chat_session
    except Exception:
        if chat_session is not None:
            await chat_session.close(close_client=True)
        elif chat_client is not None:
            await chat_client.__aexit__(None, None, None)
        await user_client.__aexit__(None, None, None)
        raise


async def _open_codex_use_websocket_chat_session(
    *,
    websocket_url: str,
    agent_name: str,
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
    thread_path: str | None,
) -> ChatThreadSession:
    chat_client = WebSocketChatClient(
        url=_normalize_codex_use_websocket_url(websocket_url),
        headers=_codex_use_websocket_headers(
            agent_name=agent_name,
            user=user,
            websocket_auth=websocket_auth,
            iap_token=iap_token,
        ),
    )
    try:
        await chat_client.__aenter__()
        if thread_path is None or thread_path.strip() == "":
            return ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=user.strip(),
                close_client_on_close=True,
            )
        return ChatThreadSession(
            client=chat_client,
            thread_path=thread_path.strip(),
            local_participant_name=user.strip(),
            close_client_on_close=True,
        )
    except Exception:
        await chat_client.__aexit__(None, None, None)
        raise


async def _close_codex_chat_session(session: ChatThreadSession | None) -> None:
    if session is None:
        return
    with contextlib.suppress(Exception):
        await session.close(close_client=True)


async def _close_codex_room_client(client: RoomClient | None) -> None:
    if client is None:
        return
    with contextlib.suppress(Exception):
        await client.__aexit__(None, None, None)


async def _open_codex_thread_and_wait(
    session: ChatThreadSession,
    *,
    load: bool = True,
    since_turn: str | None = None,
    timeout: float = 30,
) -> None:
    if not session.has_thread_path:
        return
    payload = OpenThread(
        type=AGENT_MESSAGE_THREAD_OPEN,
        thread_id=session.thread_path,
        load=load,
        since_turn=since_turn,
    )
    await session.send(payload)
    async with asyncio.timeout(timeout):
        while True:
            event = await session.receive()
            if event.get("type") != AGENT_EVENT_THREAD_LOADED:
                continue
            loaded = ThreadLoaded.model_validate(event)
            if (
                loaded.thread_id == session.thread_path
                and loaded.source_message_id == payload.message_id
            ):
                return


async def _run_codex_chat_tui(
    *,
    title: str,
    model: str,
    session: ChatThreadSession,
    working_dir: str | None = None,
    message: str | None = None,
    agent_name: str | None = None,
) -> None:
    from meshagent.cli import ask as ask_module

    thread_generation = 0

    def _current_session() -> ChatThreadSession:
        return session_holder["session"]

    session_holder = {"session": session}

    def _select_configured_model() -> None:
        current = _current_session()
        thread_id = current.thread_path if current.has_thread_path else "new"
        current.select_model(_codex_model_changed(model=model, thread_id=thread_id))
        current.apply_models_response(_codex_models_response(model=model))

    async def _new_thread() -> None:
        nonlocal thread_generation
        current = _current_session()
        new_session = ChatThreadSession(
            client=current.client,
            thread_path=None,
            local_participant_name=current.local_participant_name,
            close_client_on_close=False,
        )
        await current.close(close_client=False)
        session_holder["session"] = new_session
        _select_configured_model()
        thread_generation += 1

    async def _switch_thread(path: str) -> None:
        nonlocal thread_generation
        current = _current_session()
        normalized_path = path.strip()
        if normalized_path == "":
            return
        if current.has_thread_path and current.thread_path == normalized_path:
            return
        new_session = ChatThreadSession(
            client=current.client,
            thread_path=normalized_path,
            local_participant_name=current.local_participant_name,
            close_client_on_close=False,
        )
        await _open_codex_thread_and_wait(new_session, load=True)
        await current.close(close_client=False)
        session_holder["session"] = new_session
        _select_configured_model()
        thread_generation += 1

    async def _list_threads():
        response = await _current_session().list_threads(limit=100, offset=0)
        return sort_thread_entries(
            [thread_list_entry_from_agent_entry(entry) for entry in response.threads]
        )

    def _subscribe_thread_events(
        callback: Callable[[Any], None],
    ) -> Callable[[], None]:
        def _handle_payload(payload: dict[str, Any]) -> None:
            event = thread_list_event_from_agent_payload(payload)
            if event is not None:
                callback(event)

        return _current_session().client.add_event_listener(_handle_payload)

    def _current_thread_path() -> str | None:
        current = _current_session()
        return current.thread_path if current.has_thread_path else None

    async def _handle_command(command: str) -> str | None:
        if command.strip() == "/new":
            await _new_thread()
            return "New thread"
        if command.strip() in ("/model", f"/model {model}"):
            _select_configured_model()
            return f"Using {model}"
        return None

    _select_configured_model()
    if session.has_thread_path:
        await _open_codex_thread_and_wait(session, load=True)

    if message is not None:

        def _write_message(agent_message: AgentMessage) -> None:
            if isinstance(agent_message, AgentTextContentDelta):
                typer.echo(agent_message.text, nl=False)

        await _current_session().ask(prompt=message, on_message=_write_message)
        typer.echo()
        return

    sidebar = ThreadSidebar(
        list_threads=_list_threads,
        subscribe_thread_events=_subscribe_thread_events,
        current_thread_path=_current_thread_path,
        switch_thread=_switch_thread,
        delete_thread=lambda path: _current_session().delete_thread(path),
        rename_thread=lambda path, name: _current_session().rename_thread(path, name),
    )
    await sidebar.start()
    try:
        await ask_module._run_ask_tui(
            model=model,
            session=_current_session(),
            session_provider=_current_session,
            thread_generation_provider=lambda: thread_generation,
            current_working_directory=working_dir,
            title=title,
            assistant_name=agent_name or "codex",
            command_handler=_handle_command,
            model_label_provider=lambda: model,
            side_panel_renderer=sidebar.render,
            side_panel_key_handler=sidebar.handle_key,
            side_panel_mouse_handler=sidebar.handle_click,
        )
    finally:
        await sidebar.close()


async def _run_codex_run_tui(
    *,
    bot: Any,
    model: str,
    thread_path: str | None,
    agent_name: str | None,
    message: str | None,
    working_dir: str | None,
    chat_client: ChatThreadSession | None = None,
) -> None:
    channel_client: LocalChatClient | None = None
    channel_started = False
    if chat_client is None:
        supervisor = bot._supervisor
        events = supervisor.subscribe_local_events()
        channel_client = LocalChatClient(
            thread_path=thread_path.strip()
            if thread_path is not None and thread_path.strip() != ""
            else None,
            send_message=supervisor.send,
            events=events,
            on_close=lambda: supervisor.unsubscribe_local_events(events),
            local_participant_name="you",
        )
        chat_client = channel_client.thread_session
    try:
        if channel_client is not None:
            await channel_client.start()
            channel_started = True
        await _run_codex_chat_tui(
            title="meshagent codex run",
            model=model,
            session=chat_client,
            working_dir=working_dir,
            message=message,
            agent_name=agent_name,
        )
    finally:
        await chat_client.close(close_client=False)
        if channel_client is not None and channel_started:
            await channel_client.close()


async def _run_codex_use_tui(
    *,
    account_client: Any | None,
    project_id: str,
    room: str,
    agent_name: str,
    thread_path: str | None,
    message: str | None,
    websocket_url: str | None = None,
    user: str = "you",
    websocket_auth: WebSocketAuthMode = "jwt",
    iap_token: str | None = None,
) -> None:
    user_client: RoomClient | None = None
    chat_client: ChatThreadSession | None = None
    try:
        if websocket_url is None:
            if account_client is None:
                raise RoomException("codex use account client is unavailable")
            user_client, chat_client = await _open_codex_use_chat_session(
                account_client=account_client,
                project_id=project_id,
                room=room,
                participant_name=agent_name,
                thread_path=thread_path,
            )
        else:
            chat_client = await _open_codex_use_websocket_chat_session(
                websocket_url=websocket_url,
                agent_name=agent_name,
                user=user,
                websocket_auth=websocket_auth,
                iap_token=iap_token,
                thread_path=thread_path,
            )
        await _run_codex_chat_tui(
            title=f"meshagent codex use: {agent_name}",
            model="remote",
            session=chat_client,
            message=message,
            agent_name=agent_name,
        )
    finally:
        await _close_codex_chat_session(chat_client)
        await _close_codex_room_client(user_client)


@app.async_command("join", help="Join a room and run a Codex-backed agent.")
async def join(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    token_from_env: Annotated[
        Optional[str],
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token",
        ),
    ] = None,
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Default Codex model")
    ] = None,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    key: Annotated[
        Optional[str],
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    thread_dir: ThreadDirOption = None,
    channel: CodexChannelOption = [],
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for websocket channels: jwt, iap, or none."),
        ),
    ] = "jwt",
) -> None:
    resolved_channels = _resolved_codex_channels(channel=channel)
    _require_codex_channels(channels=resolved_channels)
    room = _require_resolved_room(resolve_room(room))
    account_client, resolved_project_id, jwt = await _connect_agent_room(
        project_id=project_id,
        room=room,
        role=role,
        agent_name=agent_name,
        key=key,
        token_from_env=token_from_env,
    )
    try:
        print("[bold green]Connecting to room...[/bold green]", flush=True)
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )
        bot = _build_codex_agent(
            model=model,
            codex_bin=codex_bin,
            codex_config=codex_config,
            working_dir=working_dir,
            channels=resolved_channels,
            websocket_auth=websocket_auth,
            thread_dir=thread_dir,
        )()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:

            async def run_join_session(room_client: RoomClient) -> None:
                print(
                    "[bold green]Open the studio to interact with your agent: "
                    f"{meshagent_base_url().replace('api.', 'studio.')}/projects/"
                    f"{resolved_project_id}/rooms/{room_client.room_name}[/bold green]",
                    flush=True,
                )
                await room_client.protocol.wait_for_close()

            await _run_agent_room_session(
                client=client,
                bot=bot,
                runner=run_join_session,
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        return
    finally:
        await account_client.close()


@app.async_command("service", help="Add a Codex-backed agent service to the host.")
async def service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Default Codex model")
    ] = None,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    thread_dir: ThreadDirOption = None,
    channel: CodexChannelOption = [],
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for websocket channels: jwt, iap, or none."),
        ),
    ] = "jwt",
) -> None:
    resolved_channels = _resolved_codex_channels(channel=channel)
    _require_codex_channels(channels=resolved_channels)
    service_host = get_service(host=host, port=port)
    if path is None:
        path = "/agent"
        index = 0
        while service_host.has_path(path):
            index += 1
            path = f"/agent{index}"

    service_host.agents.append(
        AgentSpec(
            name=agent_name,
            annotations=_codex_agent_annotations(
                thread_dir=thread_dir,
                channels=resolved_channels,
            ),
        )
    )
    service_host.add_path(
        identity=agent_name,
        path=path,
        cls=_build_codex_agent(
            model=model,
            codex_bin=codex_bin,
            codex_config=codex_config,
            working_dir=working_dir,
            channels=resolved_channels,
            websocket_auth=websocket_auth,
            thread_dir=thread_dir,
        ),
    )

    if not get_deferred():
        await run_services()


def _build_service_spec(
    *,
    agent_name: str,
    service_name: str | None,
    service_description: str | None,
    thread_dir: str | None,
    channels: list[str],
) -> Any:
    resolved_service_name = service_name if service_name is not None else agent_name
    spec = service_specs(token_identity=agent_name)[0]
    spec.ports = []
    spec.metadata.annotations = {"meshagent.service.id": resolved_service_name}
    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            "codex",
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
    )
    spec.agents = [
        AgentSpec(
            name=agent_name,
            annotations=_codex_agent_annotations(
                thread_dir=thread_dir,
                channels=channels,
            ),
        )
    ]
    return spec


@app.async_command("spec", help="Generate a service spec for deploying Codex.")
async def spec(
    *,
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Default Codex model")
    ] = None,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    thread_dir: ThreadDirOption = None,
    channel: CodexChannelOption = [],
) -> None:
    del service_title, model, codex_bin, codex_config, working_dir
    resolved_channels = _resolved_codex_channels(channel=channel)
    _require_codex_channels(channels=resolved_channels)
    get_service(host=None, port=None)
    service_spec = _build_service_spec(
        agent_name=agent_name,
        service_name=service_name,
        service_description=service_description,
        thread_dir=thread_dir,
        channels=resolved_channels,
    )
    print(
        yaml.dump(
            service_spec.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        )
    )


@app.async_command("deploy", help="Deploy a Codex-backed agent service.")
async def deploy(
    *,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
    service_name: Annotated[
        Optional[str], typer.Option("--service-name", help="service name")
    ] = None,
    service_description: Annotated[
        Optional[str], typer.Option("--service-description", help="service description")
    ] = None,
    service_title: Annotated[
        Optional[str],
        typer.Option("--service-title", help="a display name for the service"),
    ] = None,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Default Codex model")
    ] = None,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    thread_dir: ThreadDirOption = None,
    channel: CodexChannelOption = [],
) -> None:
    del service_title, model, codex_bin, codex_config, working_dir
    project_id = await resolve_project_id(project_id=project_id)
    resolved_channels = _resolved_codex_channels(channel=channel)
    _require_codex_channels(channels=resolved_channels)
    service_spec = _build_service_spec(
        agent_name=agent_name,
        service_name=service_name,
        service_description=service_description,
        thread_dir=thread_dir,
        channels=resolved_channels,
    )

    client = await get_client()
    try:
        service_id = None
        services = (
            await client.list_services(project_id=project_id)
            if room is None
            else await client.list_room_services(project_id=project_id, room_name=room)
        )
        for existing in services:
            if existing.metadata.name == service_spec.metadata.name:
                service_id = existing.id
                break

        try:
            if service_id is None:
                if room is None:
                    service_id = await client.create_service(
                        project_id=project_id,
                        service=service_spec,
                    )
                else:
                    service_id = await client.create_room_service(
                        project_id=project_id,
                        room_name=room,
                        service=service_spec,
                    )
            else:
                service_spec.id = service_id
                if room is None:
                    await client.update_service(
                        project_id=project_id,
                        service_id=service_id,
                        service=service_spec,
                    )
                else:
                    await client.update_room_service(
                        project_id=project_id,
                        room_name=room,
                        service_id=service_id,
                        service=service_spec,
                    )
        except ConflictError:
            print(
                f"[red]Service name already in use: {service_spec.metadata.name}[/red]"
            )
            raise typer.Exit(code=1) from None

        print(f"[green]Deployed service:[/] {service_id}")
    finally:
        await client.close()


@app.async_command("run", help="Join a room, run Codex, and wait for messages.")
async def run(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    model: Annotated[
        Optional[str], typer.Option("--model", "-m", help="Default Codex model")
    ] = None,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    key: Annotated[
        Optional[str],
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    thread_dir: ThreadDirOption = ".codex/threads",
    channel: CodexChannelOption = [],
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for websocket channels: jwt, iap, or none."),
        ),
    ] = "jwt",
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Enable verbose logging and disable default log suppression",
        ),
    ] = False,
    user: Annotated[
        str,
        typer.Option(
            "--user",
            help="User name for the local websocket run client.",
        ),
    ] = "you",
    thread_path: Annotated[
        Optional[str],
        typer.Option("--thread-path", "--thread-id", help="Codex thread id to open"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option("--message", help="the input message to use"),
    ] = None,
) -> None:
    resolved_channels = _resolved_codex_channels(channel=channel)
    websocket_run_channel = _codex_run_websocket_channel(channels=resolved_channels)
    if not _has_chat_channel(channels=resolved_channels):
        if websocket_run_channel is None:
            raise typer.BadParameter(
                "--channel=chat or --channel=websocket:PORT is required"
            )
    if not verbose:
        logging.getLogger().setLevel(logging.ERROR)

    room = _require_resolved_room(resolve_room(room))
    account_client, _, jwt = await _connect_agent_room(
        project_id=project_id,
        room=room,
        role=role,
        agent_name=agent_name,
        key=key,
    )
    try:
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )
        bot = _build_codex_agent(
            model=model,
            codex_bin=codex_bin,
            codex_config=codex_config,
            working_dir=working_dir,
            channels=resolved_channels,
            websocket_auth=websocket_auth,
            thread_dir=thread_dir,
        )()

        async def run_interactive_session(room_client: RoomClient) -> None:
            chat_client = None
            if websocket_run_channel is not None:
                chat_client = await _open_codex_run_websocket_chat_session(
                    room=room_client,
                    websocket_config=websocket_run_channel,
                    user=user,
                    websocket_auth=websocket_auth,
                    iap_token=jwt,
                    thread_path=thread_path,
                )
            interaction_task = asyncio.create_task(
                _run_codex_run_tui(
                    bot=bot,
                    model=model if model is not None else DEFAULT_CODEX_MODEL,
                    thread_path=thread_path,
                    agent_name=agent_name,
                    message=message,
                    working_dir=working_dir,
                    chat_client=chat_client,
                )
            )
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(room_client.protocol.wait_for_close()),
                    interaction_task,
                ],
                return_when="FIRST_COMPLETED",
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

        await _run_agent_room_session(
            client=client,
            bot=bot,
            runner=run_interactive_session,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        return
    finally:
        await account_client.close()


@app.async_command("threads", help="List threads from the local Codex app-server.")
async def list_threads_command(
    *,
    codex_bin: CodexBinOption = None,
    codex_config: CodexConfigOption = [],
    working_dir: WorkingDirOption = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum threads to show")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Thread list offset")] = 0,
) -> None:
    from rich.table import Table

    participant = Participant(id="codex-cli", attributes={"name": "codex"})
    supervisor = _LocalEventCodexSupervisor(
        participant=participant,
        config=_codex_config(
            codex_bin=codex_bin,
            working_dir=working_dir,
            codex_config=codex_config,
        ),
    )
    await supervisor.start()
    try:
        page = await supervisor.list_threads(
            list_threads=ListThreads(
                type=AGENT_MESSAGE_THREAD_LIST,
                limit=limit,
                offset=offset,
            ),
            sender=participant,
        )
        if page.total == 0:
            print("No threads found.")
            return
        table = Table(title="Codex threads")
        table.add_column("Name")
        table.add_column("Path")
        table.add_column("Modified")
        for entry in page.threads:
            table.add_row(entry.name, entry.path, entry.modified_at)
        print(table)
    finally:
        await supervisor.stop()


@app.async_command(
    "use",
    help="Send a one-shot or interactive message to a running Codex-backed agent.",
)
async def use(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    thread_path: Annotated[
        Optional[str],
        typer.Option("--thread-path", help="Codex thread id to open"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option("--message", help="the input message to use"),
    ] = None,
    websocket_url: Annotated[
        Optional[str],
        typer.Option(
            "--websocket-url",
            help="Connect to a websocket channel instead of room chat.",
        ),
    ] = None,
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for --websocket-url: jwt, iap, or none."),
        ),
    ] = "iap",
    user: Annotated[
        str,
        typer.Option(
            "--user",
            help="User name for the websocket use client.",
        ),
    ] = "you",
) -> None:
    logging.getLogger().setLevel(logging.ERROR)
    resolved_websocket_url = (
        _normalize_codex_use_websocket_url(websocket_url)
        if websocket_url is not None
        else None
    )
    room = _require_resolved_room(resolve_room(room))
    needs_account_client = resolved_websocket_url is None or websocket_auth == "iap"
    account_client = await get_client() if needs_account_client else None
    try:
        if resolved_websocket_url is not None and websocket_auth != "iap":
            project_id = ""
            iap_token = None
        else:
            project_id = await resolve_project_id(project_id=project_id)
            iap_token = None
            if resolved_websocket_url is not None:
                if account_client is None:
                    raise RuntimeError("codex use account client is unavailable")
                connection = await account_client.connect_room(
                    project_id=project_id,
                    room=room,
                )
                iap_token = connection.jwt

        if agent_name is None or agent_name.strip() == "":
            print("[bold red]--agent-name must be specified for codex use[/bold red]")
            raise typer.Exit(1)

        await _run_codex_use_tui(
            account_client=account_client,
            project_id=project_id,
            room=room,
            agent_name=agent_name,
            thread_path=thread_path,
            message=message,
            websocket_url=resolved_websocket_url,
            user=user,
            websocket_auth=websocket_auth,
            iap_token=iap_token,
        )
    except asyncio.CancelledError:
        return
    finally:
        if account_client is not None:
            await account_client.close()
