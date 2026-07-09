import typer
from typer._click.globals import get_current_context
from rich import print
from typing import Annotated, Any, Optional, List, Literal, Awaitable, Callable
import inspect
import uuid
from meshagent.tools import (
    BaseTool,
    Toolkit,
    WebFetchTool,
    ContainerShellTool,
    ContainerToolkit,
    MemoriesToolkit,
)
from meshagent.tools.storage import (
    StorageToolLocalMount,
    StorageToolMount,
    StorageToolkit,
)
from meshagent.tools.datetime import DatetimeToolkit
from meshagent.tools.uuid import UUIDToolkit
from meshagent.tools.document_tools import (
    DocumentAuthoringToolkit,
    DocumentTypeAuthoringToolkit,
)
from meshagent.agents.config import RulesConfig
from meshagent.agents.context import AgentSessionContext
from meshagent.agents.skills import to_prompt
from meshagent.agents.widget_schema import widget_schema

from meshagent.cli.common_options import (
    AllowGotoUrlOption,
    ProjectIdOption,
    ShellConfigMountOption,
    RoomOption,
    ShellEmptyDirMountLegacyOption,
    ShellEmptyDirMountOption,
    ShellRoomMountLegacyOption,
    ShellRoomMountOption,
    StartingUrlOption,
)
from meshagent.api import (
    Participant,
    RoomClient,
    WebSocketClientProtocol,
    ApiScope,
    RoomException,
    RemoteParticipant,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    NormalizedRequiredToolOptions,
    build_shell_tool,
    cleanup_args,
    cleanup_args_strip_options,
    DEPRECATED_REQUIRE_OPTION_ALIASES,
    DEFAULT_DATASET_NAMESPACE,
    DUPLICATE_REQUIRE_OPTION_NAMES,
    get_client,
    merge_option_lists,
    mint_participant_token_for_cli,
    normalize_required_tool_options,
    parse_shell_tool_mounts,
    parse_memory_selector,
    parse_storage_tool_mounts,
    resolve_dataset_namespace,
    resolve_shell_image,
    resolve_project_id,
    resolve_room,
    strip_command_options,
    supports_openai_shell_tool,
)

from meshagent.openai import OpenAIResponsesAdapter, OpenAIResponsesMCPToolkit
from meshagent.anthropic import (
    AnthropicMessagesMCPToolkit,
    AnthropicOpenAIResponsesStreamAdapter,
    WebFetchTool as AnthropicWebFetchTool,
    WebSearchTool as AnthropicWebSearchTool,
)

from pathlib import Path, PurePosixPath
import posixpath
from urllib.parse import urlparse

from meshagent.tools.script import get_script_tools

from meshagent.openai.tools.responses_adapter import (
    WebSearchTool,
    ApplyPatchTool,
    ShellTool,
    ImageGenerationTool,
)

from meshagent.tools.dataset import make_dataset_toolkit
from meshagent.agents.adapter import LLMAdapter, LLMProvider, MessageStreamLLMAdapter
from meshagent.agents.messages import (
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_EVENT_TURN_STEER_ACCEPTED,
    AGENT_EVENT_TURN_STEER_REJECTED,
    AGENT_EVENT_USAGE_UPDATED,
    AGENT_MESSAGE_MODELS_RESPONSE,
    AGENT_MESSAGE_TURN_INTERRUPT,
    AGENT_MESSAGE_TURN_START,
    AGENT_MESSAGE_TURN_STEER,
    AgentError,
    AgentTextContentDelta,
    AgentTextContent,
    AgentUsageUpdated,
    ModelsRequest,
    ModelsResponse,
    TurnEnded,
    TurnInterrupt,
    TurnStart,
    TurnStarted,
    TurnSteer,
    TurnSteerAccepted,
    TurnSteerRejected,
)
from meshagent.agents.chat_client import ChatThreadSession, MessagingChatClient
from meshagent.agents.process import ContentScheme, Message, agent_provider_info

from meshagent.api import RequiredToolkit, RequiredSchema
from meshagent.api.messaging import FileContent
import logging
import os.path
import os

from meshagent.api.specs.service import (
    AgentSpec,
    ANNOTATION_AGENT_TYPE,
    ContainerMountSpec,
)

from meshagent.cli.host import get_service, run_services, get_deferred, service_specs

import yaml

import shlex
import sys
import re

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone


from meshagent.api.client import ConflictError

logger = logging.getLogger("chatbot")


async def _await_cleanup(
    awaitable: Awaitable[Any],
    *,
    timeout: float = 2,
    label: str = "cleanup",
) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("%s did not finish during shutdown; cancelling", label)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return None
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _cancel_background_tasks(
    tasks: set[asyncio.Task[Any]],
    *,
    timeout: float = 1,
) -> None:
    if len(tasks) == 0:
        return

    for task in tasks:
        task.cancel()

    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass

    for task in pending:
        logger.debug("background task did not exit during shutdown: %r", task)


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


async def _maybe_await(callback_result: Any) -> None:
    if inspect.isawaitable(callback_result):
        await callback_result


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


class _ProcessRunSession:
    def __init__(
        self,
        *,
        bot: Any,
        model: str,
        thread_path: str | None,
        current_working_directory: str | None,
    ) -> None:
        self._model = model
        self._thread_id = thread_path or f"/process-run/{uuid.uuid4()}"
        self._current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._supervisor = bot._supervisor
        self._events = self._supervisor.subscribe_local_events()
        self._active_turn_id: str | None = None
        self._pending_steer_callbacks: dict[
            str,
            tuple[
                Callable[[], Awaitable[None] | None] | None,
                Callable[[RoomException], Awaitable[None] | None] | None,
            ],
        ] = {}

    @property
    def current_working_directory(self) -> str:
        return self._current_working_directory

    async def close(self) -> None:
        self._supervisor.unsubscribe_local_events(self._events)

    async def ask(
        self,
        *,
        prompt: str,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_status: Callable[[str | None], None] | None = None,
        on_usage: Callable[[AgentUsageUpdated], Awaitable[None] | None] | None = None,
        on_turn_started: Callable[[], Awaitable[None] | None] | None = None,
    ) -> str:
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=self._thread_id,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
            model=self._model,
        )
        if on_status is not None:
            on_status("Working")
        self._supervisor.send(Message(data=turn_start))

        output_parts: list[str] = []
        active_turn_id: str | None = None
        while True:
            event = await self._events.get()

            if event.data.type == AGENT_EVENT_TURN_STARTED:
                turn_started = TurnStarted.model_validate(
                    event.data.model_dump(mode="python")
                )
                if turn_started.thread_id != self._thread_id:
                    continue
                if turn_started.source_message_id == turn_start.message_id:
                    active_turn_id = turn_started.turn_id
                    self._active_turn_id = active_turn_id
                    if on_turn_started is not None:
                        await _maybe_await(on_turn_started())
                continue

            if event.data.type == AGENT_EVENT_TURN_STEER_ACCEPTED:
                steer_accepted = TurnSteerAccepted.model_validate(
                    event.data.model_dump(mode="python")
                )
                if steer_accepted.thread_id != self._thread_id:
                    continue
                pending_callbacks = self._pending_steer_callbacks.pop(
                    steer_accepted.source_message_id, None
                )
                if pending_callbacks is None:
                    continue
                accepted_callback, _ = pending_callbacks
                if accepted_callback is not None:
                    await _maybe_await(accepted_callback())
                continue

            if event.data.type == AGENT_EVENT_TURN_STEER_REJECTED:
                steer_rejected = TurnSteerRejected.model_validate(
                    event.data.model_dump(mode="python")
                )
                if steer_rejected.thread_id != self._thread_id:
                    continue
                pending_callbacks = self._pending_steer_callbacks.pop(
                    steer_rejected.source_message_id, None
                )
                if pending_callbacks is None:
                    continue
                _, rejected_callback = pending_callbacks
                if rejected_callback is not None:
                    await _maybe_await(
                        rejected_callback(
                            RoomException(
                                steer_rejected.error.message,
                                code=steer_rejected.error.code,
                            )
                        )
                    )
                continue

            if event.data.type == AGENT_EVENT_TEXT_CONTENT_DELTA:
                text_delta = event.data.model_copy(deep=False)
                if text_delta.thread_id != self._thread_id:
                    continue
                if active_turn_id is not None and text_delta.turn_id != active_turn_id:
                    continue
                output_parts.append(text_delta.text)
                if on_delta is not None:
                    await _maybe_await(on_delta(text_delta.text))
                continue

            if isinstance(event.data, AgentUsageUpdated):
                usage_update = event.data
                if usage_update.thread_id != self._thread_id:
                    continue
                if active_turn_id is not None and usage_update.turn_id not in (
                    None,
                    active_turn_id,
                ):
                    continue
                if on_usage is not None:
                    await _maybe_await(on_usage(usage_update))
                continue

            if event.data.type == AGENT_EVENT_TURN_ENDED:
                turn_ended = TurnEnded.model_validate(
                    event.data.model_dump(mode="python")
                )
                if turn_ended.thread_id != self._thread_id:
                    continue
                if active_turn_id is not None and turn_ended.turn_id != active_turn_id:
                    continue
                self._active_turn_id = None
                self._pending_steer_callbacks.clear()
                if on_status is not None:
                    on_status(None)
                if turn_ended.error is not None:
                    raise RoomException(
                        turn_ended.error.message,
                        code=turn_ended.error.code,
                    )
                return "".join(output_parts)

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        if self._active_turn_id is None:
            return None

        turn_steer = TurnSteer(
            type=AGENT_MESSAGE_TURN_STEER,
            thread_id=self._thread_id,
            turn_id=self._active_turn_id,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
        )
        self._pending_steer_callbacks[turn_steer.message_id] = (
            on_accepted,
            on_rejected,
        )
        self._supervisor.send(Message(data=turn_steer))
        return turn_steer.message_id

    def interrupt(self) -> bool:
        if self._active_turn_id is None:
            return False

        self._supervisor.send(
            Message(
                data=TurnInterrupt(
                    type=AGENT_MESSAGE_TURN_INTERRUPT,
                    thread_id=self._thread_id,
                    turn_id=self._active_turn_id,
                )
            )
        )
        return True


async def _run_process_run_tui(
    *,
    bot: Any,
    model: str,
    thread_path: str | None,
    message: str | None,
    working_dir: str | None,
) -> None:
    from meshagent.cli import ask as ask_module

    session = _ProcessRunSession(
        bot=bot,
        model=model,
        thread_path=thread_path,
        current_working_directory=working_dir,
    )
    try:
        if message is not None:
            await session.ask(
                prompt=message,
                on_delta=lambda text: typer.echo(text, nl=False),
            )
            typer.echo()
            return

        await ask_module._run_ask_tui(
            model=model,
            session=session,
            title="meshagent process run",
        )
    finally:
        await session.close()


class _ChatChannelUseSession:
    def __init__(
        self,
        *,
        chat_client: ChatThreadSession,
        current_working_directory: str | None = None,
    ) -> None:
        self._chat_client = chat_client
        self._current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._active_turn_id: str | None = None
        self._pending_steer_callbacks: dict[
            str,
            tuple[
                Callable[[], Awaitable[None] | None] | None,
                Callable[[RoomException], Awaitable[None] | None] | None,
            ],
        ] = {}

    @property
    def current_working_directory(self) -> str:
        return self._current_working_directory

    async def close(self) -> None:
        self._pending_steer_callbacks.clear()

    async def ask(
        self,
        *,
        prompt: str,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_status: Callable[[str | None], None] | None = None,
        on_usage: Callable[[AgentUsageUpdated], Awaitable[None] | None] | None = None,
        on_turn_started: Callable[[], Awaitable[None] | None] | None = None,
    ) -> str:
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=self._chat_client.thread_path,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
        )
        if on_status is not None:
            on_status("Working")
        await self._chat_client.send(turn_start)

        output_parts: list[str] = []
        active_turn_id: str | None = None
        try:
            while True:
                payload = await self._chat_client.receive()
                event_type = payload.get("type")

                if event_type == AGENT_EVENT_TURN_STARTED:
                    turn_started = TurnStarted.model_validate(payload)
                    if turn_started.source_message_id == turn_start.message_id:
                        active_turn_id = turn_started.turn_id
                        self._active_turn_id = active_turn_id
                        if on_turn_started is not None:
                            await _maybe_await(on_turn_started())
                    continue

                if event_type == AGENT_EVENT_TURN_STEER_ACCEPTED:
                    steer_accepted = TurnSteerAccepted.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_accepted.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    accepted_callback, _ = pending_callbacks
                    if accepted_callback is not None:
                        await _maybe_await(accepted_callback())
                    continue

                if event_type == AGENT_EVENT_TURN_STEER_REJECTED:
                    steer_rejected = TurnSteerRejected.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_rejected.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    _, rejected_callback = pending_callbacks
                    if rejected_callback is not None:
                        await _maybe_await(
                            rejected_callback(
                                RoomException(
                                    steer_rejected.error.message,
                                    code=steer_rejected.error.code,
                                )
                            )
                        )
                    continue

                if event_type == AGENT_EVENT_TEXT_CONTENT_DELTA:
                    text_delta = AgentTextContentDelta.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and text_delta.turn_id != active_turn_id
                    ):
                        continue
                    output_parts.append(text_delta.text)
                    if on_delta is not None:
                        await _maybe_await(on_delta(text_delta.text))
                    continue

                if event_type == AGENT_EVENT_USAGE_UPDATED:
                    usage_update = AgentUsageUpdated.model_validate(payload)
                    if active_turn_id is not None and usage_update.turn_id not in (
                        None,
                        active_turn_id,
                    ):
                        continue
                    if on_usage is not None:
                        await _maybe_await(on_usage(usage_update))
                    continue

                if event_type == AGENT_EVENT_TURN_ENDED:
                    turn_ended = TurnEnded.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and turn_ended.turn_id != active_turn_id
                    ):
                        continue
                    self._active_turn_id = None
                    self._pending_steer_callbacks.clear()
                    if turn_ended.error is not None:
                        raise RoomException(
                            turn_ended.error.message,
                            code=turn_ended.error.code,
                        )
                    return "".join(output_parts)
        finally:
            self._active_turn_id = None
            if on_status is not None:
                on_status(None)

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        if self._active_turn_id is None:
            return None

        turn_steer = TurnSteer(
            type=AGENT_MESSAGE_TURN_STEER,
            thread_id=self._chat_client.thread_path,
            turn_id=self._active_turn_id,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
        )
        self._pending_steer_callbacks[turn_steer.message_id] = (
            on_accepted,
            on_rejected,
        )

        async def _send_steer() -> None:
            try:
                await self._chat_client.send(turn_steer)
            except RoomException as exc:
                self._pending_steer_callbacks.pop(turn_steer.message_id, None)
                if on_rejected is not None:
                    await _maybe_await(on_rejected(exc))
            except Exception as exc:
                self._pending_steer_callbacks.pop(turn_steer.message_id, None)
                if on_rejected is not None:
                    await _maybe_await(on_rejected(RoomException(str(exc))))

        task = asyncio.create_task(_send_steer())
        task.add_done_callback(_consume_task_exception)
        return turn_steer.message_id

    def interrupt(self) -> bool:
        if self._active_turn_id is None:
            return False

        turn_interrupt = TurnInterrupt(
            type=AGENT_MESSAGE_TURN_INTERRUPT,
            thread_id=self._chat_client.thread_path,
            turn_id=self._active_turn_id,
        )

        async def _send_cancel() -> None:
            await self._chat_client.send(turn_interrupt)

        task = asyncio.create_task(_send_cancel())
        task.add_done_callback(_consume_task_exception)
        return True


async def _close_process_use_chat_client(
    client: ChatThreadSession | None,
) -> None:
    if client is None:
        return
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        pass


async def _close_process_use_room_client(client: RoomClient | None) -> None:
    if client is None:
        return
    try:
        await client.__aexit__(None, None, None)
    except Exception:
        pass


async def _open_process_use_chat_session(
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
        local_user_name = user_client.local_participant.get_attribute("name")
        resolved_thread_path = thread_path
        if resolved_thread_path is None:
            resolved_thread_path = (
                f".threads/{participant_name}/{local_user_name}.thread"
            )

        chat_client = MessagingChatClient(
            room=user_client,
            participant_name=participant_name,
        )
        await chat_client.__aenter__()
        chat_session = await chat_client.open_thread(
            resolved_thread_path,
            close_client_on_close=True,
        )
        return user_client, chat_session
    except Exception:
        await _close_process_use_chat_client(chat_session)
        if chat_session is None and chat_client is not None:
            await chat_client.__aexit__(None, None, None)
        await _close_process_use_room_client(user_client)
        raise


async def _run_process_use_tui(
    *,
    account_client: Any,
    project_id: str,
    room: str,
    agent_name: str,
    thread_path: str | None,
    message: str | None,
) -> None:
    from meshagent.cli import ask as ask_module

    user_client: RoomClient | None = None
    chat_client: ChatThreadSession | None = None
    session: _ChatChannelUseSession | None = None
    try:
        user_client, chat_client = await _open_process_use_chat_session(
            account_client=account_client,
            project_id=project_id,
            room=room,
            participant_name=agent_name,
            thread_path=thread_path,
        )
        session = _ChatChannelUseSession(chat_client=chat_client)

        if message is not None:
            await session.ask(
                prompt=message,
                on_delta=lambda text: typer.echo(text, nl=False),
            )
            typer.echo()
            return

        await ask_module._run_ask_tui(
            model="remote",
            session=session,
            title=f"meshagent process use: {agent_name}",
            assistant_name=agent_name,
        )
    finally:
        if session is not None:
            await session.close()
        await _close_process_use_chat_client(chat_client)
        await _close_process_use_room_client(user_client)


app = async_typer.AsyncTyper(help="Join a chatbot to a room")
app.add_deprecated_option_aliases(
    {**DEPRECATED_REQUIRE_OPTION_ALIASES, "--database-namespace": "--dataset-namespace"}
)

ThreadingMode = Literal["none", "default-new"]
ContextManagementMode = Literal["auto", "standalone", "none"]

ShellCopyEnvOption = Annotated[
    list[str],
    typer.Option(
        "--shell-copy-env",
        help=(
            "Copy local env vars into shell tool env. "
            "Accepts comma-separated names and can be repeated."
        ),
    ),
]

ShellSetEnvOption = Annotated[
    list[str],
    typer.Option(
        "--shell-set-env",
        help=("Set env vars in shell tool env as NAME=VALUE. Can be repeated."),
    ),
]

InstructionsOption = Annotated[
    list[str],
    typer.Option(
        "--instructions",
        help=(
            "a path in the configured storage toolkit to a rules file that "
            "will be loaded at runtime"
        ),
    ),
]

RequireAdvancedShellOption = Annotated[
    Optional[bool],
    typer.Option(
        "--advanced-shell",
        help=("Enable the managed container toolkit with start/list/stop/run tools."),
    ),
]

WORKING_DIR_HELP = "The default working directory for shell commands"

WorkingDirOption = Annotated[
    Optional[str],
    typer.Option(
        "--working-dir",
        help=WORKING_DIR_HELP,
    ),
]

WorkingDirectoryAliasOption = Annotated[
    Optional[str],
    typer.Option(
        "--working-directory",
        help="Alias for --working-dir",
        hidden=True,
    ),
]

ThreadingModeOption = Annotated[
    ThreadingMode,
    typer.Option(
        "--threading-mode",
        help=(
            "Threading mode for thread UIs. "
            "Use 'default-new' to show a new-thread composer before loading a thread."
        ),
    ),
]

ThreadDirOption = Annotated[
    Optional[str],
    typer.Option(
        "--thread-dir",
        help=(
            "Thread directory for agent thread files. "
            "Defaults to .threads/<agent-name> when not provided."
        ),
    ),
]

ContextManagementOption = Annotated[
    ContextManagementMode,
    typer.Option(
        "--context-management",
        help=(
            "Context compaction mode for OpenAI Responses process agents: "
            "auto, standalone, or none."
        ),
    ),
]

CompactionThresholdOption = Annotated[
    Optional[int],
    typer.Option(
        "--compaction-threshold",
        help="Token threshold for OpenAI Responses context compaction.",
    ),
]

MaxOutputTokensOption = Annotated[
    Optional[int],
    typer.Option(
        "--max-output-tokens",
        help="Maximum output tokens to request from OpenAI Responses models.",
    ),
]

DecisionModelOption = Annotated[
    Optional[str],
    typer.Option(
        "--decision-model",
        help="Model used for thread naming and other secondary LLM decisions",
    ),
]

ChannelOption = Annotated[
    list[str],
    typer.Option(
        "--channel",
        help=(
            "Attach a channel to the agent process. "
            "Can be repeated. Currently supported: chat, mail:EMAIL_ADDRESS, "
            "queue:QUEUE_NAME, toolkit:NAME."
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class _MailChannelConfig:
    queue_name: str
    email_address: str


@dataclass(frozen=True, slots=True)
class _QueueChannelConfig:
    queue_name: str


@dataclass(frozen=True, slots=True)
class _ToolkitChannelConfig:
    toolkit_name: str


def _current_command_runtime() -> Literal["chatbot", "process"]:
    context = get_current_context(silent=True)
    while context is not None:
        info_name = context.info_name
        if info_name == "chatbot":
            return "chatbot"
        if info_name == "process":
            return "process"
        context = context.parent
    return "chatbot"


def _resolved_dataset_namespace(
    *,
    runtime: Literal["chatbot", "process"],
    dataset_namespace: Optional[str],
) -> Optional[list[str]]:
    default_namespace: tuple[str, ...] | None = None
    if runtime == "chatbot":
        default_namespace = DEFAULT_DATASET_NAMESPACE

    return resolve_dataset_namespace(
        namespace=dataset_namespace,
        default_namespace=default_namespace,
    )


def _resolved_channels(
    *,
    runtime: Literal["chatbot", "process"],
    channel: Optional[list[str]],
    require_chat: bool = False,
) -> list[str]:
    normalized_channels: list[str] = []
    seen_channels: set[str] = set()
    for item in channel or []:
        normalized = item.strip()
        if normalized == "":
            continue
        if normalized.casefold() == "chat":
            if "chat" not in seen_channels:
                seen_channels.add("chat")
                normalized_channels.append("chat")
            continue

        if normalized[:5].casefold() == "mail:":
            mail_config = _parse_mail_channel(channel=normalized)
            channel_key = f"mail:{mail_config.email_address.casefold()}"
            if channel_key not in seen_channels:
                seen_channels.add(channel_key)
                normalized_channels.append(
                    f"mail:{mail_config.email_address}",
                )
            continue

        if normalized[:6].casefold() == "queue:":
            queue_config = _parse_queue_channel(channel=normalized)
            channel_key = f"queue:{queue_config.queue_name}"
            if channel_key not in seen_channels:
                seen_channels.add(channel_key)
                normalized_channels.append(channel_key)
            continue

        if normalized[:8].casefold() == "toolkit:":
            toolkit_config = _parse_toolkit_channel(channel=normalized)
            channel_key = f"toolkit:{toolkit_config.toolkit_name}"
            if channel_key not in seen_channels:
                seen_channels.add(channel_key)
                normalized_channels.append(channel_key)
            continue

        raise typer.BadParameter(f"unsupported channel: {item}")

    if runtime == "chatbot":
        return ["chat"]

    if require_chat and "chat" not in normalized_channels:
        raise typer.BadParameter("--channel=chat is required for this command")

    return normalized_channels


def _require_process_channels(
    *, runtime: Literal["chatbot", "process"], channels: list[str]
) -> None:
    if runtime != "process" or len(channels) > 0:
        return

    print("[bold red]at least one channel is required for process agents[/bold red]")
    raise typer.Exit(1)


def _parse_mail_channel(*, channel: str) -> _MailChannelConfig:
    if channel[:5].casefold() != "mail:":
        raise typer.BadParameter(f"unsupported mail channel: {channel}")

    address = channel[5:].strip()
    if address == "":
        raise typer.BadParameter(
            "mail channels must be passed as --channel=mail:mailbox@example.com"
        )

    return _MailChannelConfig(
        queue_name=address,
        email_address=address,
    )


def _parse_queue_channel(*, channel: str) -> _QueueChannelConfig:
    if channel[:6].casefold() != "queue:":
        raise typer.BadParameter(f"unsupported queue channel: {channel}")

    queue_name = channel[6:].strip()
    if queue_name == "":
        raise typer.BadParameter(
            "queue channels must be passed as --channel=queue:QUEUE_NAME"
        )

    return _QueueChannelConfig(queue_name=queue_name)


def _parse_toolkit_channel(*, channel: str) -> _ToolkitChannelConfig:
    if channel[:8].casefold() != "toolkit:":
        raise typer.BadParameter(f"unsupported toolkit channel: {channel}")

    toolkit_name = channel[8:].strip()
    if toolkit_name == "":
        raise typer.BadParameter(
            "toolkit channels must be passed as --channel=toolkit:NAME"
        )

    return _ToolkitChannelConfig(toolkit_name=toolkit_name)


def _has_chat_channel(*, channels: list[str]) -> bool:
    return "chat" in channels


def _normalized_thread_dir(*, thread_dir: Optional[str]) -> Optional[str]:
    if thread_dir is None:
        return None

    normalized = thread_dir.strip().rstrip("/")
    if normalized == "":
        return None

    return normalized


def _normalized_decision_model(*, decision_model: Optional[str]) -> Optional[str]:
    if not isinstance(decision_model, str):
        return None

    normalized = decision_model.strip()
    if normalized == "":
        return None

    return normalized


def _build_decision_llm_adapter(
    *,
    decision_model: str,
    api_key: str | None = None,
    log_llm_requests: Optional[bool],
) -> LLMAdapter:
    if decision_model.startswith("claude-"):
        return AnthropicOpenAIResponsesStreamAdapter(
            model=decision_model,
            api_key=api_key,
            log_requests=log_llm_requests,
        )

    return OpenAIResponsesAdapter(
        model=decision_model,
        api_key=api_key,
        log_requests=log_llm_requests,
    )


def _chatbot_agent_annotations(
    *,
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
) -> dict[str, str]:
    annotations: dict[str, str] = {ANNOTATION_AGENT_TYPE: "ChatBot"}
    if threading_mode != "none":
        annotations["meshagent.chatbot.threading"] = threading_mode

    normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
    if normalized_thread_dir is not None:
        annotations["meshagent.chatbot.thread-dir"] = normalized_thread_dir
        annotations["meshagent.chatbot.thread-list"] = (
            f"{normalized_thread_dir}/index.threadl"
        )

    return annotations


def _process_agent_annotations(
    *,
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
    channel: list[str],
) -> dict[str, str]:
    if not _has_chat_channel(channels=channel):
        return {}
    return _chatbot_agent_annotations(
        threading_mode=threading_mode,
        thread_dir=thread_dir,
    )


def _agent_annotations_for_runtime(
    *,
    runtime: Literal["chatbot", "process"],
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
    channel: list[str],
) -> dict[str, str]:
    if runtime == "process":
        return _process_agent_annotations(
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            channel=channel,
        )
    return _chatbot_agent_annotations(
        threading_mode=threading_mode,
        thread_dir=thread_dir,
    )


def _builder_for_runtime(runtime: Literal["chatbot", "process"]):
    if runtime == "process":
        return build_process_agent
    return build_chatbot


def _require_storage_tool_mounts(
    *,
    room: RoomClient,
    local_paths: list[str],
    room_paths: list[str],
    default_room_mount: bool,
) -> list[StorageToolMount]:
    mounts = parse_storage_tool_mounts(
        room=room,
        local_paths=local_paths,
        room_paths=room_paths,
        default_room_mount=default_room_mount,
    )
    if mounts is None:
        raise RuntimeError("storage toolkit requires at least one configured mount")
    return mounts


def _default_rules_storage_toolkit() -> StorageToolkit:
    return StorageToolkit(
        read_only=True,
        mounts=[
            StorageToolLocalMount(
                path="/",
                local_path=str(Path.cwd()),
            )
        ],
    )


def _resolve_configured_local_storage_path(path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    return (Path.cwd() / path).resolve()


def _normalize_configured_storage_path(path: Path) -> str:
    if path.is_absolute():
        return path.as_posix()

    normalized = posixpath.normpath(f"/{path.as_posix()}")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _default_rules_storage_for_path(path: str) -> tuple[StorageToolkit, str]:
    configured_path = Path(path)
    cwd = Path.cwd().resolve()
    resolved = _resolve_configured_local_storage_path(configured_path)

    if resolved.is_relative_to(cwd):
        relative = resolved.relative_to(cwd)
        virtual_path = Path("/") if relative == Path(".") else Path("/") / relative
        return _default_rules_storage_toolkit(), virtual_path.as_posix()

    return (
        StorageToolkit(
            read_only=True,
            mounts=[
                StorageToolLocalMount(
                    path="/",
                    local_path=str(cwd),
                ),
                StorageToolLocalMount(
                    path=resolved.as_posix(),
                    local_path=str(resolved),
                ),
            ],
        ),
        resolved.as_posix(),
    )


def _normalize_room_content_path(*, url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.scheme != "room":
        raise ValueError(f"unsupported room file url: {url}")

    raw_path = f"{parsed_url.netloc}{parsed_url.path}".lstrip("/")
    normalized = PurePosixPath("/" + raw_path).as_posix().strip("/")
    if normalized == "":
        raise ValueError("room file url must reference a non-root storage path")

    if any(part in {".", ".."} for part in PurePosixPath(normalized).parts):
        raise ValueError("room file url cannot contain '.' or '..' segments")

    return normalized


def _room_content_scheme(*, room: RoomClient) -> ContentScheme:
    async def _download(url: str) -> FileContent:
        path = _normalize_room_content_path(url=url)
        return await room.storage.download(path=path)

    return ContentScheme(prefix="room://", download=_download)


async def _load_storage_rules(
    *,
    path: str,
    storage_toolkit: StorageToolkit | None,
    participant: Participant | None,
) -> list[str]:
    if storage_toolkit is None:
        resolved_storage_toolkit, normalized_path = _default_rules_storage_for_path(
            path
        )
    else:
        resolved_storage_toolkit = storage_toolkit
        normalized_path = _normalize_configured_storage_path(Path(path))
    rules: list[str] = []

    try:
        instructions_file = await resolved_storage_toolkit.read_file(
            path=normalized_path
        )
    except RoomException as exc:
        logger.warning("unable to load instructions from %s: %s", path, exc)
        return rules

    rules_txt = instructions_file.data.decode()
    rules_config = RulesConfig.parse(rules_txt)

    if rules_config.rules is not None:
        rules.extend(rules_config.rules)

    if participant is not None:
        client = participant.get_attribute("client")
        if rules_config.client_rules is not None and client is not None:
            client_rules = rules_config.client_rules.get(client)
            if client_rules is not None:
                rules.extend(client_rules)

    return rules


def _build_runtime_agent(
    *,
    client: RoomClient | None,
    api_key: str | None = None,
    runtime: Literal["chatbot", "process"],
    normalized_tool_options: NormalizedRequiredToolOptions,
    model: str,
    rule: list[str],
    rules_file: Optional[list[str]],
    instructions: Optional[list[str]],
    discover_script_tools: Optional[bool],
    storage_tool_local_paths: list[str],
    storage_tool_room_paths: list[str],
    default_room_storage_mount: bool,
    shell_tool_mounts: Optional[ContainerMountSpec],
    require_read_only_storage: Optional[str],
    require_time: bool,
    require_uuid: bool,
    use_memory: Optional[str],
    memory_model: Optional[str],
    require_table_read: list[str] | None,
    require_table_write: list[str] | None,
    require_document_authoring: Optional[str],
    require_discovery: Optional[str],
    require_advanced_shell: Optional[bool],
    llm_participant: Optional[str],
    decision_model: Optional[str],
    always_reply: Optional[bool],
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
    context_management: ContextManagementMode,
    compaction_threshold: Optional[int],
    max_output_tokens: Optional[int],
    working_dir: Optional[str],
    dataset_namespace: Optional[list[str]],
    skill_dirs: Optional[list[str]],
    shell_image: Optional[str],
    delegate_shell_token: Optional[bool],
    shell_copy_env: Optional[list[str]],
    shell_set_env: Optional[list[str]],
    log_llm_requests: Optional[bool],
    channels: Optional[list[str]],
    starting_url: Optional[str],
    allow_goto_url: bool,
    room_rules_path: Optional[list[str]],
):
    builder = _builder_for_runtime(runtime)
    builder_kwargs: dict[str, Any] = {
        "computer_use": False,
        "require_computer_use": normalized_tool_options["require_computer_use"],
        "api_key": api_key,
        "starting_url": starting_url,
        "allow_goto_url": allow_goto_url,
        "model": model,
        "rule": rule,
        "toolkit": normalized_tool_options["toolkit"],
        "schema": normalized_tool_options["schema"],
        "rules_file": rules_file,
        "instructions": instructions,
        "discover_script_tools": discover_script_tools,
        "client": client,
        "storage_tool_local_paths": storage_tool_local_paths,
        "storage_tool_room_paths": storage_tool_room_paths,
        "default_room_storage_mount": default_room_storage_mount,
        "shell_tool_mounts": shell_tool_mounts,
        "require_apply_patch": normalized_tool_options["require_apply_patch"],
        "require_web_search": normalized_tool_options["require_web_search"],
        "require_web_fetch": normalized_tool_options["require_web_fetch"],
        "require_shell": normalized_tool_options["require_shell"],
        "require_advanced_shell": require_advanced_shell,
        "require_image_generation": normalized_tool_options["require_image_generation"],
        "require_mcp": normalized_tool_options["mcp"],
        "require_storage": normalized_tool_options["require_storage"],
        "require_table_read": require_table_read,
        "require_table_write": require_table_write,
        "require_read_only_storage": require_read_only_storage,
        "require_time": require_time,
        "require_uuid": require_uuid,
        "use_memory": use_memory,
        "memory_model": memory_model,
        "room_rules_path": room_rules_path,
        "require_document_authoring": require_document_authoring,
        "require_discovery": require_discovery,
        "working_dir": working_dir,
        "llm_participant": llm_participant,
        "decision_model": decision_model,
        "always_reply": always_reply,
        "threading_mode": threading_mode,
        "thread_dir": thread_dir,
        "dataset_namespace": dataset_namespace,
        "skill_dirs": skill_dirs,
        "shell_image": shell_image,
        "delegate_shell_token": delegate_shell_token,
        "shell_copy_env": shell_copy_env,
        "shell_set_env": shell_set_env,
        "log_llm_requests": log_llm_requests,
        "channels": channels,
    }
    if runtime == "process":
        builder_kwargs["context_management"] = context_management
        builder_kwargs["compaction_threshold"] = compaction_threshold
        builder_kwargs["max_output_tokens"] = max_output_tokens
    return builder(**builder_kwargs)


def _copy_shell_env_vars(*, copy_env: Optional[list[str]]) -> dict[str, str]:
    if copy_env is None:
        return {}

    names: list[str] = []
    seen: set[str] = set()
    for item in copy_env:
        for split_item in item.split(","):
            name = split_item.strip()
            if name == "":
                continue
            if name in seen:
                continue
            seen.add(name)
            names.append(name)

    env: dict[str, str] = {}
    for name in names:
        value = os.getenv(name)
        if value is None:
            raise typer.BadParameter(f"--shell-copy-env variable is not set: {name}")
        env[name] = value

    return env


def _set_shell_env_vars(*, set_env: Optional[list[str]]) -> dict[str, str]:
    if set_env is None:
        return {}

    env: dict[str, str] = {}
    for item in set_env:
        value = item.strip()
        if value == "":
            continue

        if "=" not in value:
            raise typer.BadParameter(
                f"--shell-set-env value must be NAME=VALUE, got: {item}"
            )

        name, assigned_value = value.split("=", 1)
        name = name.strip()
        if name == "":
            raise typer.BadParameter(
                f"--shell-set-env variable name cannot be empty: {item}"
            )

        env[name] = assigned_value

    return env


def _build_shell_tool_env(
    *,
    base_env: dict[str, str],
    delegate_shell_token: Optional[bool],
    room: RoomClient,
) -> dict[str, str]:
    env = dict(base_env)
    if delegate_shell_token:
        env["MESHAGENT_TOKEN"] = room.protocol.token
        env["OPENAI_API_KEY"] = room.protocol.token
        env["ANTHROPIC_API_KEY"] = room.protocol.token
    return env


def _resolve_working_dir_option(
    *,
    working_dir: Optional[str],
    working_directory: Optional[str],
) -> Optional[str]:
    if (
        working_dir is not None
        and working_directory is not None
        and working_dir != working_directory
    ):
        raise typer.BadParameter(
            "Conflicting values for --working-dir and --working-directory"
        )
    return working_dir if working_dir is not None else working_directory


def build_chatbot(
    *,
    client: RoomClient | None = None,
    api_key: str | None = None,
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    computer_use: Optional[str] = None,
    discover_script_tools: Optional[bool] = None,
    storage_tool_local_paths: list[str] | None = None,
    storage_tool_room_paths: list[str] | None = None,
    default_room_storage_mount: bool = False,
    shell_tool_mounts: Optional[ContainerMountSpec] = None,
    require_image_generation: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_advanced_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_computer_use: Optional[str] = None,
    starting_url: Optional[str] = None,
    allow_goto_url: bool = False,
    require_web_search: Optional[str] = None,
    require_web_fetch: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    use_memory: Optional[str] = None,
    memory_model: Optional[str] = None,
    rules_file: Optional[list[str]] = None,
    instructions: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    require_discovery: Optional[str] = None,
    require_document_authoring: Optional[str] = None,
    working_dir: Optional[str] = None,
    llm_participant: Optional[str] = None,
    decision_model: Optional[str] = None,
    dataset_namespace: Optional[list[str]] = None,
    always_reply: Optional[bool] = None,
    thread_dir: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    threading_mode: ThreadingMode = "none",
    shell_image: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
    delegate_shell_token: Optional[bool] = None,
    shell_copy_env: Optional[list[str]] = None,
    shell_set_env: Optional[list[str]] = None,
    channels: Optional[list[str]] = None,
):
    del channels
    from meshagent.agents.chat import ChatBot

    from meshagent.tools.storage import StorageToolkit

    requirements = []

    toolkits = []

    if storage_tool_local_paths is None:
        storage_tool_local_paths = []
    if storage_tool_room_paths is None:
        storage_tool_room_paths = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    client_rules = {}

    if rules_file is not None:
        for rules_path in rules_file:
            try:
                logger.info(f"loading rules from {rules_path}")
                with open(Path(os.path.expanduser(rules_path)).resolve(), "r") as f:
                    rules_config = RulesConfig.parse(f.read())
                    if rules_config.rules is not None:
                        rule.extend(rules_config.rules)
                    if rules_config.client_rules is not None:
                        client_rules.update(rules_config.client_rules)

            except FileNotFoundError:
                print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    is_claude_model = model.startswith("claude-")
    supports_openai_tools = llm_participant is None and not is_claude_model
    supports_openai_shell = supports_openai_shell_tool(
        model=model, llm_participant=llm_participant
    )
    base_shell_env = _copy_shell_env_vars(copy_env=shell_copy_env)
    base_shell_env.update(_set_shell_env_vars(set_env=shell_set_env))
    resolved_shell_image = resolve_shell_image(shell_image)
    if computer_use or require_computer_use:
        print("[red]computer use is not supported by chatbot runtime[/red]")
        raise typer.Exit(1)

    if not supports_openai_tools:
        if require_image_generation:
            print("[red]image generation tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if require_apply_patch:
            print("[red]apply patch tool is only supported by openai models[/red]")
            raise typer.Exit(1)

    memory_selection: Optional[tuple[str, Optional[list[str]]]] = None
    if use_memory is not None:
        memory_selection = parse_memory_selector(use_memory)

    BaseClass = ChatBot
    resolved_decision_model = _normalized_decision_model(decision_model=decision_model)
    if llm_participant:
        llm_adapter = MessageStreamLLMAdapter(
            participant_name=llm_participant,
        )
    else:
        if is_claude_model:
            llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                model=model,
                api_key=api_key,
                log_requests=log_llm_requests,
            )
            if resolved_decision_model is None:
                resolved_decision_model = model
        else:
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                api_key=api_key,
                log_requests=log_llm_requests,
            )

    class CustomChatbot(BaseClass):
        def __init__(self):
            resolved_threading_mode: Optional[str] = None
            if threading_mode != "none":
                resolved_threading_mode = threading_mode

            super().__init__(
                llm_adapter=llm_adapter,
                requires=requirements,
                toolkits=toolkits,
                rules=rule if len(rule) > 0 else None,
                client_rules=client_rules,
                always_reply=always_reply,
                thread_dir=thread_dir,
                skill_dirs=skill_dirs,
                threading_mode=resolved_threading_mode,
                decision_model=resolved_decision_model,
            )

            self.shell_tool = None
            self.advanced_shell_toolkit = None

        async def start(self, *, room: RoomClient):
            await super().start(room=room)
            if require_mcp:
                await room.local_participant.set_attribute("supports_mcp", True)

            env = _build_shell_tool_env(
                base_env=base_shell_env,
                delegate_shell_token=delegate_shell_token,
                room=room,
            )

            if require_shell:
                if supports_openai_shell:
                    shell_kwargs = {
                        "working_dir": working_dir,
                        "name": "shell",
                        "image": resolved_shell_image,
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts
                    self.shell_tool = ShellTool(room=room, **shell_kwargs)
                else:
                    shell_kwargs = {
                        "image": resolved_shell_image,
                        "name": "shell",
                        "working_dir": working_dir,
                        "env": env,
                    }
                    if shell_tool_mounts is not None:
                        shell_kwargs["mounts"] = shell_tool_mounts

                    self.shell_tool = ContainerShellTool(room=room, **shell_kwargs)

            if require_advanced_shell:
                self.advanced_shell_toolkit = ContainerToolkit(
                    room=room,
                    working_dir=working_dir,
                    default_image=resolved_shell_image,
                    mounts=shell_tool_mounts,
                    env=env or None,
                )

            if room_rules_path is not None:
                for p in room_rules_path:
                    await self._load_room_rules(path=p)

        async def stop(self) -> None:
            room = self._room
            try:
                if self.advanced_shell_toolkit is not None and room is not None:
                    await self.advanced_shell_toolkit.stop_all(room=room)
            finally:
                if require_mcp and room is not None:
                    await room.local_participant.set_attribute("supports_mcp", None)
                self.advanced_shell_toolkit = None
                self.shell_tool = None
                await super().stop()

        async def init_session(self):
            from meshagent.cli.helper import init_context_from_spec

            context = await super().init_session()
            await init_context_from_spec(context)

            return context

        async def _load_room_rules(
            self,
            *,
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
                logger.info(
                    f"unable to load rules from {path}, continuing with default rules"
                )
                pass

            return rules

        def get_skills_storage_toolkit(self) -> StorageToolkit | None:
            if require_storage:
                return StorageToolkit(
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    )
                )

            if require_read_only_storage:
                return StorageToolkit(
                    read_only=True,
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    ),
                )

            return None

        async def get_rules(self, *, thread_context, participant):
            rules = await super().get_rules(
                thread_context=thread_context, participant=participant
            )
            storage_toolkit = self.get_skills_storage_toolkit()

            if instructions is not None:
                for instructions_path in instructions:
                    rules.extend(
                        await _load_storage_rules(
                            path=instructions_path,
                            storage_toolkit=storage_toolkit,
                            participant=participant,
                        )
                    )

            if room_rules_path is not None:
                for p in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(path=p, participant=participant)
                    )

            logging.info(f"using rules {rules}")

            return rules

        async def get_thread_toolkits(self, *, thread_context, participant):
            required_toolkits: list[Toolkit] = []

            def add_toolkit(toolkit: Toolkit) -> None:
                required_toolkits.append(toolkit)

            def add_tool(*, toolkit_name: str, tool) -> None:
                add_toolkit(Toolkit(name=toolkit_name, tools=[tool]))

            if discover_script_tools:
                for script_tool in await get_script_tools(self.room):
                    add_tool(toolkit_name="script", tool=script_tool)

            if require_image_generation:
                add_tool(
                    toolkit_name="image_generation",
                    tool=ImageGenerationTool(
                        model=require_image_generation,
                        partial_images=3,
                    ),
                )

            if require_apply_patch:
                add_tool(
                    toolkit_name="apply_patch",
                    tool=ApplyPatchTool(
                        storage=StorageToolkit(
                            mounts=_require_storage_tool_mounts(
                                room=client or self.room,
                                local_paths=storage_tool_local_paths,
                                room_paths=storage_tool_room_paths,
                                default_room_mount=True,
                            )
                        )
                    ),
                )

            if self.shell_tool is not None:
                add_tool(toolkit_name=self.shell_tool.name, tool=self.shell_tool)
            if self.advanced_shell_toolkit is not None:
                add_toolkit(self.advanced_shell_toolkit)
            if require_mcp:
                if is_claude_model:
                    add_toolkit(AnthropicMessagesMCPToolkit())
                else:
                    add_toolkit(OpenAIResponsesMCPToolkit())

            if require_web_search:
                if is_claude_model:
                    add_tool(
                        toolkit_name="web_search",
                        tool=AnthropicWebSearchTool(),
                    )
                else:
                    add_tool(
                        toolkit_name="web_search",
                        tool=WebSearchTool(),
                    )

            if require_web_fetch:
                if is_claude_model:
                    add_tool(
                        toolkit_name="web_fetch",
                        tool=AnthropicWebFetchTool(),
                    )
                else:
                    add_tool(toolkit_name="web_fetch", tool=WebFetchTool())

            if require_storage:
                add_toolkit(
                    StorageToolkit(
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        )
                    )
                )

            if len(require_table_read) > 0:
                add_toolkit(
                    await make_dataset_toolkit(
                        room=self.room,
                        tables=require_table_read,
                        read_only=True,
                        namespace=dataset_namespace,
                    )
                )

            if require_time:
                add_toolkit(DatetimeToolkit())

            if require_uuid:
                add_toolkit(UUIDToolkit())

            if memory_selection is not None:
                memory_name, memory_namespace = memory_selection
                add_toolkit(
                    MemoriesToolkit(
                        room=self.room,
                        memory_name=memory_name,
                        namespace=memory_namespace,
                        llm_model=memory_model,
                    )
                )

            if len(require_table_write) > 0:
                add_toolkit(
                    await make_dataset_toolkit(
                        room=self.room,
                        tables=require_table_write,
                        read_only=False,
                        namespace=dataset_namespace,
                    )
                )

            if require_read_only_storage:
                add_toolkit(
                    StorageToolkit(
                        read_only=True,
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        ),
                    )
                )

            if require_document_authoring:
                add_toolkit(DocumentAuthoringToolkit(room=self.room))
                add_toolkit(
                    DocumentTypeAuthoringToolkit(
                        room=self.room,
                        schema=widget_schema,
                        document_type="widget",
                    )
                )

            if require_discovery:
                from meshagent.tools.discovery import DiscoveryToolkit

                add_toolkit(DiscoveryToolkit(room=self.room))

            tk = await super().get_thread_toolkits(
                thread_context=thread_context, participant=participant
            )

            return [*required_toolkits, *tk]

    return CustomChatbot


def build_process_agent(
    *,
    client: RoomClient | None = None,
    api_key: str | None = None,
    model: str,
    rule: List[str],
    toolkit: List[str],
    schema: List[str],
    computer_use: Optional[str] = None,
    discover_script_tools: Optional[bool] = None,
    storage_tool_local_paths: list[str] | None = None,
    storage_tool_room_paths: list[str] | None = None,
    default_room_storage_mount: bool = False,
    shell_tool_mounts: Optional[ContainerMountSpec] = None,
    require_image_generation: Optional[str] = None,
    require_shell: Optional[bool] = None,
    require_advanced_shell: Optional[bool] = None,
    require_apply_patch: Optional[str] = None,
    require_computer_use: Optional[str] = None,
    starting_url: Optional[str] = None,
    allow_goto_url: bool = False,
    require_web_search: Optional[str] = None,
    require_web_fetch: Optional[str] = None,
    require_mcp: Optional[str] = None,
    require_storage: Optional[str] = None,
    require_table_read: list[str] = None,
    require_table_write: list[str] = None,
    require_read_only_storage: Optional[str] = None,
    require_time: bool = True,
    require_uuid: bool = False,
    use_memory: Optional[str] = None,
    memory_model: Optional[str] = None,
    rules_file: Optional[list[str]] = None,
    instructions: Optional[list[str]] = None,
    room_rules_path: Optional[list[str]] = None,
    require_discovery: Optional[str] = None,
    require_document_authoring: Optional[str] = None,
    working_dir: Optional[str] = None,
    llm_participant: Optional[str] = None,
    decision_model: Optional[str] = None,
    dataset_namespace: Optional[list[str]] = None,
    always_reply: Optional[bool] = None,
    thread_dir: Optional[str] = None,
    context_management: ContextManagementMode = "auto",
    compaction_threshold: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    skill_dirs: Optional[list[str]] = None,
    threading_mode: ThreadingMode = "none",
    shell_image: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
    delegate_shell_token: Optional[bool] = None,
    shell_copy_env: Optional[list[str]] = None,
    shell_set_env: Optional[list[str]] = None,
    channels: Optional[list[str]] = None,
):
    from meshagent.agents import (
        DatasetThreadStorage,
        MessagingChatChannel,
        MailChannel,
        QueueChannel,
        SingleRoomAgent,
        ToolkitChannel,
    )
    from meshagent.agents.messages import TurnStart, TurnSteer
    from meshagent.agents.process import AgentSupervisor, LLMAgentProcess
    from meshagent.tools import RoomToolContext, Toolkit
    from meshagent.tools.hosting import _RemoteToolkitWrapper, start_hosted_toolkit

    requirements = []
    toolkits = []

    if storage_tool_local_paths is None:
        storage_tool_local_paths = []
    if storage_tool_room_paths is None:
        storage_tool_room_paths = []

    for t in toolkit:
        requirements.append(RequiredToolkit(name=t))

    for t in schema:
        requirements.append(RequiredSchema(name=t))

    client_rules = {}

    if rules_file is not None:
        for rules_path in rules_file:
            try:
                logger.info(f"loading rules from {rules_path}")
                with open(Path(os.path.expanduser(rules_path)).resolve(), "r") as f:
                    rules_config = RulesConfig.parse(f.read())
                    if rules_config.rules is not None:
                        rule.extend(rules_config.rules)
                    if rules_config.client_rules is not None:
                        client_rules.update(rules_config.client_rules)

            except FileNotFoundError:
                print(f"[yellow]rules file not found at {rules_path}[/yellow]")

    is_claude_model = model.startswith("claude-")
    supports_openai_tools = llm_participant is None and not is_claude_model
    base_shell_env = _copy_shell_env_vars(copy_env=shell_copy_env)
    base_shell_env.update(_set_shell_env_vars(set_env=shell_set_env))
    resolved_shell_image = resolve_shell_image(shell_image)
    if not supports_openai_tools:
        if require_image_generation:
            print("[red]image generation tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if require_apply_patch:
            print("[red]apply patch tool is only supported by openai models[/red]")
            raise typer.Exit(1)
        if computer_use or require_computer_use:
            print(
                "[red]computer use tool is currently only supported by openai models[/red]"
            )
            raise typer.Exit(1)

    memory_selection: Optional[tuple[str, Optional[list[str]]]] = None
    if use_memory is not None:
        memory_selection = parse_memory_selector(use_memory)

    resolved_channel_decision_model = (
        _normalized_decision_model(decision_model=decision_model) or "gpt-5.4-mini"
    )
    channel_llm_adapter = _build_decision_llm_adapter(
        decision_model=resolved_channel_decision_model,
        api_key=api_key,
        log_llm_requests=log_llm_requests,
    )

    if llm_participant:
        llm_adapter = MessageStreamLLMAdapter(
            participant_name=llm_participant,
        )
    else:
        if computer_use or require_computer_use:
            llm_adapter = OpenAIResponsesAdapter(
                model=model,
                api_key=api_key,
                response_options={
                    "reasoning": {"summary": "concise"},
                },
                log_requests=log_llm_requests,
                context_management=context_management,
                compaction_threshold=compaction_threshold,
                max_output_tokens=max_output_tokens,
            )
        else:
            if is_claude_model:
                llm_adapter = AnthropicOpenAIResponsesStreamAdapter(
                    model=model,
                    api_key=api_key,
                    log_requests=log_llm_requests,
                )
            else:
                llm_adapter = OpenAIResponsesAdapter(
                    model=model,
                    api_key=api_key,
                    log_requests=log_llm_requests,
                    context_management=context_management,
                    compaction_threshold=compaction_threshold,
                    max_output_tokens=max_output_tokens,
                )

    resolved_channels = _resolved_channels(
        runtime="process",
        channel=channels,
    )

    class CustomProcessAgent(SingleRoomAgent):
        def __init__(self) -> None:
            super().__init__(requires=requirements)
            self._skill_dirs = skill_dirs
            self._client_rules = client_rules if len(client_rules) > 0 else None
            self._supervisor: AgentSupervisor | None = None
            self._exposed_toolkits = []
            self._chat_channel: MessagingChatChannel | None = None
            self._mail_channels: list[MailChannel] = []
            self._queue_channels: list[QueueChannel] = []
            self._toolkit_channels: list[ToolkitChannel] = []
            self._shell_env: dict[str, str] = dict(base_shell_env)
            self._advanced_shell_toolkit: ContainerToolkit | None = None
            self._required_shell_tools: dict[str, BaseTool] = {}
            self._resolved_threading_mode: str | None = None
            if threading_mode != "none":
                self._resolved_threading_mode = threading_mode

        def _get_required_shell_tool(self, *, model: str) -> BaseTool:
            shell_tool = self._required_shell_tools.get(model)
            if shell_tool is None:
                shell_tool = build_shell_tool(
                    room=self.room,
                    model=model,
                    llm_participant=llm_participant,
                    name="shell",
                    working_dir=working_dir,
                    image=resolved_shell_image,
                    mounts=shell_tool_mounts,
                    env=self._shell_env,
                )
                self._required_shell_tools[model] = shell_tool
            return shell_tool

        async def _stop_cached_shell_tools(self) -> None:
            room = self._room
            cached_required_shell_tools = [*self._required_shell_tools.values()]
            self._required_shell_tools = {}

            if room is None:
                return

            stopped_tool_ids: set[int] = set()
            for tool in cached_required_shell_tools:
                if not isinstance(tool, ContainerShellTool):
                    continue
                tool_id = id(tool)
                if tool_id in stopped_tool_ids:
                    continue
                stopped_tool_ids.add(tool_id)
                await tool.stop(room=room)

        async def get_exposed_toolkits(self) -> list[Toolkit]:
            exposed_toolkits = await super().get_exposed_toolkits()
            channels = []
            if self._chat_channel is not None:
                channels.append(self._chat_channel)
            channels.extend(self._mail_channels)
            channels.extend(self._queue_channels)
            channels.extend(self._toolkit_channels)
            for channel in channels:
                if channel.state != "started":
                    continue
                exposed_toolkits.extend(channel.get_exposed_toolkits())
            return exposed_toolkits

        async def start(self, *, room: RoomClient) -> None:
            if self._room is not None:
                raise RoomException("agent is already started")

            self._room = room
            if require_mcp:
                await room.local_participant.set_attribute("supports_mcp", True)
            if _has_chat_channel(channels=resolved_channels):
                self._chat_channel = MessagingChatChannel(
                    room=room,
                    threading_mode=self._resolved_threading_mode,
                    thread_dir=thread_dir,
                    llm_adapter=channel_llm_adapter,
                )
            self._mail_channels = []
            for channel_spec in resolved_channels:
                if channel_spec[:5].casefold() != "mail:":
                    continue
                mail_config = _parse_mail_channel(channel=channel_spec)
                self._mail_channels.append(
                    MailChannel(
                        room=room,
                        queue_name=mail_config.queue_name,
                        email_address=mail_config.email_address,
                        threading_mode=self._resolved_threading_mode,
                        thread_dir=thread_dir,
                        llm_adapter=channel_llm_adapter,
                    )
                )
            self._queue_channels = []
            for channel_spec in resolved_channels:
                if channel_spec[:6].casefold() != "queue:":
                    continue
                queue_config = _parse_queue_channel(channel=channel_spec)
                self._queue_channels.append(
                    QueueChannel(
                        room=room,
                        queue_name=queue_config.queue_name,
                        threading_mode=self._resolved_threading_mode,
                        thread_dir=thread_dir,
                        llm_adapter=channel_llm_adapter,
                    )
                )
            self._toolkit_channels = []
            for channel_spec in resolved_channels:
                if channel_spec[:8].casefold() != "toolkit:":
                    continue
                toolkit_config = _parse_toolkit_channel(channel=channel_spec)
                self._toolkit_channels.append(
                    ToolkitChannel(
                        room=room,
                        toolkit_name=toolkit_config.toolkit_name,
                        thread_dir=thread_dir,
                    )
                )
            started_remote_toolkits: list[_RemoteToolkitWrapper] = []
            supervisor: AgentSupervisor | None = None

            try:
                await self.install_requirements()

                env = _build_shell_tool_env(
                    base_env=base_shell_env,
                    delegate_shell_token=delegate_shell_token,
                    room=room,
                )

                if require_shell:
                    self._shell_env = env

                if require_advanced_shell:
                    self._advanced_shell_toolkit = ContainerToolkit(
                        room=room,
                        working_dir=working_dir,
                        default_image=resolved_shell_image,
                        mounts=shell_tool_mounts,
                        env=env or None,
                    )

                if room_rules_path is not None:
                    for room_rules_file in room_rules_path:
                        await self._load_room_rules(path=room_rules_file)

                supervisor = _ProcessSupervisor(agent=self)
                if self._chat_channel is not None:
                    supervisor.add_channel(self._chat_channel)
                for mail_channel in self._mail_channels:
                    supervisor.add_channel(mail_channel)
                for queue_channel in self._queue_channels:
                    supervisor.add_channel(queue_channel)
                for toolkit_channel in self._toolkit_channels:
                    supervisor.add_channel(toolkit_channel)
                await supervisor.start()
                self._supervisor = supervisor

                self._exposed_toolkits = await self.get_exposed_toolkits()
                for toolkit in self._exposed_toolkits:
                    hosted_toolkit = await start_hosted_toolkit(
                        room=room,
                        toolkit=toolkit,
                    )
                    started_remote_toolkits.append(hosted_toolkit)
                self._hosted_exposed_toolkits = started_remote_toolkits
            except Exception:
                for toolkit in reversed(started_remote_toolkits):
                    await toolkit.stop()
                self._hosted_exposed_toolkits = []
                self._exposed_toolkits = []
                if supervisor is not None:
                    await supervisor.stop()
                self._supervisor = None
                self._chat_channel = None
                self._mail_channels = []
                self._queue_channels = []
                self._toolkit_channels = []
                self._advanced_shell_toolkit = None
                self._room = None
                raise

        async def stop(self) -> None:
            supervisor = self._supervisor
            self._supervisor = None
            if supervisor is not None:
                await supervisor.stop()
            self._chat_channel = None
            self._mail_channels = []
            self._queue_channels = []
            self._toolkit_channels = []
            room = self._room
            try:
                if self._advanced_shell_toolkit is not None and room is not None:
                    await self._advanced_shell_toolkit.stop_all(room=room)
                await self._stop_cached_shell_tools()
            finally:
                if require_mcp and room is not None:
                    await room.local_participant.set_attribute("supports_mcp", None)
                self._advanced_shell_toolkit = None
                self._shell_env = dict(base_shell_env)
                await super().stop()

        async def init_session(self) -> AgentSessionContext:
            from meshagent.cli.helper import init_context_from_spec

            context = llm_adapter.create_session()
            await init_context_from_spec(context)
            return context

        async def _load_room_rules(
            self,
            *,
            path: str,
            participant: Optional[Participant] = None,
        ) -> list[str]:
            rules: list[str] = []
            try:
                room_rules = await self.room.storage.download(path=path)

                rules_txt = room_rules.data.decode()
                rules_config = RulesConfig.parse(rules_txt)

                if rules_config.rules is not None:
                    rules.extend(rules_config.rules)

                if participant is not None:
                    client = participant.get_attribute("client")
                    if rules_config.client_rules is not None and client is not None:
                        selected_rules = rules_config.client_rules.get(client)
                        if selected_rules is not None:
                            rules.extend(selected_rules)
            except RoomException:
                logger.info(
                    f"unable to load rules from {path}, continuing with default rules"
                )

            return rules

        def get_skills_storage_toolkit(self) -> StorageToolkit | None:
            if require_storage:
                return StorageToolkit(
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    )
                )

            if require_read_only_storage:
                return StorageToolkit(
                    read_only=True,
                    mounts=_require_storage_tool_mounts(
                        room=client or self.room,
                        local_paths=storage_tool_local_paths,
                        room_paths=storage_tool_room_paths,
                        default_room_mount=default_room_storage_mount,
                    ),
                )

            return None

        async def get_rules(self, *, participant: Optional[Participant]) -> list[str]:
            rules = [*rule]
            storage_toolkit = self.get_skills_storage_toolkit()

            if self._skill_dirs is not None and len(self._skill_dirs) > 0:
                rules.append(
                    "You have access to to following skills which follow the agentskills spec:"
                )
                rules.append(
                    await to_prompt(
                        [*(Path(p) for p in self._skill_dirs)],
                        storage_toolkit=storage_toolkit,
                    )
                )
                rules.append(
                    "Use the shell or storage tool to find out more about skills and execute them when they are required"
                )

            if participant is not None:
                client = participant.get_attribute("client")
                if self._client_rules is not None and client is not None:
                    selected_rules = self._client_rules.get(client)
                    if selected_rules is not None:
                        rules.extend(selected_rules)

            if instructions is not None:
                for instructions_path in instructions:
                    rules.extend(
                        await _load_storage_rules(
                            path=instructions_path,
                            storage_toolkit=storage_toolkit,
                            participant=participant,
                        )
                    )

            if room_rules_path is not None:
                for room_rules_file in room_rules_path:
                    rules.extend(
                        await self._load_room_rules(
                            path=room_rules_file,
                            participant=participant,
                        )
                    )

            rules.append("based on the previous transcript, take your turn and respond")
            return rules

        async def get_process_turn_toolkits(
            self,
            *,
            process: LLMAgentProcess,
            sender: Participant | None,
            model: str,
            turns: list[TurnStart | TurnSteer],
        ) -> list[Toolkit]:
            built_required_toolkits: list[Toolkit] = []
            extra_toolkits: list[Toolkit] = []

            def add_toolkit(toolkit: Toolkit) -> None:
                built_required_toolkits.append(toolkit)

            def add_tool(*, toolkit_name: str, tool) -> None:
                add_toolkit(Toolkit(name=toolkit_name, tools=[tool]))

            if discover_script_tools:
                for script_tool in await get_script_tools(self.room):
                    add_tool(toolkit_name="script", tool=script_tool)

            if require_image_generation:
                add_tool(
                    toolkit_name="image_generation",
                    tool=ImageGenerationTool(
                        model=require_image_generation,
                        partial_images=3,
                    ),
                )

            if require_apply_patch:
                add_tool(
                    toolkit_name="apply_patch",
                    tool=ApplyPatchTool(
                        storage=StorageToolkit(
                            mounts=_require_storage_tool_mounts(
                                room=client or self.room,
                                local_paths=storage_tool_local_paths,
                                room_paths=storage_tool_room_paths,
                                default_room_mount=True,
                            )
                        )
                    ),
                )

            if require_shell:
                add_tool(
                    toolkit_name="shell",
                    tool=self._get_required_shell_tool(model=model),
                )
            if self._advanced_shell_toolkit is not None:
                add_toolkit(self._advanced_shell_toolkit)

            if require_mcp:
                if is_claude_model:
                    add_toolkit(AnthropicMessagesMCPToolkit())
                else:
                    add_toolkit(OpenAIResponsesMCPToolkit())

            if require_web_search:
                if is_claude_model:
                    add_tool(
                        toolkit_name="web_search",
                        tool=AnthropicWebSearchTool(),
                    )
                else:
                    add_tool(
                        toolkit_name="web_search",
                        tool=WebSearchTool(),
                    )

            if require_web_fetch:
                if is_claude_model:
                    add_tool(
                        toolkit_name="web_fetch",
                        tool=AnthropicWebFetchTool(),
                    )
                else:
                    add_tool(toolkit_name="web_fetch", tool=WebFetchTool())

            if require_storage:
                add_toolkit(
                    StorageToolkit(
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        )
                    )
                )

            if len(require_table_read) > 0:
                add_toolkit(
                    await make_dataset_toolkit(
                        room=self.room,
                        tables=require_table_read,
                        read_only=True,
                        namespace=dataset_namespace,
                    )
                )

            if require_time:
                add_toolkit(DatetimeToolkit())

            if require_uuid:
                add_toolkit(UUIDToolkit())

            if memory_selection is not None:
                memory_name, memory_namespace = memory_selection
                add_toolkit(
                    MemoriesToolkit(
                        room=self.room,
                        memory_name=memory_name,
                        namespace=memory_namespace,
                        llm_model=memory_model,
                    )
                )

            if len(require_table_write) > 0:
                add_toolkit(
                    await make_dataset_toolkit(
                        room=self.room,
                        tables=require_table_write,
                        read_only=False,
                        namespace=dataset_namespace,
                    )
                )

            if require_read_only_storage:
                add_toolkit(
                    StorageToolkit(
                        read_only=True,
                        mounts=_require_storage_tool_mounts(
                            room=client or self.room,
                            local_paths=storage_tool_local_paths,
                            room_paths=storage_tool_room_paths,
                            default_room_mount=default_room_storage_mount,
                        ),
                    )
                )

            if require_document_authoring:
                add_toolkit(DocumentAuthoringToolkit(room=self.room))
                add_toolkit(
                    DocumentTypeAuthoringToolkit(
                        room=self.room,
                        schema=widget_schema,
                        document_type="widget",
                    )
                )

            if require_discovery:
                from meshagent.tools.discovery import DiscoveryToolkit

                add_toolkit(DiscoveryToolkit(room=self.room))

            if require_computer_use:
                from meshagent.agents.images_dataset import ImagesDataset
                from meshagent.agents.messages import (
                    AGENT_EVENT_THREAD_EVENT,
                    AgentThreadEvent,
                )
                from meshagent.computers.agent import ComputerToolkit

                images_dataset = ImagesDataset(self.room.datasets)
                computer_toolkit: ComputerToolkit | None = None

                async def render_screen(image_bytes: bytes) -> None:
                    thread_storage = process.thread_storage
                    thread_id = process.thread_id
                    if thread_storage is None or thread_id is None:
                        return

                    created_by = self.room.local_participant.get_attribute("name")
                    if not isinstance(created_by, str):
                        created_by = ""

                    try:
                        saved_image = await images_dataset.save(
                            data=image_bytes,
                            mime_type="image/png",
                            created_by=created_by,
                            annotations={
                                "source": "computer_toolkit",
                                "thread_path": thread_id,
                            },
                        )
                    except Exception as ex:
                        logger.error(
                            "failed to persist computer screenshot", exc_info=ex
                        )
                        return

                    width: int | float | None = None
                    height: int | float | None = None
                    if computer_toolkit is not None:
                        width, height = computer_toolkit.computer.dimensions

                    thread_storage.push_message(
                        message=AgentThreadEvent(
                            type=AGENT_EVENT_THREAD_EVENT,
                            thread_id=thread_id,
                            event={
                                "type": "computer.screenshot",
                                "uri": f"dataset://{ImagesDataset.TABLE_NAME}?id={saved_image.id}",
                                "mime_type": saved_image.mime_type,
                                "created_at": saved_image.created_at,
                                "created_by": saved_image.created_by,
                                "width": width,
                                "height": height,
                                "status": "completed",
                            },
                        )
                    )

                computer_toolkit = ComputerToolkit(
                    room=self.room,
                    render_screen=render_screen,
                    starting_url=starting_url,
                    include_goto_tool=allow_goto_url,
                )
                extra_toolkits.append(computer_toolkit)

            def handle_tool_event(event: dict) -> None:
                thread_storage = process.thread_storage
                if thread_storage is not None and process.thread_id is not None:
                    thread_storage.push_message(
                        message=AgentThreadEvent(
                            type=AGENT_EVENT_THREAD_EVENT,
                            thread_id=process.thread_id,
                            event=event,
                        )
                    )

            required_toolkits = await self.get_required_toolkits(
                context=RoomToolContext(
                    room=self.room,
                    caller=self.room.local_participant,
                    on_behalf_of=sender,
                    event_handler=handle_tool_event,
                )
            )

            combined_toolkits: list[Toolkit] = [*toolkits]
            combined_toolkits.extend(built_required_toolkits)
            combined_toolkits.extend(required_toolkits)
            combined_toolkits.extend(extra_toolkits)
            if process.thread_storage is not None:
                combined_toolkits.append(process.thread_storage.make_toolkit())
            return combined_toolkits

    class _ProcessSupervisor(AgentSupervisor):
        def __init__(self, *, agent: CustomProcessAgent) -> None:
            super().__init__()
            self._agent = agent
            self._local_event_queues: list[asyncio.Queue[Message]] = []

        def subscribe_local_events(self) -> asyncio.Queue[Message]:
            queue: asyncio.Queue[Message] = asyncio.Queue()
            self._local_event_queues.append(queue)
            return queue

        def unsubscribe_local_events(self, queue: asyncio.Queue[Message]) -> None:
            if queue in self._local_event_queues:
                self._local_event_queues.remove(queue)

        def send(self, message: Message) -> None:
            if message.source is not None:
                for queue in [*self._local_event_queues]:
                    queue.put_nowait(message)
            super().send(message)

        async def on_models_request(self, message: Message) -> None:
            if not isinstance(message.data, ModelsRequest):
                return
            provider_name = llm_adapter.provider_name()
            provider = LLMProvider(
                name=provider_name.strip()
                if provider_name is not None and provider_name.strip() != ""
                else "default",
                adapter=llm_adapter,
            )
            default_model = llm_adapter.default_model()
            self._send_to_channels(
                Message(
                    data=ModelsResponse(
                        type=AGENT_MESSAGE_MODELS_RESPONSE,
                        source_message_id=message.data.message_id,
                        providers=[
                            agent_provider_info(
                                provider=provider,
                                current_provider=provider.name,
                                current_model=default_model,
                            )
                        ],
                    ),
                    sender=message.sender,
                )
            )

        async def validate_turn_start(self, turn_start: TurnStart) -> AgentError | None:
            provider_name = llm_adapter.provider_name()
            normalized_provider = (
                provider_name.strip()
                if provider_name is not None and provider_name.strip() != ""
                else "default"
            )
            if (
                turn_start.provider is not None
                and turn_start.provider.strip() != ""
                and turn_start.provider != normalized_provider
            ):
                return AgentError(
                    message=f"unknown provider {turn_start.provider!r}",
                    code="unknown_provider",
                )

            model = turn_start.model
            if model is None or model.strip() == "":
                return None

            models = llm_adapter.list_models()
            if not any(model_info.name == model for model_info in models):
                names = ", ".join(model_info.name for model_info in models)
                return AgentError(
                    message=(
                        f"unknown model {model!r} for provider {normalized_provider!r}; "
                        f"available models: {names}"
                    ),
                    code="unknown_model",
                )
            return None

        def create_thread_process(self, thread_id: str) -> LLMAgentProcess:
            normalized_thread_id = thread_id.strip()
            if not normalized_thread_id.startswith("dataset://"):
                normalized_thread_id = f"dataset://{normalized_thread_id.lstrip('/')}"

            async def _turn_instructions_provider(
                participant: Participant | None,
            ) -> str | None:
                rules = await self._agent.get_rules(participant=participant)
                if len(rules) == 0:
                    return None

                return "\n".join(rules)

            process = LLMAgentProcess(
                thread_id=thread_id,
                participant=self._agent.room.local_participant,
                llm_adapter=llm_adapter,
                toolkits=[*toolkits],
                thread_storage=DatasetThreadStorage(
                    room=self._agent.room,
                    path=normalized_thread_id,
                ),
                session_initializer=self._agent.init_session,
                turn_instructions_provider=_turn_instructions_provider,
                turn_toolkits_builder=lambda sender, model, turns: (
                    self._agent.get_process_turn_toolkits(
                        process=process,
                        sender=sender,
                        model=model,
                        turns=turns,
                    )
                ),
            )
            process.register_content_scheme(_room_content_scheme(room=self._agent.room))
            return process

    return CustomProcessAgent


@app.async_command("join", help="Join a room and run a chatbot agent.")
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
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    instructions: InstructionsOption = [],
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
            "--toolkit",
            "-t",
            help="the name or url of a required toolkit",
            hidden=True,
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.6-sol",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable computer use"),
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_tool_config_mount: ShellConfigMountOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use",
            hidden=True,
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_advanced_shell: RequireAdvancedShellOption = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Use a specific dataset namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            "--uuid",
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    decision_model: DecisionModelOption = None,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = None,
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = None,
    channel: ChannelOption = [],
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    runtime = _current_command_runtime()
    resolved_channels = _resolved_channels(runtime=runtime, channel=channel)
    _require_process_channels(runtime=runtime, channels=resolved_channels)
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    resolved_dataset_namespace = _resolved_dataset_namespace(
        runtime=runtime,
        dataset_namespace=dataset_namespace,
    )
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        require_toolkit=require_toolkit,
        schema=schema,
        require_schema=require_schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=computer_use,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        advanced_shell=require_advanced_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        mcp=mcp,
        require_mcp=require_mcp,
        storage=storage,
        require_storage=require_storage,
    )

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        token_env = token_from_env or "MESHAGENT_TOKEN"
        jwt = os.getenv(token_env)
        if jwt is None:
            if token_from_env:
                print(
                    f"[bold red]{token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)
            if agent_name is None:
                print(
                    f"[bold red]--agent-name must be specified when the {token_env} environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            jwt = await mint_participant_token_for_cli(
                project_id=project_id,
                name=agent_name,
                room_name=room,
                role=role,
                api_scope=ApiScope.agent_default(tunnels=require_computer_use),
                key=key,
            )

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        default_room_storage_mount = bool(
            normalized_tool_options["require_storage"] or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            empty_dir_paths=merge_option_lists(
                shell_empty_dir_mount,
                shell_tool_empty_dir,
            ),
            config_paths=shell_tool_config_mount,
            image_paths=shell_image_mount,
        )
        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )

        CustomChatbot = _build_runtime_agent(
            client=client,
            api_key=jwt,
            runtime=runtime,
            normalized_tool_options=normalized_tool_options,
            model=model,
            rule=rule,
            rules_file=rules_file,
            instructions=instructions,
            discover_script_tools=discover_script_tools,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            require_advanced_shell=require_advanced_shell,
            llm_participant=llm_participant,
            decision_model=decision_model,
            always_reply=always_reply,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
        )

        bot = CustomChatbot()

        if get_deferred():
            from meshagent.cli.host import agents

            agents.append((bot, jwt))
        else:

            async def run_join_session(client: RoomClient) -> None:
                print(
                    f"[bold green]Open the studio to interact with your agent: {meshagent_base_url().replace('api.', 'studio.')}/projects/{project_id}/rooms/{client.room_name}[/bold green]",
                    flush=True,
                )
                await client.protocol.wait_for_close()

            try:
                await _run_agent_room_session(
                    client=client,
                    bot=bot,
                    runner=run_join_session,
                )
            except KeyboardInterrupt:
                return
            except asyncio.CancelledError:
                return
    finally:
        await account_client.close()


_HIDDEN_REQUIRE_OPTION_NAMES = DUPLICATE_REQUIRE_OPTION_NAMES | {
    "require_computer_use",
}


@app.async_command("service")
async def service(
    *,
    agent_name: Annotated[str, typer.Option(..., help="Name of the agent to call")],
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    instructions: InstructionsOption = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
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
            "--toolkit",
            "-t",
            help="the name or url of a required toolkit",
            hidden=True,
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.6-sol",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable computer use"),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_tool_config_mount: ShellConfigMountOption = [],
    shell_image_mount: Annotated[
        List[str],
        typer.Option(
            "--shell-image-mount",
            help="Mount image as <image>=<mount>[:ro|rw]",
        ),
    ] = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use",
            hidden=True,
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_advanced_shell: RequireAdvancedShellOption = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Use a specific dataset namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            "--uuid",
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    decision_model: DecisionModelOption = None,
    host: Annotated[
        Optional[str], typer.Option(help="Host to bind the service on")
    ] = None,
    port: Annotated[
        Optional[int], typer.Option(help="Port to bind the service on")
    ] = None,
    path: Annotated[
        Optional[str], typer.Option(help="HTTP path to mount the service at")
    ] = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = None,
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = None,
    channel: ChannelOption = [],
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    runtime = _current_command_runtime()
    resolved_channels = _resolved_channels(runtime=runtime, channel=channel)
    _require_process_channels(runtime=runtime, channels=resolved_channels)
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    resolved_dataset_namespace = _resolved_dataset_namespace(
        runtime=runtime,
        dataset_namespace=dataset_namespace,
    )
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        require_toolkit=require_toolkit,
        schema=schema,
        require_schema=require_schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=computer_use,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        advanced_shell=require_advanced_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        mcp=mcp,
        require_mcp=require_mcp,
        storage=storage,
        require_storage=require_storage,
    )

    service = get_service(host=host, port=port)
    default_room_storage_mount = bool(
        normalized_tool_options["require_storage"] or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
        config_paths=shell_tool_config_mount,
        image_paths=shell_image_mount,
    )

    if path is None:
        path = "/agent"
        i = 0
        while service.has_path(path):
            i += 1
            path = f"/agent{i}"

    service.agents.append(
        AgentSpec(
            name=agent_name,
            annotations=_agent_annotations_for_runtime(
                runtime=runtime,
                threading_mode=threading_mode,
                thread_dir=thread_dir,
                channel=resolved_channels,
            ),
        )
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=_build_runtime_agent(
            client=None,
            runtime=runtime,
            normalized_tool_options=normalized_tool_options,
            model=model,
            rule=rule,
            rules_file=rules_file,
            instructions=instructions,
            discover_script_tools=discover_script_tools,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            require_advanced_shell=require_advanced_shell,
            llm_participant=llm_participant,
            decision_model=decision_model,
            always_reply=always_reply,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
        ),
    )

    if not get_deferred():
        await run_services()


@app.async_command("spec", help="Generate a service spec for deploying a chatbot.")
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
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    instructions: InstructionsOption = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
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
            "--toolkit",
            "-t",
            help="the name or url of a required toolkit",
            hidden=True,
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.6-sol",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable computer use"),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_tool_config_mount: ShellConfigMountOption = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use",
            hidden=True,
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_advanced_shell: RequireAdvancedShellOption = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Use a specific dataset namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            "--uuid",
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    decision_model: DecisionModelOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = None,
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = None,
    channel: ChannelOption = [],
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
):
    runtime = _current_command_runtime()
    resolved_channels = _resolved_channels(runtime=runtime, channel=channel)
    _require_process_channels(runtime=runtime, channels=resolved_channels)
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    resolved_dataset_namespace = _resolved_dataset_namespace(
        runtime=runtime,
        dataset_namespace=dataset_namespace,
    )
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        require_toolkit=require_toolkit,
        schema=schema,
        require_schema=require_schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=computer_use,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        advanced_shell=require_advanced_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        mcp=mcp,
        require_mcp=require_mcp,
        storage=storage,
        require_storage=require_storage,
    )

    service = get_service(host=None, port=None)
    default_room_storage_mount = bool(
        normalized_tool_options["require_storage"] or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
        config_paths=shell_tool_config_mount,
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    service.agents.append(
        AgentSpec(
            name=agent_name,
            annotations=_agent_annotations_for_runtime(
                runtime=runtime,
                threading_mode=threading_mode,
                thread_dir=thread_dir,
                channel=resolved_channels,
            ),
        )
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=_build_runtime_agent(
            client=None,
            runtime=runtime,
            normalized_tool_options=normalized_tool_options,
            model=model,
            rule=rule,
            rules_file=rules_file,
            instructions=instructions,
            discover_script_tools=discover_script_tools,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            require_advanced_shell=require_advanced_shell,
            llm_participant=llm_participant,
            decision_model=decision_model,
            always_reply=always_reply,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
        ),
    )

    spec = service_specs(token_identity=agent_name)[0]
    spec.ports = []
    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }

    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            runtime,
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
    )

    print(yaml.dump(spec.model_dump(mode="json", exclude_none=True), sort_keys=False))


@app.async_command("deploy", help="Deploy a chatbot service to a project or room.")
async def deploy(
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
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    rules_file: Optional[list[str]] = None,
    instructions: InstructionsOption = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
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
            "--toolkit",
            "-t",
            help="the name or url of a required toolkit",
            hidden=True,
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.6-sol",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable computer use"),
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_tool_config_mount: ShellConfigMountOption = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use",
            hidden=True,
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_advanced_shell: RequireAdvancedShellOption = False,
    require_apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Use a specific dataset namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            "--uuid",
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable document authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    decision_model: DecisionModelOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = None,
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = None,
    channel: ChannelOption = [],
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    project_id: ProjectIdOption,
    room: Annotated[
        Optional[str],
        typer.Option("--room", help="The name of a room to create the service for"),
    ] = os.getenv("MESHAGENT_ROOM"),
):
    runtime = _current_command_runtime()
    resolved_channels = _resolved_channels(runtime=runtime, channel=channel)
    _require_process_channels(runtime=runtime, channels=resolved_channels)
    resolved_service_name = service_name if service_name is not None else agent_name
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    project_id = await resolve_project_id(project_id=project_id)

    resolved_dataset_namespace = _resolved_dataset_namespace(
        runtime=runtime,
        dataset_namespace=dataset_namespace,
    )
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        require_toolkit=require_toolkit,
        schema=schema,
        require_schema=require_schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=computer_use,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        advanced_shell=require_advanced_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        mcp=mcp,
        require_mcp=require_mcp,
        storage=storage,
        require_storage=require_storage,
    )

    service = get_service(host=None, port=None)
    default_room_storage_mount = bool(
        normalized_tool_options["require_storage"] or require_read_only_storage
    )
    shell_tool_mounts = parse_shell_tool_mounts(
        room_paths=merge_option_lists(
            shell_room_mount,
            shell_tool_room_path,
        ),
        empty_dir_paths=merge_option_lists(
            shell_empty_dir_mount,
            shell_tool_empty_dir,
        ),
        config_paths=shell_tool_config_mount,
    )

    path = "/agent"
    i = 0
    while service.has_path(path):
        i += 1
        path = f"/agent{i}"

    service.agents.append(
        AgentSpec(
            name=agent_name,
            annotations=_agent_annotations_for_runtime(
                runtime=runtime,
                threading_mode=threading_mode,
                thread_dir=thread_dir,
                channel=resolved_channels,
            ),
        )
    )

    service.add_path(
        identity=agent_name,
        path=path,
        cls=_build_runtime_agent(
            client=None,
            runtime=runtime,
            normalized_tool_options=normalized_tool_options,
            model=model,
            rule=rule,
            rules_file=rules_file,
            instructions=instructions,
            discover_script_tools=discover_script_tools,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            require_advanced_shell=require_advanced_shell,
            llm_participant=llm_participant,
            decision_model=decision_model,
            always_reply=always_reply,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
        ),
    )

    spec = service_specs(token_identity=agent_name)[0]
    spec.ports = []

    spec.metadata.annotations = {
        "meshagent.service.id": resolved_service_name,
    }

    spec.metadata.name = resolved_service_name
    spec.metadata.description = service_description
    spec.container.image = "meshagent/cli:default"
    spec.container.command = shlex.join(
        [
            "meshagent",
            runtime,
            "join",
            *cleanup_args_strip_options(
                cleanup_args(sys.argv[2:]),
                ["--host", "--path"],
            ),
        ]
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


async def chat_with(
    *,
    participant_name: str,
    project_id: str,
    room: str,
    thread_path: Optional[str],
    message: Optional[str] = None,
):
    from meshagent.cli import ask as ask_module

    try:
        from textual import events
        from textual._context import active_app
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.widgets import OptionList, Static, TextArea
        from textual.widgets.option_list import Option
        from rich.align import Align
        from rich.console import Group
        from rich.console import RenderableType
        from rich.markdown import Markdown
        from rich.padding import Padding
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.table import Table
        from rich.text import Text
    except ImportError as exc:
        print(
            "[bold red]Textual is required for chatbot UI. Install meshagent-cli dependencies and retry.[/bold red]"
        )
        raise typer.Exit(1) from exc

    def _suppress_textual_debug_features() -> None:
        raw_features = os.environ.get("TEXTUAL")
        if raw_features is None or raw_features.strip() == "":
            return

        parsed = [
            value.strip() for value in raw_features.split(",") if value.strip() != ""
        ]
        if len(parsed) == 0:
            return

        filtered = [
            value for value in parsed if value.lower() not in ("debug", "devtools")
        ]
        if len(filtered) == len(parsed):
            return

        if len(filtered) == 0:
            os.environ.pop("TEXTUAL", None)
        else:
            os.environ["TEXTUAL"] = ",".join(filtered)

    caret = "›"

    class _ChatWithClient:
        def __init__(
            self,
            *,
            room: RoomClient,
            participant_name: str,
            thread_path: str,
        ) -> None:
            self.room = room
            self.participant_name = participant_name
            self.thread_path = thread_path
            self._client = MessagingChatClient(
                room=room,
                participant_name=participant_name,
            )
            self._thread: ChatThreadSession | None = None
            self._session: ask_module._AgentMessageSession | None = None
            self._doc = None
            self._responses: asyncio.Queue[str] = asyncio.Queue()

        @property
        def doc(self):
            return self._doc

        @property
        def thread_status_text(self) -> str | None:
            if self._thread is None:
                return None
            return self._thread.thread_status_text

        async def __aenter__(self) -> "_ChatWithClient":
            self._doc = await self.room.sync.open(path=self.thread_path)
            await self._client.__aenter__()
            local_participant_name = self.room.local_participant.get_attribute("name")
            self._thread = await self._client.open_thread(
                self.thread_path,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
            )
            self._session = ask_module._AgentMessageSession(
                client=self._thread,
                model=None,
                local_participant_name=local_participant_name,
            )
            return self

        async def __aexit__(self, exc_type, exc, exc_tb) -> None:
            del exc_type, exc, exc_tb
            if self._thread is not None:
                await self._thread.__aexit__(None, None, None)
                self._thread = None
            if self._doc is not None:
                await self.room.sync.close(path=self.thread_path)
                self._doc = None

        async def clear(self) -> None:
            return

        async def cancel(self) -> None:
            if self._session is not None:
                self._session.interrupt()

        async def send_approval_decision(
            self, *, approval_id: str, approve: bool
        ) -> None:
            del approval_id, approve
            raise RoomException("tool approval is not available on this chat client")

        async def send(self, *, text: str) -> None:
            if self._session is None:
                raise RoomException("chat client not started")
            response = await self._session.ask(prompt=text)
            self._responses.put_nowait(response)

        async def receive(self) -> str:
            return await self._responses.get()

    class RoomConnectTextualApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            align: center middle;
            padding: 0 2;
        }
        #connect-title {
            content-align: center middle;
            width: 100%;
        }
        #connect-title-gap-top {
            width: 100%;
            height: 1;
        }
        #connect-title-divider {
            width: 100%;
            content-align: center middle;
        }
        #connect-title-gap-bottom {
            width: 100%;
            height: 1;
        }
        #connect-header {
            content-align: center middle;
            width: 100%;
            padding: 0 0 1 0;
        }
        #connect-status {
            content-align: center middle;
            width: 100%;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "cancel_connect", "Cancel", priority=True),
        ]

        def __init__(
            self,
            *,
            room_name: str,
            status_queue: "asyncio.Queue[tuple[str, str]]",
            connect_task: "asyncio.Task[RoomClient]",
        ) -> None:
            super().__init__()
            self._room_name = room_name
            self._status_queue = status_queue
            self._connect_task = connect_task
            self._divider_view: Static | None = None
            self._header_view: Static | None = None
            self._status_view: Static | None = None
            self._consume_task: asyncio.Task | None = None
            self._watch_task: asyncio.Task | None = None
            self._spinner_timer = None
            self._spinner_frames = (
                "⠋",
                "⠙",
                "⠹",
                "⠸",
                "⠼",
                "⠴",
                "⠦",
                "⠧",
                "⠇",
                "⠏",
            )
            self._spinner_frame = 0
            self._divider_pulse_position = 0
            self._divider_pulse_direction = 1
            self._statuses: list[tuple[str, str]] = []
            self._connect_complete = False
            self._connect_failed = False

        def compose(self) -> ComposeResult:
            yield Static(Text("MeshAgent", style="bold green"), id="connect-title")
            yield Static(" ", id="connect-title-gap-top")
            yield Static(Rule(style="bright_black"), id="connect-title-divider")
            yield Static(" ", id="connect-title-gap-bottom")
            yield Static("", id="connect-header")
            yield Static("", id="connect-status")

        async def on_mount(self) -> None:
            self._divider_view = self.query_one("#connect-title-divider", Static)
            self._header_view = self.query_one("#connect-header", Static)
            self._status_view = self.query_one("#connect-status", Static)
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._consume_task = asyncio.create_task(self._consume_statuses())
            self._watch_task = asyncio.create_task(self._watch_connect_task())
            self._render_title_divider()
            self._render_statuses()

        async def on_unmount(self) -> None:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            if self._consume_task is not None:
                if not self._consume_task.done():
                    self._consume_task.cancel()
                await asyncio.gather(self._consume_task, return_exceptions=True)
                self._consume_task = None
            if self._watch_task is not None:
                if not self._watch_task.done():
                    self._watch_task.cancel()
                await asyncio.gather(self._watch_task, return_exceptions=True)
                self._watch_task = None

        async def action_cancel_connect(self) -> None:
            if not self._connect_task.done():
                self._connect_task.cancel()
            self.exit()

        async def _consume_statuses(self) -> None:
            try:
                while True:
                    status, message = await self._status_queue.get()
                    self._push_status(status=status, message=message)
            except asyncio.CancelledError:
                return

        async def _watch_connect_task(self) -> None:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                return
            except Exception as ex:
                self._connect_failed = True
                self._push_status(status="error", message=str(ex))
                await asyncio.sleep(0.75)
            else:
                self._connect_complete = True
                await asyncio.sleep(0.2)
            finally:
                self.exit()

        def _on_spinner_tick(self) -> None:
            if self._connect_complete or self._connect_failed:
                return
            if len(self._spinner_frames) == 0:
                return
            self._spinner_frame = (self._spinner_frame + 1) % len(self._spinner_frames)
            self._advance_title_divider_pulse()
            self._advance_title_divider_pulse()
            self._render_title_divider()
            self._render_statuses()

        def _title_divider_length(self) -> int:
            available_width = max(self.size.width - 4, 1)
            return max(8, int(round(available_width * 0.3)))

        def _advance_title_divider_pulse(self) -> None:
            divider_length = self._title_divider_length()
            pulse_width = max(2, min(5, divider_length // 6))
            max_position = max(0, divider_length - pulse_width)

            next_position = self._divider_pulse_position + self._divider_pulse_direction
            if next_position >= max_position:
                self._divider_pulse_position = max_position
                self._divider_pulse_direction = -1
                return
            if next_position <= 0:
                self._divider_pulse_position = 0
                self._divider_pulse_direction = 1
                return

            self._divider_pulse_position = next_position

        def _render_title_divider(self) -> None:
            if self._divider_view is None:
                return

            divider_length = self._title_divider_length()
            pulse_width = max(2, min(5, divider_length // 6))
            max_position = max(0, divider_length - pulse_width)
            pulse_start = min(max(self._divider_pulse_position, 0), max_position)
            pulse_end = min(divider_length, pulse_start + pulse_width)

            divider_text = Text(
                "─" * divider_length, style="bright_black", justify="center"
            )
            if pulse_end > pulse_start:
                divider_text.stylize("bold green", pulse_start, pulse_end)
            self._divider_view.update(divider_text)

        def _push_status(self, *, status: str, message: str) -> None:
            normalized_status = status.strip() if isinstance(status, str) else ""
            normalized_message = message.strip() if isinstance(message, str) else ""
            if normalized_message == "":
                normalized_message = normalized_status.replace("_", " ").strip()
            if normalized_message == "":
                normalized_message = "connecting to room"

            if len(self._statuses) > 0 and self._statuses[-1][0] == normalized_status:
                self._statuses[-1] = (normalized_status, normalized_message)
            else:
                self._statuses.append((normalized_status, normalized_message))

            if len(self._statuses) > 12:
                self._statuses = self._statuses[-12:]
            self._render_statuses()

        def _render_statuses(self) -> None:
            if self._header_view is None or self._status_view is None:
                return

            self._header_view.update(
                Text(
                    f"Connecting to room '{self._room_name}'...",
                    style="bold",
                    justify="center",
                )
            )

            if len(self._statuses) == 0:
                lines = Text(justify="center")
                lines.append(
                    f"{self._spinner_frames[self._spinner_frame]} ", style="cyan"
                )
                lines.append("connecting to room")
                self._status_view.update(lines)
                return

            body = Text(justify="center")
            last_index = len(self._statuses) - 1
            for index, (status, message) in enumerate(self._statuses):
                active = index == last_index and not self._connect_complete
                prefix_style = "dim"
                prefix = "• "
                if index == last_index:
                    if self._connect_failed:
                        prefix = "✖ "
                        prefix_style = "bold red"
                    elif self._connect_complete:
                        prefix = "✓ "
                        prefix_style = "bold green"
                    elif active:
                        prefix = f"{self._spinner_frames[self._spinner_frame]} "
                        prefix_style = "cyan"

                body.append(prefix, style=prefix_style)
                body.append(message)

                status_label = status.replace("_", " ").strip()
                if status_label != "" and status_label != message:
                    body.append(f" ({status_label})", style="dim")

                if index < last_index:
                    body.append("\n")

            self._status_view.update(body)

    class ChatWithTextualApp(App[None]):
        CSS = """
        Screen {
            layout: grid;
            grid-size: 1 3;
            grid-rows: 1fr auto auto;
            padding: 0;
        }
        #messages-scroll {
            height: 1fr;
            padding: 0;
            align: left bottom;
        }
        #messages {
            content-align: left bottom;
            width: 100%;
        }
        #approval-panel {
            display: none;
            background: #252525;
            padding: 1 2;
        }
        #approval-header {
            width: 100%;
            color: white;
        }
        #approval-details {
            width: 100%;
            color: $text-muted;
            margin: 0 0 1 0;
        }
        #approval-actions {
            border: none;
            height: auto;
            max-height: 2;
            padding: 0;
            margin: 0;
            background: #252525;
        }
        #approval-actions > .option-list--option {
            background: #252525;
            color: white;
            padding: 0 1;
        }
        #approval-actions > .option-list--option-highlighted {
            background: #3f3f3f;
            color: white;
            text-style: bold;
        }
        #input-row {
            margin: 0;
            background: #2f2f2f;
            padding: 1 0 1 0;
        }
        #input-prompt {
            width: 2;
            height: auto;
            content-align: center top;
            color: $text-muted;
            background: #2f2f2f;
        }
        #chat-input {
            width: 1fr;
            height: 1;
            min-height: 1;
            max-height: 6;
            border: none;
            outline: none;
            padding: 0;
            margin: 0;
            color: white;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #chat-input:focus {
            border: none;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #chat-input .text-area--cursor-line {
            background: #2f2f2f;
        }
        #chat-input .text-area--gutter {
            background: #2f2f2f;
        }
        #chat-input .text-area--cursor-gutter {
            background: #2f2f2f;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit_app", "Quit", priority=True),
            Binding("enter", "submit_chat_input", "Send", priority=True),
            Binding("escape", "cancel_turn", "Cancel"),
            Binding("ctrl+l", "clear_thread", "Clear thread"),
            Binding("left", "select_previous_approval", show=False),
            Binding("right", "select_next_approval", show=False),
        ]

        def __init__(
            self,
            *,
            chat_client: _ChatWithClient,
            participant_name: str,
            local_user_name: str,
        ) -> None:
            super().__init__()
            self._chat_client = chat_client
            self._participant_name = participant_name
            self._local_user_name = local_user_name
            self._messages_view: Static | None = None
            self._messages_scroll: VerticalScroll | None = None
            self._approval_panel: Vertical | None = None
            self._approval_header_view: Static | None = None
            self._approval_details_view: Static | None = None
            self._approval_actions: OptionList | None = None
            self._chat_input: TextArea | None = None
            self._chat_input_height = 1
            self._doc_watch_task: asyncio.Task | None = None
            self._doc_changed = asyncio.Event()
            self._spinner_frames = (
                "⠋",
                "⠙",
                "⠹",
                "⠸",
                "⠼",
                "⠴",
                "⠦",
                "⠧",
                "⠇",
                "⠏",
            )
            self._spinner_frame = 0
            self._spinner_timer = None
            self._has_active_events = False
            self._pending_approval_items: list[tuple[str, str, str]] = []
            self._selected_pending_approval_index = 0

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="messages-scroll"):
                yield Static("", id="messages")
            with Vertical(id="approval-panel"):
                yield Static("", id="approval-header")
                yield Static("", id="approval-details")
                yield OptionList(
                    Option("[A]pprove", id="approve"),
                    Option("[D]eny", id="deny"),
                    id="approval-actions",
                )
            with Horizontal(id="input-row"):
                yield Static(caret, id="input-prompt")
                yield TextArea(
                    "",
                    id="chat-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                )

        async def on_mount(self) -> None:
            self._messages_view = self.query_one("#messages", Static)
            self._messages_scroll = self.query_one("#messages-scroll", VerticalScroll)
            self._approval_panel = self.query_one("#approval-panel", Vertical)
            self._approval_header_view = self.query_one("#approval-header", Static)
            self._approval_details_view = self.query_one("#approval-details", Static)
            self._approval_actions = self.query_one("#approval-actions", OptionList)
            self._chat_input = self.query_one("#chat-input", TextArea)
            self._chat_input.focus()
            self._resize_chat_input(self._chat_input)
            self._render_approval_menu()
            self._bind_thread_document_events()
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._doc_watch_task = asyncio.create_task(self._watch_thread_document())
            self._doc_changed.set()

        async def on_unmount(self) -> None:
            self._stop_spinner_timer()
            await self._stop_doc_watch_loop()

        async def action_clear_thread(self) -> None:
            await self._chat_client.clear()

        async def action_cancel_turn(self) -> None:
            await self._chat_client.cancel()

        async def action_quit_app(self) -> None:
            self._stop_spinner_timer()
            await self._stop_doc_watch_loop()
            self.exit()

        async def action_select_previous_approval(self) -> None:
            count = len(self._pending_approval_items)
            if count == 0:
                return
            self._selected_pending_approval_index = (
                self._selected_pending_approval_index - 1
            ) % count
            self._render_approval_menu()

        async def action_select_next_approval(self) -> None:
            count = len(self._pending_approval_items)
            if count == 0:
                return
            self._selected_pending_approval_index = (
                self._selected_pending_approval_index + 1
            ) % count
            self._render_approval_menu()

        async def action_approve_selected_approval(self) -> None:
            await self._submit_selected_approval_decision(approve=True)

        async def action_reject_selected_approval(self) -> None:
            await self._submit_selected_approval_decision(approve=False)

        async def on_key(self, event: events.Key) -> None:
            selected = self._selected_pending_approval()
            if selected is None:
                return
            if (
                self._approval_actions is None
                or self.focused is not self._approval_actions
            ):
                return

            key_character = event.character
            if key_character is None:
                return

            normalized = key_character.lower()
            if normalized == "a":
                if self._approval_actions is not None:
                    self._approval_actions.highlighted = 0
                event.stop()
                event.prevent_default()
                await self._submit_selected_approval_decision(approve=True)
            elif normalized == "d":
                if self._approval_actions is not None:
                    self._approval_actions.highlighted = 1
                event.stop()
                event.prevent_default()
                await self._submit_selected_approval_decision(approve=False)

        def _stop_spinner_timer(self) -> None:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None

        def _is_active_event_state(self, state: str) -> bool:
            normalized = state.strip().lower()
            return normalized in ("queued", "in_progress", "running", "pending")

        def _event_spinner(self) -> str:
            count = len(self._spinner_frames)
            if count == 0:
                return ""
            return self._spinner_frames[self._spinner_frame % count]

        def _on_spinner_tick(self) -> None:
            if not self._has_active_events:
                return
            count = len(self._spinner_frames)
            if count == 0:
                return
            self._spinner_frame = (self._spinner_frame + 1) % count
            self._render_from_thread_document()

        def _notify_user(self, message: str, *, severity: str | None = None) -> None:
            notify = getattr(self, "notify", None)
            if callable(notify):
                try:
                    if severity is None:
                        notify(message)
                    else:
                        notify(message, severity=severity)
                    return
                except Exception:
                    pass
            logger.info(message)

        def _selected_pending_approval(self) -> tuple[str, str, str] | None:
            if len(self._pending_approval_items) == 0:
                return None

            index = max(
                0,
                min(
                    self._selected_pending_approval_index,
                    len(self._pending_approval_items) - 1,
                ),
            )
            self._selected_pending_approval_index = index
            return self._pending_approval_items[index]

        async def _submit_selected_approval_decision(self, *, approve: bool) -> None:
            selected = self._selected_pending_approval()
            if selected is None:
                self._notify_user("No pending approvals found.", severity="warning")
                return

            approval_id, _, _ = selected
            try:
                await self._chat_client.send_approval_decision(
                    approval_id=approval_id,
                    approve=approve,
                )
                if self._messages_scroll is not None:
                    self._messages_scroll.scroll_end(animate=False)
            except Exception as ex:
                self._notify_user(f"Unable to submit approval decision: {ex}")

        async def _submit_highlighted_approval_decision(self) -> None:
            highlighted = 0
            if self._approval_actions is not None and isinstance(
                self._approval_actions.highlighted, int
            ):
                highlighted = self._approval_actions.highlighted
            await self._submit_selected_approval_decision(approve=highlighted != 1)

        def _render_approval_menu(self) -> None:
            if (
                self._approval_panel is None
                or self._approval_header_view is None
                or self._approval_details_view is None
                or self._approval_actions is None
            ):
                return

            selected = self._selected_pending_approval()
            if selected is None:
                self._approval_panel.styles.display = "none"
                self._approval_header_view.update("")
                self._approval_details_view.update("")
                if (
                    self._chat_input is not None
                    and self.focused is self._approval_actions
                ):
                    self._chat_input.focus()
                return

            approval_id, headline, details = selected
            total = len(self._pending_approval_items)
            current = self._selected_pending_approval_index + 1
            label = headline.strip() if isinstance(headline, str) else ""
            if label == "":
                label = "Approval required"

            header = Text("approval requested", style="bold yellow")
            if total > 1:
                header.append(f" ({current}/{total})", style="bold yellow")
            self._approval_header_view.update(header)

            details_view = Text()
            details_view.append(label)
            if details.strip() != "" and details.strip().casefold() != label.casefold():
                details_view.append(f"\n{details.strip()}", style="dim")
            details_view.append(f"\nid: {approval_id}", style="dim")
            if total > 1:
                details_view.append("  [←/→ switch request]", style="dim")
            self._approval_details_view.update(details_view)

            self._approval_panel.styles.display = "block"
            if self._approval_actions.highlighted is None:
                self._approval_actions.highlighted = 0
            if self.focused is not self._approval_actions:
                self._approval_actions.focus()

        async def action_submit_chat_input(self) -> None:
            if (
                self._approval_actions is not None
                and self.focused is self._approval_actions
            ):
                await self._submit_highlighted_approval_decision()
                return

            if self._chat_input is None or self.focused is not self._chat_input:
                return

            user_input = self._chat_input.text.strip()
            self._chat_input.load_text("")
            self._resize_chat_input(self._chat_input)

            if not user_input:
                return

            if user_input in {"/exit", "/quit"}:
                await self.action_quit_app()
                return

            if user_input == "/clear":
                await self.action_clear_thread()
                return
            if user_input == "/cancel":
                await self.action_cancel_turn()
                return

            if user_input.startswith("/approve") or user_input.startswith("/reject"):
                approve = user_input.startswith("/approve")
                parts = user_input.split(maxsplit=1)
                approval_id = parts[1].strip() if len(parts) > 1 else ""

                if approval_id == "":
                    approval_id = self._latest_pending_approval_id() or ""

                if approval_id == "":
                    self._notify_user("No pending approvals found.", severity="warning")
                    return

                try:
                    await self._chat_client.send_approval_decision(
                        approval_id=approval_id,
                        approve=approve,
                    )
                    if self._messages_scroll is not None:
                        self._messages_scroll.scroll_end(animate=False)
                except Exception as ex:
                    self._notify_user(f"Unable to submit approval decision: {ex}")
                return

            await self._chat_client.send(text=user_input)
            if self._messages_scroll is not None:
                self._messages_scroll.scroll_end(animate=False)

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if self._chat_input is None or event.text_area is not self._chat_input:
                return
            self._resize_chat_input(event.text_area)

        def _resize_chat_input(self, chat_input: TextArea) -> None:
            target_height = max(1, min(6, chat_input.virtual_size.height))
            if target_height == self._chat_input_height:
                return
            self._chat_input_height = target_height
            chat_input.styles.height = target_height
            # Reflow message area after input height changes to keep feed visible.
            self._render_from_thread_document()

        def _bind_thread_document_events(self) -> None:
            doc = self._chat_client.doc
            if doc is None:
                return

            @doc.on("inserted")
            def _on_inserted(_):
                self._doc_changed.set()

            @doc.on("updated")
            def _on_updated(_, __):
                self._doc_changed.set()

            @doc.on("deleted")
            def _on_deleted(_):
                self._doc_changed.set()

        async def _watch_thread_document(self) -> None:
            try:
                while True:
                    await self._doc_changed.wait()
                    self._doc_changed.clear()
                    self._render_from_thread_document()
            except asyncio.CancelledError:
                return

        async def _stop_doc_watch_loop(self) -> None:
            if self._doc_watch_task is None:
                return
            if not self._doc_watch_task.done():
                self._doc_watch_task.cancel()
            await asyncio.gather(self._doc_watch_task, return_exceptions=True)
            self._doc_watch_task = None

        def _render_from_thread_document(self) -> None:
            if self._messages_view is None:
                return

            doc = self._chat_client.doc
            if doc is None:
                self._pending_approval_items = []
                self._selected_pending_approval_index = 0
                self._render_approval_menu()
                self._messages_view.update("")
                return

            message_nodes = doc.root.get_children_by_tag_name("messages")
            if len(message_nodes) == 0:
                self._pending_approval_items = []
                self._selected_pending_approval_index = 0
                self._render_approval_menu()
                self._messages_view.update("")
                return

            items = message_nodes[0].get_children()
            has_active_event_nodes = self._thread_has_active_event_nodes(items)
            thread_status_text = self._active_thread_status_text()
            self._has_active_events = (
                has_active_event_nodes or thread_status_text is not None
            )
            selected_before = self._selected_pending_approval()
            selected_id = selected_before[0] if selected_before is not None else None
            self._pending_approval_items = self._collect_pending_approvals(items)
            if len(self._pending_approval_items) == 0:
                self._selected_pending_approval_index = 0
            elif selected_id is not None:
                for index, (approval_id, _, _) in enumerate(
                    self._pending_approval_items
                ):
                    if approval_id == selected_id:
                        self._selected_pending_approval_index = index
                        break
                else:
                    self._selected_pending_approval_index = 0
            else:
                self._selected_pending_approval_index = 0
            self._render_approval_menu()
            rendered_items: list[RenderableType] = []
            last_index = len(items) - 1
            for index, item in enumerate(items):
                for renderable in self._render_thread_item(
                    item,
                    is_last_item=index == last_index,
                    has_active_event=self._has_active_events,
                ):
                    rendered_items.append(renderable)

            if thread_status_text is not None and not has_active_event_nodes:
                rendered_items.append(
                    self._render_thread_status_item(thread_status_text)
                )

            if len(rendered_items) == 0:
                self._messages_view.update("")
            else:
                self._messages_view.update(Group(*rendered_items))

            if self._messages_scroll is not None:
                self._messages_scroll.scroll_end(animate=False)

        def _thread_has_active_event_nodes(self, items) -> bool:
            for item in items:
                if getattr(item, "tag_name", None) != "event":
                    continue
                state = item.get_attribute("state") or "info"
                if self._is_active_event_state(state):
                    return True
            return False

        def _active_thread_status_text(self) -> str | None:
            return ask_module._thread_status_text(self._chat_client.thread_status_text)

        def _render_thread_status_item(self, status_text: str) -> RenderableType:
            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            table.add_row(Text("  "), Text(" "), Text("  "))
            table.add_row(
                Text("  "),
                Text(f"{self._event_spinner()} {status_text}", style="bold magenta"),
                Text("  "),
            )
            table.add_row(Text("  "), Text(" "), Text("  "))
            return table

        def _collect_pending_approvals(self, items) -> list[tuple[str, str, str]]:
            approvals: list[tuple[str, str, str]] = []
            seen: set[str] = set()

            for item in items:
                if getattr(item, "tag_name", None) != "event":
                    continue

                kind = item.get_attribute("kind") or ""
                if not isinstance(kind, str) or kind.strip().lower() != "approval":
                    continue

                state = item.get_attribute("state") or ""
                if not isinstance(state, str) or not self._is_active_event_state(state):
                    continue

                approval_id = (
                    item.get_attribute("item_id")
                    or item.get_attribute("approval_id")
                    or ""
                )
                if not isinstance(approval_id, str):
                    continue
                approval_id = approval_id.strip()
                if approval_id == "" or approval_id in seen:
                    continue

                headline = (
                    item.get_attribute("headline")
                    or item.get_attribute("summary")
                    or item.get_attribute("name")
                    or "Approval required"
                )
                if not isinstance(headline, str):
                    headline = "Approval required"
                headline = headline.strip()
                if headline == "":
                    headline = "Approval required"

                details = self._approval_details_text(item, headline=headline)
                approvals.append((approval_id, headline, details))
                seen.add(approval_id)

            return approvals

        def _approval_details_text(self, item, *, headline: str) -> str:
            for attr_name in ("details", "summary", "data", "name"):
                raw = item.get_attribute(attr_name) or ""
                if not isinstance(raw, str):
                    continue
                for line in raw.splitlines():
                    stripped = line.strip()
                    if stripped == "":
                        continue
                    if stripped.casefold() == headline.casefold():
                        continue
                    return stripped
            return headline

        def _latest_pending_approval_id(self) -> str | None:
            selected = self._selected_pending_approval()
            if selected is None:
                return None
            return selected[0]

        def _render_thread_item(
            self,
            item,
            *,
            is_last_item: bool,
            has_active_event: bool,
        ) -> list[RenderableType]:
            tag_name = getattr(item, "tag_name", None)
            if tag_name == "message":
                render_markdown = not (has_active_event and is_last_item)
                return [
                    self._render_message_item(item, render_markdown=render_markdown)
                ]
            if tag_name == "event":
                return [self._render_event_item(item)]
            if tag_name == "reasoning":
                summary = item.get_attribute("summary")
                if not isinstance(summary, str) or summary.strip() == "":
                    return []
                return [self._render_reasoning_item(item)]
            if tag_name == "exec":
                return [self._render_exec_item(item)]
            if tag_name == "ui":
                return [self._render_ui_item(item)]
            return []

        def _render_message_item(
            self, item, *, render_markdown: bool = True
        ) -> RenderableType:
            author = item.get_attribute("author_name") or "unknown"
            text = item.get_attribute("text") or ""
            relative_time = self._relative_time_label(item.get_attribute("created_at"))
            sender_prefix = (
                author if relative_time == "" else f"{author} ({relative_time})"
            )
            is_local = author == self._local_user_name
            header_style = "bold white" if is_local else "bold"
            body_style = "white" if is_local else ""
            row_style = "on #3f3f3f" if is_local else ""

            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            def add_message_row(
                content: RenderableType, *, left_padding: str = "  "
            ) -> None:
                left = Text(left_padding, style=row_style if row_style != "" else "")
                right = Text("  ", style=row_style if row_style != "" else "")
                if row_style != "":
                    table.add_row(left, content, right, style=row_style)
                else:
                    table.add_row(left, content, right)

            # Top padding row for every message block.
            add_message_row(Text(" "))

            add_message_row(Text(sender_prefix, style=header_style))

            markdown_text = text if text.strip() != "" else " "
            if render_markdown:
                add_message_row(Markdown(markdown_text), left_padding="  ")
            else:
                add_message_row(
                    Text(markdown_text, no_wrap=False, overflow="fold"),
                    left_padding="  ",
                )

            for child in item.get_children():
                if getattr(child, "tag_name", None) == "file":
                    path = child.get_attribute("path")
                    if path is not None and path != "":
                        attachment_line = f"[attachment] {path}"
                        if body_style != "":
                            add_message_row(Text(attachment_line, style=body_style))
                        else:
                            add_message_row(Text(attachment_line, style="dim"))

            # Bottom padding row for every message block.
            add_message_row(Text(" "))

            return table

        def _relative_time_label(self, created_at: str | None) -> str:
            if created_at is None or created_at == "":
                return ""

            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                return ""

            seconds = int((datetime.now(timezone.utc) - parsed).total_seconds())
            if seconds < 0:
                seconds = 0

            if seconds < 10:
                return "just now"
            if seconds < 60:
                return f"{seconds}s ago"

            minutes = seconds // 60
            if minutes < 60:
                return f"{minutes}m ago"

            hours = minutes // 60
            if hours < 24:
                return f"{hours}h ago"

            days = hours // 24
            if days < 7:
                return f"{days}d ago"

            weeks = days // 7
            if weeks < 5:
                return f"{weeks}w ago"

            months = days // 30
            if months < 12:
                return f"{months}mo ago"

            years = days // 365
            return f"{years}y ago"

        def _render_event_item(self, item) -> RenderableType:
            kind = item.get_attribute("kind") or "event"
            state = item.get_attribute("state") or "info"
            normalized_kind = kind.strip().lower() if isinstance(kind, str) else "event"
            active = self._is_active_event_state(state)
            if active:
                self._has_active_events = True
            approval_id = item.get_attribute("item_id") or ""
            if not isinstance(approval_id, str):
                approval_id = ""
            approval_id = approval_id.strip()
            headline = (
                item.get_attribute("headline")
                or item.get_attribute("summary")
                or item.get_attribute("name")
                or ""
            )
            if not isinstance(headline, str):
                headline = ""
            headline = headline.strip()

            summary = item.get_attribute("summary") or ""
            if not isinstance(summary, str):
                summary = ""
            summary = summary.strip()

            if headline == "":
                headline = summary

            if headline == "":
                headline = "event"

            diff_blocks = (
                self._diff_preview_blocks(item) if normalized_kind == "diff" else []
            )
            if len(diff_blocks) > 0:
                headline = self._diff_preview_headline(
                    blocks=diff_blocks,
                    state=state,
                    fallback=headline,
                )

            if active:
                headline_text = f"{self._event_spinner()} {headline}"
            else:
                headline_text = headline

            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            # Top padding row.
            table.add_row(Text("  "), Text(" "), Text("  "))
            table.add_row(
                Text("  "), Text(headline_text, style="bold magenta"), Text("  ")
            )

            if summary != "" and summary.casefold() != headline.casefold():
                table.add_row(Text("  "), Text(summary, style="dim"), Text("  "))

            if normalized_kind == "approval" and approval_id != "":
                table.add_row(
                    Text("  "),
                    Text(f"Approval ID: {approval_id}", style="bold cyan"),
                    Text("  "),
                )
                if active:
                    table.add_row(
                        Text("  "),
                        Text(
                            f"/approve {approval_id} or /reject {approval_id}",
                            style="bold yellow",
                        ),
                        Text("  "),
                    )

            detail_lines = self._event_detail_lines(item)
            if len(diff_blocks) > 0:
                table.add_row(Text("  "), Text(" "), Text("  "))
                for index, block in enumerate(diff_blocks):
                    if index > 0:
                        table.add_row(Text("  "), Text(" "), Text("  "))
                    table.add_row(
                        Text("  "),
                        Text(f"└ {block['header']}", style="dim"),
                        Text("  "),
                    )
                    for line in block["lines"]:
                        detail_text = Text("    ")
                        detail_text.append_text(self._render_diff_line(line))
                        table.add_row(Text("  "), detail_text, Text("  "))
                table.add_row(Text("  "), Text(" "), Text("  "))
            elif len(detail_lines) > 0:
                table.add_row(Text("  "), Text(" "), Text("  "))
                for line in detail_lines:
                    detail_text = Text("  ")
                    detail_text.append_text(
                        self._render_event_detail_line(kind=kind, line=line)
                    )
                    table.add_row(Text("  "), detail_text, Text("  "))
                table.add_row(Text("  "), Text(" "), Text("  "))
            # Bottom padding row.
            table.add_row(Text("  "), Text(" "), Text("  "))

            return table

        def _event_detail_lines(self, item) -> list[str]:
            details = item.get_attribute("details") or ""
            if not isinstance(details, str) or details.strip() == "":
                return []
            return details.splitlines()

        def _diff_preview_blocks(self, item) -> list[dict[str, object]]:
            candidates: list[str] = []
            for attr in ("preview", "data"):
                value = item.get_attribute(attr)
                if isinstance(value, str) and value.strip() != "":
                    candidates.append(value)
            for candidate in candidates:
                blocks = self._apply_patch_preview_blocks(candidate)
                if len(blocks) > 0:
                    return blocks
            return []

        def _apply_patch_preview_blocks(self, text: str) -> list[dict[str, object]]:
            normalized = text.replace("\r\n", "\n").rstrip()
            if (
                "*** Begin Patch" not in normalized
                and "*** Update File:" not in normalized
                and "*** Add File:" not in normalized
                and "*** Delete File:" not in normalized
            ):
                return []

            blocks: list[dict[str, object]] = []
            current_path = ""
            current_lines: list[str] = []
            lines_added = 0
            lines_removed = 0

            def flush() -> None:
                nonlocal current_lines, lines_added, lines_removed
                if current_path == "" or len(current_lines) == 0:
                    current_lines = []
                    lines_added = 0
                    lines_removed = 0
                    return
                blocks.append(
                    {
                        "path": current_path,
                        "header": self._diff_preview_header(
                            path=current_path,
                            lines_added=lines_added,
                            lines_removed=lines_removed,
                        ),
                        "lines": current_lines,
                        "lines_added": lines_added,
                        "lines_removed": lines_removed,
                    }
                )
                current_lines = []
                lines_added = 0
                lines_removed = 0

            for line in normalized.splitlines():
                match = re.match(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", line)
                if match is not None:
                    flush()
                    current_path = match.group(1).strip()
                    continue
                if current_path == "" or line.startswith("*** "):
                    continue
                current_lines.append(line)
                if line.startswith("+") and not line.startswith("+++"):
                    lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    lines_removed += 1
            flush()
            return blocks

        def _diff_preview_header(
            self, *, path: str, lines_added: int, lines_removed: int
        ) -> str:
            if lines_added == 0 and lines_removed == 0:
                return path
            return f"{path} (+{lines_added} -{lines_removed})"

        def _diff_preview_headline(
            self, *, blocks: list[dict[str, object]], state: str, fallback: str
        ) -> str:
            if len(blocks) == 0:
                return fallback
            lines_added = sum(
                value if isinstance(value := block.get("lines_added"), int) else 0
                for block in blocks
            )
            lines_removed = sum(
                value if isinstance(value := block.get("lines_removed"), int) else 0
                for block in blocks
            )
            path = blocks[0].get("path")
            target = (
                f"{len(blocks)} files"
                if len(blocks) != 1
                else path
                if isinstance(path, str) and path.strip() != ""
                else "patch"
            )
            verb = "Editing"
            normalized_state = state.strip().lower()
            if normalized_state == "completed":
                verb = "Edited"
            elif normalized_state == "failed":
                verb = "Attempted to patch"
            elif normalized_state == "cancelled":
                verb = "Patch cancelled:"
            return f"{verb} {target} (+{lines_added} -{lines_removed})"

        def _render_event_detail_line(self, *, kind: str, line: str) -> Text:
            if kind == "diff":
                return self._render_diff_line(line)
            return Text(line, style="dim")

        def _render_diff_line(self, line: str) -> Text:
            text = Text(line)
            if line.startswith("@@"):
                text.stylize("bold cyan")
            elif line.startswith("+++ ") or line.startswith("--- "):
                text.stylize("bold yellow")
            elif line.startswith("+"):
                text.stylize("green")
            elif line.startswith("-"):
                text.stylize("red")
            elif line.startswith("diff ") or line.startswith("index "):
                text.stylize("bold blue")
            elif line.strip().startswith("```"):
                text.stylize("dim")
            return text

        def _render_reasoning_item(self, item) -> RenderableType:
            summary = item.get_attribute("summary") or ""
            if not isinstance(summary, str):
                summary = ""

            markdown_text = summary if summary.strip() != "" else " "
            return Group(
                Text(" "),
                Rule(style="bright_black"),
                Text(" "),
                Padding(Markdown(markdown_text), (0, 2)),
                Text(" "),
            )

        def _render_exec_item(self, item) -> RenderableType:
            command = item.get_attribute("command") or ""
            outcome = item.get_attribute("outcome") or ""
            stdout = item.get_attribute("stdout") or ""
            stderr = item.get_attribute("stderr") or ""
            parts = []
            if command != "":
                parts.append(f"$ {command}")
            if outcome != "":
                parts.append(f"outcome: {outcome}")
            if stdout != "":
                parts.append(stdout)
            if stderr != "":
                parts.append(stderr)
            text = "\n".join(parts).strip() or "exec"
            return Align.center(Panel(Text(text), border_style="yellow", title="exec"))

        def _render_ui_item(self, item) -> RenderableType:
            widget = item.get_attribute("widget") or "ui"
            renderer = item.get_attribute("renderer") or "unknown"
            data = item.get_attribute("data") or ""
            if data != "":
                text = f"{widget} via {renderer}\n{data}"
            else:
                text = f"{widget} via {renderer}"
            return Align.center(Panel(Text(text), border_style="blue", title="ui"))

    account_client = None
    user_client: RoomClient | None = None
    chat_client: _ChatWithClient | None = None

    def _queue_status(
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
        *,
        status: str,
        message: str,
    ) -> None:
        if status_queue is None:
            return
        status_queue.put_nowait((status, message))

    async def _close_chat_client(client: _ChatWithClient | None) -> None:
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _close_user_client(client: RoomClient | None) -> None:
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _open_user_client(
        *,
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
    ) -> RoomClient:
        nonlocal account_client

        if account_client is None:
            _queue_status(
                status_queue,
                status="initializing",
                message="initializing account client",
            )
            account_client = await get_client()

        _queue_status(status_queue, status="starting_room", message="starting room")
        connection = await account_client.connect_room(project_id=project_id, room=room)
        _queue_status(status_queue, status="connecting", message="connecting to room")

        connecting_client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=connection.jwt,
            ).create_factory(),
        )

        def _on_room_status(**kwargs) -> None:
            status = kwargs.get("status", "")
            message = kwargs.get("message", "")
            if not isinstance(status, str):
                status = str(status)
            if not isinstance(message, str):
                message = str(message)
            _queue_status(status_queue, status=status, message=message)

        connecting_client.on("room.status", _on_room_status)

        try:
            await connecting_client.__aenter__()
        except asyncio.CancelledError:
            try:
                await connecting_client.__aexit__(None, None, None)
            except Exception:
                pass
            raise
        except Exception:
            try:
                await connecting_client.__aexit__(None, None, None)
            except Exception:
                pass
            raise

        _queue_status(status_queue, status="connected", message="connected to room")
        return connecting_client

    async def _prepare_chat_session(
        *,
        status_queue: "asyncio.Queue[tuple[str, str]] | None",
    ) -> tuple[RoomClient, _ChatWithClient, str]:
        prepared_user_client = await _open_user_client(status_queue=status_queue)
        prepared_chat_client: _ChatWithClient | None = None
        try:
            _queue_status(
                status_queue,
                status="syncing",
                message="initializing room state",
            )
            await prepared_user_client.messaging.enable()

            local_user_name = prepared_user_client.local_participant.get_attribute(
                "name"
            )
            resolved_thread_path = thread_path
            if resolved_thread_path is None:
                resolved_thread_path = (
                    f".threads/{participant_name}/{local_user_name}.thread"
                )

            _queue_status(
                status_queue,
                status="opening_thread",
                message="opening chat thread",
            )
            prepared_chat_client = _ChatWithClient(
                room=prepared_user_client,
                participant_name=participant_name,
                thread_path=resolved_thread_path,
            )
            await prepared_chat_client.__aenter__()

            _queue_status(
                status_queue,
                status="starting_ui",
                message="starting chat ui",
            )
            return prepared_user_client, prepared_chat_client, local_user_name
        except asyncio.CancelledError:
            await _close_chat_client(prepared_chat_client)
            await _close_user_client(prepared_user_client)
            raise
        except Exception:
            await _close_chat_client(prepared_chat_client)
            await _close_user_client(prepared_user_client)
            raise

    try:
        if message is None:
            _suppress_textual_debug_features()
            status_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
            connect_task = asyncio.create_task(
                _prepare_chat_session(status_queue=status_queue)
            )

            connect_app = RoomConnectTextualApp(
                room_name=room,
                status_queue=status_queue,
                connect_task=connect_task,
            )
            connect_token = active_app.set(connect_app)
            try:
                await connect_app.run_async()
            except KeyboardInterrupt:
                if not connect_task.done():
                    connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                return
            finally:
                active_app.reset(connect_token)

            if not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                return

            try:
                user_client, chat_client, local_user_name = await connect_task
            except asyncio.CancelledError:
                return
            except Exception as ex:
                print(f"[bold red]Unable to connect to room: {ex}[/bold red]")
                return

            app = ChatWithTextualApp(
                chat_client=chat_client,
                participant_name=participant_name,
                local_user_name=local_user_name,
            )
            token = active_app.set(app)
            try:
                await app.run_async()
            except KeyboardInterrupt:
                return
            finally:
                active_app.reset(token)
        else:
            user_client = await _open_user_client(status_queue=None)
            await user_client.messaging.enable()

            local_user_name = user_client.local_participant.get_attribute("name")
            resolved_thread_path = thread_path
            if resolved_thread_path is None:
                resolved_thread_path = (
                    f".threads/{participant_name}/{local_user_name}.thread"
                )

            chat_client = _ChatWithClient(
                room=user_client,
                participant_name=participant_name,
                thread_path=resolved_thread_path,
            )
            await chat_client.__aenter__()
            await chat_client.send(text=message)
            response = await chat_client.receive()
            print(response)
            return

    except asyncio.CancelledError:
        pass

    finally:
        await _close_chat_client(chat_client)
        await _close_user_client(user_client)
        if account_client is not None:
            await account_client.close()


@app.async_command("run", help="Join a room, run the chatbot, and wait for messages.")
async def run(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    role: str = "agent",
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to call")
    ] = None,
    rule: Annotated[List[str], typer.Option("--rule", "-r", help="a system rule")] = [],
    room_rules: Annotated[
        List[str],
        typer.Option(
            "--room-rules",
            "-rr",
            help="a path to a rules file within the room that can be used to customize the agent's behavior",
        ),
    ] = [],
    rules_file: Optional[list[str]] = None,
    instructions: InstructionsOption = [],
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
            "--toolkit",
            "-t",
            help="the name or url of a required toolkit",
            hidden=True,
        ),
    ] = [],
    schema: Annotated[
        List[str],
        typer.Option("--schema", "-s", help="the name or url of a required schema"),
    ] = [],
    model: Annotated[
        str, typer.Option(..., help="Name of the LLM model to use for the chatbot")
    ] = "gpt-5.6-sol",
    image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    computer_use: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable computer use"),
    ] = False,
    shell: Annotated[
        Optional[bool], typer.Option(..., help="Enable function shell tool calling")
    ] = False,
    apply_patch: Annotated[
        Optional[bool], typer.Option(..., help="Enable apply patch tool")
    ] = False,
    web_search: Annotated[
        Optional[bool], typer.Option(..., help="Enable web search tool calling")
    ] = False,
    web_fetch: Annotated[
        Optional[bool], typer.Option(..., help="Enable web fetch tool calling")
    ] = False,
    script_tool: Annotated[
        Optional[bool], typer.Option(..., help="Enable script tool calling")
    ] = False,
    discover_script_tools: Annotated[
        Optional[bool],
        typer.Option(..., help="Automatically add script tools from the room"),
    ] = False,
    mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    storage_tool_local_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-local-path",
            help="Mount local path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    storage_tool_room_path: Annotated[
        List[str],
        typer.Option(
            "--storage-tool-room-path",
            help="Mount room path as <source>:<mount>[:ro|rw]",
        ),
    ] = [],
    shell_room_mount: ShellRoomMountOption = [],
    shell_tool_room_path: ShellRoomMountLegacyOption = [],
    shell_empty_dir_mount: ShellEmptyDirMountOption = [],
    shell_tool_empty_dir: ShellEmptyDirMountLegacyOption = [],
    shell_tool_config_mount: ShellConfigMountOption = [],
    require_image_generation: Annotated[
        Optional[str], typer.Option(..., help="Name of an image gen model")
    ] = None,
    require_computer_use: Annotated[
        Optional[bool],
        typer.Option(
            ...,
            help="Enable computer use",
            hidden=True,
        ),
    ] = False,
    starting_url: StartingUrlOption = None,
    allow_goto_url: AllowGotoUrlOption = False,
    require_shell: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable function shell tool calling"),
    ] = False,
    require_advanced_shell: RequireAdvancedShellOption = False,
    require_apply_patch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable apply patch tool calling"),
    ] = False,
    require_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web search tool calling"),
    ] = False,
    require_web_fetch: Annotated[
        Optional[bool],
        typer.Option(..., help="Enable web fetch tool calling"),
    ] = False,
    require_mcp: Annotated[
        Optional[bool], typer.Option(..., help="Enable mcp tool calling")
    ] = False,
    require_storage: Annotated[
        Optional[bool], typer.Option(..., help="Enable storage toolkit")
    ] = False,
    dataset_namespace: Annotated[
        Optional[str],
        typer.Option("--dataset-namespace", help="Use a specific dataset namespace"),
    ] = None,
    require_table_read: Annotated[
        list[str],
        typer.Option(
            "--table-read", help="Enable table read tools for a specific table"
        ),
    ] = [],
    require_table_write: Annotated[
        list[str],
        typer.Option(
            "--table-write", help="Enable table write tools for a specific table"
        ),
    ] = [],
    require_read_only_storage: Annotated[
        Optional[bool],
        typer.Option("--read-only-storage", help="Enable read only storage toolkit"),
    ] = False,
    require_time: Annotated[
        bool,
        typer.Option(
            "--time",
            help="Enable time/datetime tools",
        ),
    ] = True,
    require_uuid: Annotated[
        bool,
        typer.Option(
            "--uuid",
            help="Enable UUID generation tools",
        ),
    ] = False,
    use_memory: Annotated[
        Optional[str],
        typer.Option(
            "--use-memory",
            help="Use memories toolkit for <name> or <namespace>/<name>",
        ),
    ] = None,
    memory_model: Annotated[
        Optional[str],
        typer.Option(
            "--memory-model",
            help="Model name for memory LLM ingestion",
        ),
    ] = None,
    require_document_authoring: Annotated[
        Optional[bool],
        typer.Option("--document-authoring", help="Enable MeshDocument authoring"),
    ] = False,
    require_discovery: Annotated[
        Optional[bool],
        typer.Option("--discovery", help="Enable discovery of agents and tools"),
    ] = False,
    working_dir: WorkingDirOption = None,
    working_directory: WorkingDirectoryAliasOption = None,
    key: Annotated[
        str,
        typer.Option("--key", help="an api key to sign the token with"),
    ] = None,
    llm_participant: Annotated[
        Optional[str],
        typer.Option(..., help="Delegate LLM interactions to a remote participant"),
    ] = None,
    decision_model: DecisionModelOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "none",
    thread_dir: ThreadDirOption = None,
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = None,
    channel: ChannelOption = [],
    skill_dir: Annotated[
        list[str],
        typer.Option(..., help="an agent skills directory"),
    ] = [],
    shell_image: Annotated[
        Optional[str],
        typer.Option(..., help="an image tag to use to run shell commands in"),
    ] = None,
    delegate_shell_token: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    shell_copy_env: ShellCopyEnvOption = [],
    shell_set_env: ShellSetEnvOption = [],
    log_llm_requests: Annotated[
        Optional[bool],
        typer.Option(..., help="log all requests to the llm"),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Enable verbose logging and disable default log suppression",
        ),
    ] = False,
    thread_path: Annotated[
        Optional[str],
        typer.Option("--thread-id", help="Thread id to open"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="the input message to use"),
    ] = None,
    use_web_search: Annotated[
        Optional[bool],
        typer.Option(..., help="request the web search tool"),
    ] = None,
    use_image_gen: Annotated[
        Optional[bool],
        typer.Option(..., help="request the image gen tool"),
    ] = None,
    use_storage: Annotated[
        Optional[bool],
        typer.Option(..., help="request the storage tool"),
    ] = None,
):
    runtime = _current_command_runtime()
    resolved_channels = _resolved_channels(
        runtime=runtime,
        channel=channel,
        require_chat=runtime == "process",
    )
    working_dir = _resolve_working_dir_option(
        working_dir=working_dir,
        working_directory=working_directory,
    )
    if not verbose and not log_llm_requests:
        root = logging.getLogger()
        root.setLevel(logging.ERROR)

    resolved_dataset_namespace = _resolved_dataset_namespace(
        runtime=runtime,
        dataset_namespace=dataset_namespace,
    )
    normalized_tool_options = normalize_required_tool_options(
        toolkit=toolkit,
        require_toolkit=require_toolkit,
        schema=schema,
        require_schema=require_schema,
        image_generation=image_generation,
        require_image_generation=require_image_generation,
        computer_use=computer_use,
        require_computer_use=require_computer_use,
        shell=shell,
        require_shell=require_shell,
        advanced_shell=require_advanced_shell,
        apply_patch=apply_patch,
        require_apply_patch=require_apply_patch,
        web_search=web_search,
        require_web_search=require_web_search,
        web_fetch=web_fetch,
        require_web_fetch=require_web_fetch,
        mcp=mcp,
        require_mcp=require_mcp,
        storage=storage,
        require_storage=require_storage,
    )

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            if agent_name is None:
                print(
                    "[bold red]--agent-name must be specified when the MESHAGENT_TOKEN environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            jwt = await mint_participant_token_for_cli(
                project_id=project_id,
                name=agent_name,
                room_name=room,
                role=role,
                api_scope=ApiScope.agent_default(tunnels=require_computer_use),
                key=key,
            )

        default_room_storage_mount = bool(
            normalized_tool_options["require_storage"] or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            empty_dir_paths=merge_option_lists(
                shell_empty_dir_mount,
                shell_tool_empty_dir,
            ),
            config_paths=shell_tool_config_mount,
        )

        client = RoomClient(
            protocol_factory=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room),
                token=jwt,
            ).create_factory()
        )
        CustomChatbot = _build_runtime_agent(
            client=client,
            api_key=jwt,
            runtime=runtime,
            normalized_tool_options=normalized_tool_options,
            model=model,
            rule=rule,
            rules_file=rules_file,
            instructions=instructions,
            discover_script_tools=discover_script_tools,
            storage_tool_local_paths=storage_tool_local_path,
            storage_tool_room_paths=storage_tool_room_path,
            default_room_storage_mount=default_room_storage_mount,
            shell_tool_mounts=shell_tool_mounts,
            require_read_only_storage=require_read_only_storage,
            require_time=require_time,
            require_uuid=require_uuid,
            use_memory=use_memory,
            memory_model=memory_model,
            require_table_read=require_table_read,
            require_table_write=require_table_write,
            require_document_authoring=require_document_authoring,
            require_discovery=require_discovery,
            require_advanced_shell=require_advanced_shell,
            llm_participant=llm_participant,
            decision_model=decision_model,
            always_reply=always_reply,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
        )

        bot = CustomChatbot()

        async def run_interactive_session(client: RoomClient) -> None:
            if runtime == "process":
                interaction_task = asyncio.create_task(
                    _run_process_run_tui(
                        bot=bot,
                        model=model,
                        thread_path=thread_path,
                        message=message,
                        working_dir=working_dir,
                    )
                )
            else:
                interaction_task = asyncio.create_task(
                    chat_with(
                        participant_name=client.local_participant.get_attribute("name"),
                        room=room,
                        project_id=project_id,
                        thread_path=thread_path,
                        message=message,
                    )
                )

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client.protocol.wait_for_close()),
                    interaction_task,
                ],
                return_when="FIRST_COMPLETED",
            )

            await _cancel_background_tasks(pending)
            for task in done:
                task.result()

        try:
            await _run_agent_room_session(
                client=client,
                bot=bot,
                runner=run_interactive_session,
            )
        except KeyboardInterrupt:
            return

    except asyncio.CancelledError:
        return

    finally:
        await account_client.close()


@app.async_command(
    "use", help="Send a one-shot or interactive message to a running chatbot."
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
        typer.Option("--thread-id", help="Thread id to open"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="the input message to use"),
    ] = None,
):
    runtime = _current_command_runtime()
    root = logging.getLogger()
    root.setLevel(logging.ERROR)

    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        room = resolve_room(room)

        if runtime == "process":
            if agent_name is None or agent_name.strip() == "":
                print(
                    "[bold red]--agent-name must be specified for process use[/bold red]"
                )
                raise typer.Exit(1)

            await _run_process_use_tui(
                account_client=account_client,
                project_id=project_id,
                room=room,
                agent_name=agent_name,
                thread_path=thread_path,
                message=message,
            )
            return

        await chat_with(
            participant_name=agent_name,
            room=room,
            project_id=project_id,
            thread_path=thread_path,
            message=message,
        )

    except asyncio.CancelledError:
        return

    finally:
        await account_client.close()


strip_command_options(app, option_names=_HIDDEN_REQUIRE_OPTION_NAMES)
