import typer
import click
import contextlib
import inspect
import jwt
from aiohttp import web
from rich import print
from typing import (
    Annotated,
    Any,
    Optional,
    List,
    Literal,
    Awaitable,
    Callable,
    Iterable,
)
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
    ShellProjectMountLegacyOption,
    ShellProjectMountOption,
    ShellRoomMountLegacyOption,
    ShellRoomMountOption,
    StartingUrlOption,
)
from meshagent.api import (
    Element,
    MeshDocument,
    Participant,
    RoomClient,
    WebSocketClientProtocol,
    ParticipantToken,
    ApiScope,
    RoomException,
    RemoteParticipant,
)
from meshagent.api.helpers import meshagent_base_url, websocket_room_url
from meshagent.cli import async_typer
from meshagent.cli.ask import AskCommandOption
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
    normalize_required_tool_options,
    parse_shell_tool_mounts,
    parse_memory_selector,
    parse_storage_tool_mounts,
    resolve_dataset_namespace,
    resolve_shell_image,
    resolve_key,
    resolve_project_id,
    resolve_room,
    strip_command_options,
    supports_openai_shell_tool,
)
from meshagent.cli.preamble_rules import DEFAULT_PREAMBLE_RULE

from meshagent.openai import (
    DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    OpenAIRealtimeAdapter,
    OpenAIResponsesAdapter,
    OpenAIResponsesMCPToolkit,
)
from meshagent.openai.tools.realtime_adapter import (
    DEFAULT_OPENAI_REALTIME_INPUT_FORMAT,
    DEFAULT_OPENAI_REALTIME_OUTPUT_FORMAT,
    DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    DEFAULT_OPENAI_REALTIME_VOICE,
    OPENAI_REALTIME_VOICES,
)
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
from meshagent.agents.adapter import (
    LLMAdapter,
    LLMAudioFormat,
    LLMProvider,
    MessageStreamLLMAdapter,
)
from meshagent.agents.messages import (
    AGENT_EVENT_FILE_CONTENT_DELTA,
    AGENT_EVENT_MODEL_CHANGED,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_THREAD_CREATED,
    AGENT_EVENT_THREAD_DELETED,
    AGENT_EVENT_THREAD_UPDATED,
    AGENT_EVENT_THREAD_STARTED,
    AGENT_MESSAGE_MODEL_CHANGE,
    AGENT_MESSAGE_MODELS_REQUEST,
    AGENT_MESSAGE_MODELS_RESPONSE,
    AGENT_MESSAGE_THREAD_OPEN,
    AGENT_MESSAGE_TURN_START,
    AgentFileContent,
    AgentFileContentDelta,
    AgentError,
    AgentAudioFormat,
    AgentMessage,
    AgentThreadStatus,
    AgentThreadListEntry,
    AgentModelInfo,
    AgentModelChanged,
    AgentProviderInfo,
    AgentRealtimeConnectionInfo,
    AgentTextContent,
    AgentTextContentDelta,
    ChangeModel,
    DeleteThread,
    ListThreads,
    ModelsRequest,
    ModelsResponse,
    OpenThread,
    RenameThread,
    StartThread,
    ThreadCreated,
    ThreadDeleted,
    ThreadUpdated,
    ThreadStarted,
    TurnStart,
)
from meshagent.agents.chat_client import (
    ChatThreadSession,
    LocalChatClient,
    MessagingChatClient,
    WebSocketChatClient,
)
from meshagent.agents.process import ContentScheme, Message
from meshagent.agents.images_dataset import ImageDatasetClient, ImagesDataset
from meshagent.agents.thread_storage import (
    ThreadListEntry,
    ThreadListEvent,
    ThreadListPage,
)

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

OutputModality = Literal["text", "audio"]
WebSocketAuthMode = Literal["iap", "jwt", "none"]

logger = logging.getLogger("process")


def _thread_status_text(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    normalized = status.strip()
    if normalized == "":
        return None
    return normalized


def _format_grouped_status_digits(value: int) -> str:
    text = str(value)
    parts: list[str] = []
    for index, char in enumerate(text):
        if index > 0 and (len(text) - index) % 3 == 0:
            parts.append(",")
        parts.append(char)
    return "".join(parts)


def _format_thread_status_text(
    text: str,
    *,
    total_bytes: int | None = None,
    lines_added: int | None = None,
    lines_removed: int | None = None,
) -> str:
    if lines_added is not None or lines_removed is not None:
        parts = [text]
        if lines_added is not None:
            parts.append(f"+{_format_grouped_status_digits(lines_added)}")
        if lines_removed is not None:
            parts.append(f"-{_format_grouped_status_digits(lines_removed)}")
        return " ".join(parts)
    if total_bytes is not None and total_bytes > 100:
        return f"{text} {_format_grouped_status_digits(total_bytes)} bytes"
    return text


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


def _agent_input_text_from_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    attachment_count = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip() != "":
                text_parts.append(text.strip())
        elif item_type in ("file", "image"):
            attachment_count += 1

    if attachment_count > 0:
        noun = "attachment" if attachment_count == 1 else "attachments"
        text_parts.append(f"{attachment_count} {noun}")

    return " ".join(text_parts).strip()


def _pending_agent_message_label(payload: dict[str, Any]) -> str | None:
    text = _agent_input_text_from_payload(payload)
    sender_name = payload.get("sender_name")
    prefix = ""
    if isinstance(sender_name, str) and sender_name.strip() != "":
        prefix = f"{sender_name.strip()}: "
    label = f"{prefix}{text}".strip()
    if label == "":
        return None
    return label


def _start_thread_list_name(start_thread: StartThread) -> str:
    if isinstance(start_thread.name, str) and start_thread.name.strip() != "":
        return start_thread.name.strip()

    text_parts: list[str] = []
    attachment_count = 0
    for item in start_thread.content or []:
        if isinstance(item, AgentTextContent) and item.text.strip() != "":
            text_parts.append(item.text.strip())
        elif isinstance(item, AgentFileContent):
            attachment_count += 1
    text = " ".join(text_parts).strip()
    if text != "":
        words = re.findall(r"[A-Za-z0-9']+", text)
        if len(words) > 0:
            return " ".join(words[:6]).title()
    if attachment_count > 0:
        return "Attachment Thread"
    return "New Chat"


def _process_run_thread_id(
    *,
    thread_path: str | None,
    thread_storage: "ThreadStorageBackend",
    agent_name: str | None,
    thread_dir: str | None,
    threading_mode: "ThreadingMode" = "none",
) -> str:
    if isinstance(thread_path, str) and thread_path.strip() != "":
        normalized = thread_path.strip()
        if thread_storage == "dataset" and not normalized.startswith("dataset://"):
            return _dataset_thread_url_for_path(path=normalized)
        if thread_storage == "none" and not normalized.startswith("tmp://"):
            return _thread_url_for_path(scheme="tmp", path=normalized)
        return normalized

    normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
    if normalized_thread_dir is not None:
        if threading_mode == "default-new":
            return _new_process_thread_path_for_dir(
                thread_dir=normalized_thread_dir,
                thread_storage=thread_storage,
            )
        return _process_thread_path_for_dir(
            thread_dir=normalized_thread_dir,
            thread_storage=thread_storage,
        )

    default_thread_path = _default_process_thread_path_for_agent(
        agent_name=agent_name,
        thread_storage=thread_storage,
    )
    if default_thread_path is not None:
        return default_thread_path

    generated_path = f"process-run/{uuid.uuid4()}"
    if thread_storage == "dataset":
        return _dataset_thread_url_for_path(path=generated_path)
    if thread_storage == "none":
        return _thread_url_for_path(scheme="tmp", path=generated_path)
    return f"/{generated_path}"


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


def _mesh_document_attribute(element: Element, name: str) -> str:
    value = element.get_attribute(name)
    return value.strip() if isinstance(value, str) else ""


def _mesh_document_agent_messages(
    *,
    document: MeshDocument,
    thread_path: str,
) -> list[AgentMessage]:
    messages_elements = [
        child
        for child in document.root.get_children()
        if isinstance(child, Element) and child.tag_name == "messages"
    ]
    if len(messages_elements) == 0:
        return []

    messages: list[AgentMessage] = []
    for index, message in enumerate(messages_elements[0].get_children()):
        if not isinstance(message, Element) or message.tag_name != "message":
            continue

        message_id = _mesh_document_attribute(message, "id")
        if message_id == "":
            message_id = _mesh_document_attribute(message, "turn_id")
        if message_id == "":
            message_id = f"{thread_path}:message:{index}"

        role = _mesh_document_attribute(message, "role")
        text = _mesh_document_attribute(message, "text")
        file_paths: list[str] = []
        for child in message.get_children():
            if not isinstance(child, Element) or child.tag_name != "file":
                continue
            path = _mesh_document_attribute(child, "path")
            if path != "":
                file_paths.append(path)
        if text == "" and len(file_paths) == 0:
            continue

        if role in {"agent", "assistant"}:
            turn_id = _mesh_document_attribute(message, "turn_id") or message_id
            author_name = _mesh_document_attribute(message, "author_name")
            if text != "":
                messages.append(
                    AgentTextContentDelta(
                        type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                        thread_id=thread_path,
                        message_id=message_id,
                        turn_id=turn_id,
                        item_id=message_id,
                        text=text,
                    ).model_copy(update={"sender_name": author_name})
                )
            for file_index, path in enumerate(file_paths):
                item_id = f"{message_id}:file:{file_index}"
                messages.append(
                    AgentFileContentDelta(
                        type=AGENT_EVENT_FILE_CONTENT_DELTA,
                        thread_id=thread_path,
                        message_id=item_id,
                        turn_id=turn_id,
                        item_id=item_id,
                        url=path,
                    ).model_copy(update={"sender_name": author_name})
                )
            continue

        author_name = _mesh_document_attribute(message, "author_name")
        content: list[AgentTextContent | AgentFileContent] = []
        if text != "":
            content.append(AgentTextContent(type="text", text=text))
        for path in file_paths:
            content.append(AgentFileContent(type="file", url=path))
        messages.append(
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                thread_id=thread_path,
                message_id=message_id,
                content=content,
            ).model_copy(update={"sender_name": author_name})
        )

    return messages


async def _thread_agent_messages_from_storage(
    thread_storage: object,
) -> list[AgentMessage]:
    from meshagent.agents.dataset_thread_storage import DatasetThreadStorage
    from meshagent.agents.process_thread_adapter import MeshDocumentThreadStorage

    if isinstance(thread_storage, (DatasetThreadStorage, MeshDocumentThreadStorage)):
        await thread_storage.wait_until_ready()
        return thread_storage.agent_messages()
    return []


def _thread_storage_class_for_backend(thread_storage: "ThreadStorageBackend"):
    if thread_storage == "dataset":
        from meshagent.agents.dataset_thread_storage import DatasetThreadStorage

        return DatasetThreadStorage
    if thread_storage == "meshdocument":
        from meshagent.agents.process_thread_adapter import MeshDocumentThreadStorage

        return MeshDocumentThreadStorage
    return None


def _thread_storage_class_for_path(*, thread_path: str):
    if thread_path.strip().startswith("dataset://"):
        from meshagent.agents.dataset_thread_storage import DatasetThreadStorage

        return DatasetThreadStorage

    from meshagent.agents.process_thread_adapter import MeshDocumentThreadStorage

    return MeshDocumentThreadStorage


async def _load_thread_agent_messages(
    *,
    room: RoomClient,
    thread_path: str,
) -> list[AgentMessage]:
    normalized_path = thread_path.strip()
    if normalized_path == "" or normalized_path.startswith("tmp://"):
        return []

    storage_class = _thread_storage_class_for_path(thread_path=normalized_path)
    storage = storage_class(room=room, path=normalized_path)
    await storage.start()
    try:
        return storage.agent_messages()
    finally:
        await storage.stop()


class _ProcessRunSession:
    def __init__(
        self,
        *,
        bot: Any,
        model: str | None,
        thread_path: str | None,
        thread_storage: "ThreadStorageBackend",
        agent_name: str | None,
        thread_dir: str | None,
        threading_mode: "ThreadingMode",
        current_working_directory: str | None,
        initial_model: AgentModelChanged | None = None,
        image_dataset_client: ImageDatasetClient | None = None,
    ) -> None:
        self._bot = bot
        self._model = model
        self._thread_storage = thread_storage
        self._thread_dir = thread_dir
        self._threading_mode = threading_mode
        self._current_working_directory = current_working_directory
        self._image_dataset_client = image_dataset_client
        self._agent_name = _normalized_annotation_string(agent_name)
        self._current_model: AgentModelChanged | None = initial_model
        self._models_response: ModelsResponse | None = None
        self._output_modalities: tuple[OutputModality, ...] = (
            tuple(initial_model.output_modalities)
            if initial_model is not None
            else ("text",)
        )
        self._timeout = 30
        self._thread_generation = 0
        resolved_thread_path = _process_run_thread_id(
            thread_path=thread_path,
            thread_storage=thread_storage,
            agent_name=agent_name,
            thread_dir=thread_dir,
            threading_mode=threading_mode,
        )
        open_on_start = not (
            threading_mode == "default-new"
            and (thread_path is None or thread_path.strip() == "")
        )
        self._configure_thread(
            thread_path=resolved_thread_path if open_on_start else None,
            open_on_start=open_on_start,
        )

    def _configure_thread(
        self, *, thread_path: str | None, open_on_start: bool
    ) -> None:
        from meshagent.cli import ask as ask_module

        self._thread_id = thread_path
        self._open_on_start = open_on_start
        events = self._bot._supervisor.subscribe_local_events()
        channel_client = LocalChatClient(
            thread_path=self._thread_id,
            send_message=self._bot._supervisor.send,
            events=events,
            on_close=lambda: self._bot._supervisor.unsubscribe_local_events(events),
        )
        self._channel_client = channel_client
        self._chat_session = channel_client.thread_session
        self._session = ask_module._AgentMessageSession(
            client=self._chat_session,
            model=self._model,
            current_working_directory=self._current_working_directory,
            model_provider=lambda: self._current_model,
            image_dataset_client=self._image_dataset_client,
            start_thread_callback=self._start_thread,
        )
        self._sync_turn_output_modalities()
        self._started = False

    async def _start_thread(self, start_thread: StartThread) -> ChatThreadSession:
        new_session = await self._channel_client.start_thread(start_thread)
        self._chat_session = new_session
        self._thread_id = new_session.thread_path
        self._sync_turn_output_modalities()
        if self._models_response is not None:
            self._apply_models_response(self._models_response)
        return new_session

    @property
    def current_working_directory(self) -> str:
        return self._session.current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        return self._session.thread_status_text

    @property
    def current_model(self) -> AgentModelChanged | None:
        return self._current_model

    @property
    def output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        return self._output_modalities

    @property
    def output_modalities_label(self) -> str:
        return "+".join(self._output_modalities)

    @property
    def thread_id(self) -> str:
        if self._chat_session.has_thread_path:
            return self._chat_session.thread_path
        if self._thread_id is None:
            raise RoomException("chat thread session not started")
        return self._thread_id

    @property
    def thread_generation(self) -> int:
        return self._thread_generation

    @property
    def models_response(self) -> ModelsResponse | None:
        return self._models_response

    @property
    def can_request_initial_models(self) -> bool:
        return self._open_on_start

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._session.queued_message_labels

    @property
    def image_dataset_client(self) -> ImageDatasetClient | None:
        return self._session.image_dataset_client

    @property
    def messages(self):
        return self._session.messages

    async def start(self) -> None:
        if self._started:
            return

        if not self._open_on_start:
            await self._channel_client.start()
            self._started = True
            return

        await self._channel_client.start()
        supervisor = self._bot._supervisor
        await supervisor.route(
            Message(
                data=OpenThread(
                    type=AGENT_MESSAGE_THREAD_OPEN,
                    thread_id=self._thread_id,
                )
            )
        )
        for agent_process in supervisor.processes:
            if agent_process.thread_id != self._thread_id:
                continue
            thread_storage = agent_process.thread_storage
            if thread_storage is None:
                break
            storage_messages = _thread_agent_messages_from_storage(thread_storage)
            if inspect.isawaitable(storage_messages):
                storage_messages = await storage_messages
            for message in storage_messages:
                self._chat_session.add_agent_message(
                    self._stored_agent_message_with_sender(message)
                )
            break
        self._started = True

    def _stored_agent_message_with_sender(self, message: AgentMessage) -> AgentMessage:
        if self._agent_name is None:
            return message
        if isinstance(message, (AgentTextContentDelta, AgentFileContentDelta)):
            if message.sender_name is not None and message.sender_name.strip() != "":
                return message
            return message.model_copy(update={"sender_name": self._agent_name})
        return message

    async def close(self) -> None:
        await self._session.close()
        await self._channel_client.close()

    async def switch_thread(self, thread_path: str) -> None:
        normalized_path = thread_path.strip()
        if normalized_path == "" or normalized_path == self._thread_id:
            return
        await self.close()
        self._thread_generation += 1
        previous_models_response = self._models_response
        self._configure_thread(thread_path=normalized_path, open_on_start=True)
        await self.start()
        if previous_models_response is not None:
            self._apply_models_response(previous_models_response)

    async def new_thread(self) -> None:
        await self.close()
        self._thread_generation += 1
        previous_models_response = self._models_response
        self._configure_thread(thread_path=None, open_on_start=False)
        await self.start()
        if previous_models_response is not None:
            self._apply_models_response(previous_models_response)

    async def delete_thread(self, thread_path: str) -> None:
        await self._chat_session.delete_thread(thread_path)

    async def rename_thread(self, thread_path: str, name: str) -> None:
        await self._chat_session.rename_thread(thread_path, name)

    async def list_threads(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadListEntry]:
        response = await self._chat_session.list_threads(limit=limit, offset=offset)
        return [
            _thread_list_entry_from_agent_entry(entry) for entry in response.threads
        ]

    def add_thread_list_event_listener(
        self,
        callback: Callable[[ThreadListEvent], None],
    ) -> Callable[[], None]:
        def _handle_payload(payload: dict[str, Any]) -> None:
            event = _thread_list_event_from_agent_payload(payload)
            if event is not None:
                callback(event)

        return self._chat_session.add_event_listener(_handle_payload)

    async def ask(
        self,
        *,
        prompt: str,
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        await self.start()
        return await self._session.ask(
            prompt=prompt,
            on_message=on_message,
        )

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        return self._session.steer(
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

    def interrupt(self) -> bool:
        return self._session.interrupt()

    async def request_models(self) -> ModelsResponse:
        payload = ModelsRequest(
            type=AGENT_MESSAGE_MODELS_REQUEST,
        )
        await self._chat_session.send(payload)
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    event = await self._chat_session.receive()
                    if event.get("type") != AGENT_MESSAGE_MODELS_RESPONSE:
                        continue
                    response = ModelsResponse.model_validate(event)
                    if response.source_message_id != payload.message_id:
                        continue
                    self._apply_models_response(response)
                    return response
        except asyncio.TimeoutError as exc:
            raise RoomException("timed out waiting for model list") from exc

    def _apply_models_response(self, response: ModelsResponse) -> None:
        self._models_response = response
        active_model = None
        if self._chat_session.has_thread_path:
            active_model = _active_model_from_models_response(
                response,
                thread_id=self._chat_session.thread_path,
            )
        if active_model is not None:
            self._current_model = active_model
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()

    def select_model(self, model: AgentModelChanged) -> None:
        self._current_model = model
        self._output_modalities = tuple(model.output_modalities)
        if self._selected_model_info() is None:
            return
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()

    def set_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> None:
        self._output_modalities = self._supported_selected_output_modalities(
            output_modalities
        )
        self._sync_turn_output_modalities()

    def toggle_output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        modalities = self._selected_model_modalities()
        if self._output_modalities == ("text",) and "audio" in modalities:
            self._output_modalities = ("audio",)
        else:
            self._output_modalities = ("text",)
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()
        return self._output_modalities

    def _sync_turn_output_modalities(self) -> None:
        output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        if output_modalities == self._output_modalities:
            self._session.set_output_modalities(output_modalities)
            if self._current_model is not None:
                self._current_model = self._current_model.model_copy(
                    update={"output_modalities": list(self._output_modalities)}
                )
            return
        self._session.set_output_modalities(None)

    def _selected_model_info(self) -> AgentModelInfo | None:
        if self._current_model is None or self._models_response is None:
            return None
        for provider in self._models_response.providers:
            if provider.name != self._current_model.provider:
                continue
            for model in provider.models:
                if model.name == self._current_model.model:
                    return model
        return None

    def _selected_model_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        model_info = self._selected_model_info()
        if model_info is None:
            return ("text",)
        return tuple(model_info.modalities)

    def _supported_selected_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> tuple[Literal["text", "audio"], ...]:
        supported = self._selected_model_modalities()
        selected = tuple(output for output in output_modalities if output in supported)
        if len(selected) == 0:
            return ("text",)
        return (selected[0],)

    async def change_model(
        self,
        *,
        provider: str | None,
        model: str | None,
        voice: str | None = None,
    ) -> AgentModelChanged:
        payload = ChangeModel(
            type=AGENT_MESSAGE_MODEL_CHANGE,
            thread_id=self._thread_id,
            provider=provider,
            model=model,
            voice=voice,
        )
        await self._channel_client.send(payload)
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    event = await self._channel_client.receive()
                    if event.get("type") != AGENT_EVENT_MODEL_CHANGED:
                        continue
                    changed = AgentModelChanged.model_validate(event)
                    if changed.source_message_id != payload.message_id:
                        continue
                    self._current_model = changed
                    self._output_modalities = tuple(changed.output_modalities)
                    self._output_modalities = (
                        self._supported_selected_output_modalities(
                            self._output_modalities
                        )
                    )
                    self._sync_turn_output_modalities()
                    return changed
        except asyncio.TimeoutError as exc:
            raise RoomException("timed out waiting for model change") from exc


async def _handle_process_model_command(
    command: str,
    *,
    session: Any,
) -> str | None:
    parts = command.strip().split()
    command_name = parts[0] if parts else ""
    if command_name == "/new":
        if len(parts) != 1:
            return "Usage: /new"
        await session.new_thread()
        return "New thread"
    if command_name == "/provider":
        response = session.models_response
        if response is None:
            response = await session.request_models()
        if len(parts) == 1:
            return _format_provider_list(
                providers=response.providers,
                current_model=session.current_model,
            )
        if len(parts) == 2:
            changed = _selected_default_model_for_provider(
                response=response,
                thread_id=session.thread_id,
                provider_name=parts[1],
            )
            if changed is None:
                return f"Unknown provider: {parts[1]}"
            session.select_model(changed)
            return (
                "Using "
                f"{_provider_model_display_name(provider=changed.provider, model=changed.model)}"
            )
        return "Usage: /provider [provider]"
    if command_name == "/model":
        response = session.models_response
        if response is None:
            response = await session.request_models()
        if len(parts) == 1:
            return _format_model_list(
                providers=response.providers,
                current_model=session.current_model,
            )
        if len(parts) != 2:
            return "Usage: /model [model|provider/model]"

        requested = parts[1]
        provider_name: str | None = None
        model_name = requested
        if "/" in requested:
            provider_name, model_name = requested.split("/", 1)
        else:
            matching_providers = [
                provider
                for provider in response.providers
                if any(model.name == requested for model in provider.models)
            ]
            if len(matching_providers) > 1:
                names = ", ".join(
                    _provider_model_display_name(
                        provider=provider.name,
                        model=requested,
                    )
                    for provider in matching_providers
                )
                return f"Model name is ambiguous. Use one of: {names}"
            if len(matching_providers) == 1:
                provider_name = matching_providers[0].name

        changed = _selected_model_from_models_response(
            response=response,
            thread_id=session.thread_id,
            provider=provider_name,
            model=model_name,
        )
        if changed is None:
            return f"Unknown model: {requested}"
        session.select_model(changed)
        return (
            "Using "
            f"{_provider_model_display_name(provider=changed.provider, model=changed.model)}"
        )
    if command_name == "/output":
        if len(parts) == 1:
            changed_modalities = session.toggle_output_modalities()
            return f"Using {','.join(changed_modalities)} responses"
        if len(parts) != 2:
            return "Usage: /output [text|audio]"
        requested_output = parts[1].strip().lower()
        if requested_output not in ("text", "audio"):
            return "Usage: /output [text|audio]"
        selected_modalities: tuple[Literal["text", "audio"], ...] = tuple(
            output for output in ("text", "audio") if output == requested_output
        )
        current_model_info = _model_info_for_current_selection(
            response=session.models_response,
            current_model=session.current_model,
        )
        supported_modalities = (
            tuple(current_model_info.modalities)
            if current_model_info is not None
            else ("text",)
        )
        unsupported_modalities = [
            output
            for output in selected_modalities
            if output not in supported_modalities
        ]
        if len(unsupported_modalities) > 0:
            model_label = _current_model_label(
                current_model=session.current_model,
                fallback="current model",
            )
            unsupported = ",".join(unsupported_modalities)
            return f"{model_label} does not support {unsupported} responses"
        session.set_output_modalities(selected_modalities)
        return f"Using {','.join(selected_modalities)} responses"
    if command_name == "/voice":
        response = session.models_response
        if response is None:
            response = await session.request_models()
        model_info = _model_info_for_current_selection(
            response=response,
            current_model=session.current_model,
        )
        if model_info is None or len(model_info.available_voices) == 0:
            return "Current model does not advertise output voices"
        current_voice = (
            session.current_model.voice if session.current_model is not None else None
        )
        if current_voice is None:
            current_voice = model_info.default_output_voice
        if len(parts) == 1:
            lines = ["Voices:"]
            for voice in model_info.available_voices:
                marker = "*" if voice == current_voice else " "
                lines.append(f"{marker} {voice}")
            return "\n".join(lines)
        if len(parts) != 2:
            return "Usage: /voice [voice]"
        requested_voice = parts[1].strip()
        if requested_voice not in model_info.available_voices:
            voices = ", ".join(model_info.available_voices)
            return f"Unknown voice: {requested_voice}. Available voices: {voices}"
        current_model = session.current_model
        if current_model is None:
            return "No current model selected"
        changed = await session.change_model(
            provider=current_model.provider,
            model=current_model.model,
            voice=requested_voice,
        )
        return f"Using voice {changed.voice or requested_voice}"
    return None


async def _request_initial_models(*, session: Any) -> None:
    try:
        async with asyncio.timeout(0.5):
            await session.request_models()
    except Exception:
        return


class _ProcessThreadSidebar:
    def __init__(
        self,
        *,
        list_threads: Callable[[], Awaitable[list[ThreadListEntry]]],
        subscribe_thread_events: Callable[
            [Callable[[ThreadListEvent], None]], Callable[[], None]
        ]
        | None = None,
        current_thread_path: Callable[[], str | None],
        switch_thread: Callable[[str], Awaitable[None]],
        delete_thread: Callable[[str], Awaitable[None]],
        rename_thread: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._list_threads = list_threads
        self._subscribe_thread_events = subscribe_thread_events
        self._unsubscribe_thread_events: Callable[[], None] | None = None
        self._current_thread_path = current_thread_path
        self._switch_thread = switch_thread
        self._delete_thread = delete_thread
        self._rename_thread = rename_thread
        self._entries: list[ThreadListEntry] = []
        self._selected_index = 0
        self._message: str | None = None
        self._confirm_delete_path: str | None = None
        self._rename_path: str | None = None
        self._rename_value = ""
        self._refresh_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._refresh_task = asyncio.create_task(self.refresh())
        if self._subscribe_thread_events is not None:
            self._unsubscribe_thread_events = self._subscribe_thread_events(
                self._apply_thread_list_event
            )

    async def close(self) -> None:
        if self._unsubscribe_thread_events is not None:
            self._unsubscribe_thread_events()
        self._unsubscribe_thread_events = None
        tasks = [
            task
            for task in (self._refresh_task,)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None

    async def refresh(self) -> None:
        try:
            self._entries = await self._list_threads()
            self._sync_selection()
            if self._message is not None and self._message.startswith(
                "Unable to load threads:"
            ):
                self._message = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._message = f"Unable to load threads: {exc}"

    def _apply_thread_list_event(self, event: ThreadListEvent) -> None:
        if event.type == "deleted":
            self._entries = [
                entry for entry in self._entries if entry.path != event.path
            ]
            if self._confirm_delete_path == event.path:
                self._confirm_delete_path = None
            if self._rename_path == event.path:
                self._rename_path = None
                self._rename_value = ""
            self._sync_selection()
            return

        entry = event.entry
        if entry is None:
            return
        entries_by_path = {existing.path: existing for existing in self._entries}
        entries_by_path[entry.path] = entry
        self._entries = _sort_process_thread_entries(entries_by_path.values())
        if self._message is not None and self._message.startswith(
            "Thread watch stopped:"
        ):
            self._message = None
        self._sync_selection()

    def _sync_selection(self) -> None:
        current_path = self._current_thread_path()
        if current_path is not None:
            for index, entry in enumerate(self._entries):
                if entry.path == current_path:
                    self._selected_index = index
                    return
        if len(self._entries) == 0:
            self._selected_index = 0
        else:
            self._selected_index = min(self._selected_index, len(self._entries) - 1)

    def render(self, focused: bool):
        from rich.text import Text

        if not focused:
            self._sync_selection()
        text = Text()
        title_style = "bold #7dd3fc" if focused else "bold #9aa5b8"
        text.append("Threads", style=title_style)
        text.append("\n")
        if self._message is not None:
            text.append(self._message, style="#9aa5b8")
            text.append("\n")
        if self._rename_path is not None:
            text.append("Rename: ", style="bold #e5e7eb")
            text.append(self._rename_value or " ", style="#cfd3dc")
            text.append("\n")
        elif self._confirm_delete_path is not None:
            text.append("Backspace again to delete", style="bold #fca5a5")
            text.append("\n")
        text.append("Tab focus  Enter open  r rename\n", style="#6f7b90")
        text.append("Backspace delete\n\n", style="#6f7b90")

        if len(self._entries) == 0:
            if self._refresh_task is not None and not self._refresh_task.done():
                text.append("Loading threads...", style="#9aa5b8")
                return text
            text.append("No threads", style="#9aa5b8")
            return text

        current_path = self._current_thread_path()
        for index, entry in enumerate(self._entries[:100]):
            selected = focused and index == self._selected_index
            current = current_path == entry.path
            prefix = "› " if selected else "  "
            style = "bold #e5e7eb" if selected else "#cfd3dc"
            if current and not selected:
                style = "#7dd3fc"
            text.append(prefix, style="#7dd3fc" if selected else "#6f7b90")
            name = " ".join(entry.name.split()) or entry.path
            if len(name) > 28:
                name = f"{name[:25].rstrip()}..."
            text.append(name, style=style)
            if current:
                text.append(" *", style="#7dd3fc")
            text.append("\n")
        return text

    async def handle_key(self, key: str, character: str | None) -> bool:
        if self._rename_path is not None:
            return await self._handle_rename_key(key=key, character=character)
        if key == "up":
            self._move_selection(-1)
            return True
        if key == "down":
            self._move_selection(1)
            return True
        if key == "enter":
            await self._open_selected_thread()
            return True
        if key == "backspace":
            await self._confirm_or_delete_selected_thread()
            return True
        if key == "r":
            self._begin_rename_selected_thread()
            return True
        return False

    async def handle_click(self, x: int, y: int) -> bool:
        del x
        visible_entries = self._entries[:100]
        entry_index = y - self._entry_line_offset()
        if entry_index < 0 or entry_index >= len(visible_entries):
            return False
        self._selected_index = entry_index
        self._confirm_delete_path = None
        await self._open_selected_thread()
        return True

    def _entry_line_offset(self) -> int:
        offset = 4
        if self._message is not None:
            offset += 1
        if self._rename_path is not None or self._confirm_delete_path is not None:
            offset += 1
        return offset

    def _move_selection(self, delta: int) -> None:
        if len(self._entries) == 0:
            return
        self._selected_index = max(
            0,
            min(len(self._entries) - 1, self._selected_index + delta),
        )
        self._confirm_delete_path = None

    def _selected_entry(self) -> ThreadListEntry | None:
        if len(self._entries) == 0:
            return None
        if self._selected_index < 0 or self._selected_index >= len(self._entries):
            return None
        return self._entries[self._selected_index]

    async def _open_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._confirm_delete_path = None
        await self._switch_thread(entry.path)
        self._message = None

    async def _confirm_or_delete_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if self._confirm_delete_path != entry.path:
            self._confirm_delete_path = entry.path
            self._message = None
            return
        deleted_current = self._current_thread_path() == entry.path
        await self._delete_thread(entry.path)
        self._confirm_delete_path = None
        self._message = None
        self._entries = [
            existing for existing in self._entries if existing.path != entry.path
        ]
        self._sync_selection()
        next_entry = self._selected_entry()
        if deleted_current and next_entry is not None:
            await self._switch_thread(next_entry.path)

    def _begin_rename_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._rename_path = entry.path
        self._rename_value = entry.name
        self._confirm_delete_path = None

    async def _handle_rename_key(self, *, key: str, character: str | None) -> bool:
        if key == "escape":
            self._rename_path = None
            self._rename_value = ""
            return True
        if key == "enter":
            await self._commit_rename()
            return True
        if key == "backspace":
            self._rename_value = self._rename_value[:-1]
            return True
        if character is not None and character.isprintable():
            self._rename_value += character
            return True
        if key == "space":
            self._rename_value += " "
            return True
        return True

    async def _commit_rename(self) -> None:
        if self._rename_path is None:
            return
        name = " ".join(self._rename_value.split())
        if name == "":
            self._message = "Thread name cannot be empty"
            return
        rename_path = self._rename_path
        await self._rename_thread(rename_path, name)
        self._message = None
        self._entries = [
            (
                ThreadListEntry(
                    path=entry.path,
                    name=name,
                    created_at=entry.created_at,
                    modified_at=entry.modified_at,
                )
                if entry.path == rename_path
                else entry
            )
            for entry in self._entries
        ]
        self._rename_path = None
        self._rename_value = ""
        self._sync_selection()


def _process_session_thread_path(session: Any) -> str | None:
    try:
        if isinstance(session, (_ProcessRunSession, _ChatChannelUseSession)):
            return session.thread_id
    except RoomException:
        return None
    return None


def _thread_list_entry_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw == "":
        return datetime.min.replace(tzinfo=timezone.utc)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_process_thread_entries(
    entries: Iterable[ThreadListEntry],
) -> list[ThreadListEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            _thread_list_entry_datetime(entry.modified_at),
            _thread_list_entry_datetime(entry.created_at),
            entry.path,
        ),
        reverse=True,
    )


def _thread_list_entry_from_agent_entry(
    entry: AgentThreadListEntry,
) -> ThreadListEntry:
    return ThreadListEntry(
        path=entry.path,
        name=entry.name,
        created_at=entry.created_at,
        modified_at=entry.modified_at,
    )


def _thread_list_event_from_agent_payload(
    payload: dict[str, Any],
) -> ThreadListEvent | None:
    payload_type = payload.get("type")
    try:
        if payload_type == AGENT_EVENT_THREAD_CREATED:
            created = ThreadCreated.model_validate(payload)
            entry = _thread_list_entry_from_agent_entry(created.thread)
            return ThreadListEvent(
                type="upserted",
                path=entry.path,
                entry=entry,
            )
        if payload_type == AGENT_EVENT_THREAD_UPDATED:
            updated = ThreadUpdated.model_validate(payload)
            entry = _thread_list_entry_from_agent_entry(updated.thread)
            return ThreadListEvent(
                type="upserted",
                path=entry.path,
                entry=entry,
            )
        if payload_type == AGENT_EVENT_THREAD_DELETED:
            deleted = ThreadDeleted.model_validate(payload)
            return ThreadListEvent(
                type="deleted",
                path=deleted.path,
                entry=None,
            )
    except Exception:
        return None
    return None


async def _run_process_run_tui(
    *,
    bot: Any,
    room: RoomClient | None = None,
    model: str | list[str],
    voice: str | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    thread_path: str | None,
    thread_storage: "ThreadStorageBackend",
    agent_name: str | None,
    thread_dir: str | None,
    threading_mode: "ThreadingMode",
    message: str | None,
    working_dir: str | None,
    chat_client: ChatThreadSession | None = None,
) -> None:
    from meshagent.cli import ask as ask_module

    configured_models = _normalize_model_options(model)
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    thread_id = _process_run_thread_id(
        thread_path=thread_path,
        thread_storage=thread_storage,
        agent_name=agent_name,
        thread_dir=thread_dir,
        threading_mode=threading_mode,
    )
    initial_model = (
        _agent_model_changed_for_model(
            model=configured_models[0],
            thread_id=thread_id,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=realtime_protocols,
            output_modalities=selected_output_modalities,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
        )
        if len(configured_models) > 0
        else None
    )
    display_model = _current_model_label(
        current_model=initial_model,
        fallback=", ".join(configured_models),
    )
    if chat_client is None:
        session = _ProcessRunSession(
            bot=bot,
            model=None,
            thread_path=thread_path,
            thread_storage=thread_storage,
            agent_name=agent_name,
            thread_dir=thread_dir,
            threading_mode=threading_mode,
            current_working_directory=working_dir,
            initial_model=initial_model,
            image_dataset_client=ImageDatasetClient(room) if room is not None else None,
        )
    else:
        session = _ChatChannelUseSession(
            chat_client=chat_client,
            current_working_directory=working_dir,
        )
        if initial_model is not None:
            session.select_model(initial_model)
    thread_sidebar: _ProcessThreadSidebar | None = None
    try:
        await session.start()
        if len(configured_models) > 0:
            session._apply_models_response(
                _configured_models_response(
                    models=configured_models,
                    current_model=session.current_model,
                    voice=voice,
                    turn_detection=turn_detection,
                    realtime_protocols=realtime_protocols,
                    output_modalities=selected_output_modalities,
                    input_audio_format=input_audio_format,
                    input_audio_sample_rate=input_audio_sample_rate,
                    input_audio_bitrate=input_audio_bitrate,
                    output_audio_format=output_audio_format,
                    output_audio_sample_rate=output_audio_sample_rate,
                    output_audio_bitrate=output_audio_bitrate,
                )
            )
        if session.can_request_initial_models:
            await _request_initial_models(session=session)
        if message is not None:

            def _write_message(agent_message: AgentMessage) -> None:
                if isinstance(agent_message, AgentTextContentDelta):
                    click.echo(agent_message.text, nl=False)

            await session.ask(
                prompt=message,
                on_message=_write_message,
            )
            click.echo()
            return

        if thread_storage != "none" and thread_dir is not None:
            thread_sidebar = _ProcessThreadSidebar(
                list_threads=lambda: session.list_threads(limit=100, offset=0),
                subscribe_thread_events=session.add_thread_list_event_listener,
                current_thread_path=lambda: _process_session_thread_path(session),
                switch_thread=session.switch_thread,
                delete_thread=session.delete_thread,
                rename_thread=session.rename_thread,
            )
            await thread_sidebar.start()

        await ask_module._run_ask_tui(
            model=display_model,
            session=session,
            title="meshagent process run",
            command_handler=lambda command: _handle_process_model_command(
                command,
                session=session,
            ),
            model_label_provider=lambda: _current_model_label(
                current_model=session.current_model,
                fallback=display_model,
            ),
            command_options_provider=lambda prompt: _process_command_options(
                prompt,
                response=session.models_response,
                current_model=session.current_model,
                current_output_modalities=session.output_modalities,
            ),
            output_label_provider=lambda: session.output_modalities_label,
            side_panel_renderer=(
                thread_sidebar.render if thread_sidebar is not None else None
            ),
            side_panel_key_handler=(
                thread_sidebar.handle_key if thread_sidebar is not None else None
            ),
            side_panel_mouse_handler=(
                thread_sidebar.handle_click if thread_sidebar is not None else None
            ),
        )
    finally:
        if thread_sidebar is not None:
            await thread_sidebar.close()
        await session.close()


class _ChatChannelUseSession:
    def __init__(
        self,
        *,
        chat_client: ChatThreadSession,
        current_working_directory: str | None = None,
    ) -> None:
        self._chat_client = chat_client
        self._current_working_directory = current_working_directory
        self._thread_generation = 0
        current_model = chat_client.current_model
        self._output_modalities: tuple[OutputModality, ...] = (
            tuple(current_model.output_modalities)
            if current_model is not None
            else ("text",)
        )
        self._session = self._build_session()
        self._sync_turn_output_modalities()
        self._started = False

    def _build_session(self):
        from meshagent.cli import ask as ask_module

        return ask_module._AgentMessageSession(
            client=self._chat_client,
            model=None,
            current_working_directory=self._current_working_directory,
            local_participant_name=self._chat_client.local_participant_name,
            model_provider=lambda: self._chat_client.current_model,
            start_thread_callback=self._start_thread,
        )

    @property
    def thread_generation(self) -> int:
        return self._thread_generation

    async def switch_thread(self, thread_path: str) -> None:
        normalized_path = thread_path.strip()
        if normalized_path == "" or (
            self._chat_client.has_thread_path
            and normalized_path == self._chat_client.thread_path
        ):
            return
        pending_session = self._chat_client
        await self._session.close(close_client=False)
        await pending_session.close(close_client=False)
        new_session = await pending_session.client.open_thread(
            normalized_path,
            local_participant_name=pending_session.local_participant_name,
            close_client_on_close=True,
            load=True,
        )
        self._chat_client = new_session
        self._session = self._build_session()
        self._sync_turn_output_modalities()
        self._started = False
        self._thread_generation += 1
        await self.start()
        if self._chat_client.has_thread_path:
            await self.request_models()

    async def new_thread(self) -> None:
        pending_session = self._chat_client
        await self._session.close(close_client=False)
        await pending_session.close(close_client=False)
        new_session = ChatThreadSession(
            client=pending_session.client,
            thread_path=None,
            local_participant_name=pending_session.local_participant_name,
            close_client_on_close=True,
        )
        self._chat_client = new_session
        self._session = self._build_session()
        self._sync_turn_output_modalities()
        self._started = False
        self._thread_generation += 1
        await self.start()

    async def delete_thread(self, thread_path: str) -> None:
        await self._chat_client.delete_thread(thread_path)

    async def rename_thread(self, thread_path: str, name: str) -> None:
        await self._chat_client.rename_thread(thread_path, name)

    async def list_threads(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadListEntry]:
        response = await self._chat_client.list_threads(limit=limit, offset=offset)
        return [
            _thread_list_entry_from_agent_entry(entry) for entry in response.threads
        ]

    def add_thread_list_event_listener(
        self,
        callback: Callable[[ThreadListEvent], None],
    ) -> Callable[[], None]:
        def _handle_payload(payload: dict[str, Any]) -> None:
            event = _thread_list_event_from_agent_payload(payload)
            if event is not None:
                callback(event)

        return self._chat_client.client.add_event_listener(_handle_payload)

    @property
    def current_working_directory(self) -> str:
        return self._session.current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        return self._session.thread_status_text

    @property
    def current_model(self) -> AgentModelChanged | None:
        return self._chat_client.current_model

    @property
    def thread_id(self) -> str:
        return self._chat_client.thread_path

    @property
    def models_response(self) -> ModelsResponse | None:
        return self._chat_client.models_response

    @property
    def can_request_initial_models(self) -> bool:
        return self._chat_client.has_thread_path

    @property
    def output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        return self._output_modalities

    @property
    def output_modalities_label(self) -> str:
        return "+".join(self._output_modalities)

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._session.queued_message_labels

    @property
    def image_dataset_client(self) -> ImageDatasetClient | None:
        return self._session.image_dataset_client

    @property
    def messages(self):
        return self._session.messages

    async def _start_thread(self, start_thread: StartThread) -> ChatThreadSession:
        pending_session = self._chat_client
        await pending_session.close(close_client=False)

        def _adopt_pending_session(new_session: ChatThreadSession) -> None:
            self._chat_client = new_session
            self._session.replace_client(new_session)
            self._sync_turn_output_modalities()

        new_session = await pending_session.client.start_thread(
            start_thread,
            local_participant_name=pending_session.local_participant_name,
            close_client_on_close=True,
            on_pending_session=_adopt_pending_session,
        )
        self._chat_client = new_session
        self._session.replace_client(new_session)
        self._sync_turn_output_modalities()
        return new_session

    async def start(self) -> None:
        if self._started:
            return
        if not self._chat_client.has_thread_path:
            self._started = True
            return
        self._started = True

    async def close(self) -> None:
        await self._session.close(close_client=False)
        if isinstance(self._chat_client, ChatThreadSession):
            await self._chat_client.close()

    async def ask(
        self,
        *,
        prompt: str,
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        return await self._session.ask(
            prompt=prompt,
            on_message=on_message,
        )

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        return self._session.steer(
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

    def interrupt(self) -> bool:
        return self._session.interrupt()

    async def request_models(self) -> ModelsResponse:
        response = await self._chat_client.request_models()
        self._sync_turn_output_modalities()
        return response

    def _apply_models_response(self, response: ModelsResponse) -> None:
        self._chat_client.apply_models_response(response)
        self._sync_turn_output_modalities()

    def select_model(self, model: AgentModelChanged) -> None:
        self._chat_client.select_model(model)
        self._output_modalities = tuple(model.output_modalities)
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()

    def set_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> None:
        self._output_modalities = self._supported_selected_output_modalities(
            output_modalities
        )
        self._sync_turn_output_modalities()

    def toggle_output_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        modalities = self._selected_model_modalities()
        if self._output_modalities == ("text",) and "audio" in modalities:
            self._output_modalities = ("audio",)
        else:
            self._output_modalities = ("text",)
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()
        return self._output_modalities

    def _sync_turn_output_modalities(self) -> None:
        output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        if output_modalities == self._output_modalities:
            self._session.set_output_modalities(output_modalities)
            current_model = self._chat_client.current_model
            if current_model is not None:
                self._chat_client.select_model(
                    current_model.model_copy(
                        update={"output_modalities": list(self._output_modalities)}
                    )
                )
            return
        self._session.set_output_modalities(None)

    def _selected_model_modalities(self) -> tuple[Literal["text", "audio"], ...]:
        model_info = _model_info_for_current_selection(
            response=self.models_response,
            current_model=self.current_model,
        )
        if model_info is None:
            return ("text",)
        return tuple(model_info.modalities)

    def _supported_selected_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...]
    ) -> tuple[Literal["text", "audio"], ...]:
        supported = self._selected_model_modalities()
        selected = tuple(output for output in output_modalities if output in supported)
        if len(selected) == 0:
            return ("text",)
        return (selected[0],)

    async def change_model(
        self,
        *,
        provider: str | None,
        model: str | None,
        voice: str | None = None,
    ) -> AgentModelChanged:
        changed = await self._chat_client.change_model(
            provider=provider,
            model=model,
            voice=voice,
        )
        self._output_modalities = tuple(changed.output_modalities)
        self._output_modalities = self._supported_selected_output_modalities(
            self._output_modalities
        )
        self._sync_turn_output_modalities()
        return changed


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


async def _open_process_room_client(
    *,
    account_client: Any,
    project_id: str,
    room: str,
) -> RoomClient:
    connection = await account_client.connect_room(project_id=project_id, room=room)
    user_client = RoomClient(
        protocol_factory=WebSocketClientProtocol(
            url=websocket_room_url(room_name=room),
            token=connection.jwt,
        ).create_factory(),
    )
    try:
        await user_client.__aenter__()
        return user_client
    except Exception:
        await _close_process_use_room_client(user_client)
        raise


def _process_use_remote_participant(
    *,
    room: RoomClient,
    participant_name: str,
) -> RemoteParticipant:
    for participant in room.messaging.get_participants():
        if participant.get_attribute("name") == participant_name:
            return participant
    raise RoomException(f"chat participant {participant_name} is not available")


def _resolve_process_use_chat_thread_path(
    *,
    room: RoomClient,
    participant_name: str,
    thread_path: str | None,
) -> str | None:
    normalized_thread_path = _normalized_annotation_string(thread_path)
    if normalized_thread_path is not None:
        return normalized_thread_path

    participant = _process_use_remote_participant(
        room=room,
        participant_name=participant_name,
    )
    if _participant_chat_threading_mode(
        participant=participant
    ) == "default-new" and _participant_supports_agent_messages(
        participant=participant,
    ):
        return None

    return _participant_chat_thread_path(
        participant=participant,
        participant_name=participant_name,
    )


async def _open_process_use_chat_session(
    *,
    account_client: Any,
    project_id: str,
    room: str,
    participant_name: str,
    thread_path: str | None,
) -> tuple[RoomClient, ChatThreadSession]:
    user_client = await _open_process_room_client(
        account_client=account_client,
        project_id=project_id,
        room=room,
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
        resolved_thread_path = _resolve_process_use_chat_thread_path(
            room=user_client,
            participant_name=participant_name,
            thread_path=thread_path,
        )
        if resolved_thread_path is None:
            chat_session = ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
            )
        else:
            chat_session = await chat_client.open_thread(
                resolved_thread_path,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
                load=True,
            )
        return user_client, chat_session
    except Exception:
        await _close_process_use_chat_client(chat_session)
        if chat_session is None and chat_client is not None:
            await chat_client.__aexit__(None, None, None)
        await _close_process_use_room_client(user_client)
        raise


async def _open_process_run_websocket_chat_session(
    *,
    room: RoomClient,
    websocket_config: "_WebSocketChannelConfig",
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
    thread_path: str | None,
    thread_storage: "ThreadStorageBackend",
    agent_name: str | None,
    thread_dir: str | None,
    threading_mode: "ThreadingMode",
) -> ChatThreadSession:
    headers = _process_run_websocket_headers(
        room=room,
        user=user,
        websocket_auth=websocket_auth,
        iap_token=iap_token,
    )
    chat_client = WebSocketChatClient(
        url=_websocket_client_url(websocket_config),
        headers=headers,
    )
    try:
        await chat_client.__aenter__()
        local_participant_name = user.strip()
        resolved_thread_path = _process_run_thread_id(
            thread_path=thread_path,
            thread_storage=thread_storage,
            agent_name=agent_name,
            thread_dir=thread_dir,
            threading_mode=threading_mode,
        )
        if threading_mode == "default-new" and (
            thread_path is None or thread_path.strip() == ""
        ):
            return ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
            )
        return await chat_client.open_thread(
            resolved_thread_path,
            local_participant_name=local_participant_name,
            close_client_on_close=True,
            load=True,
        )
    except Exception:
        await chat_client.__aexit__(None, None, None)
        raise


def _normalize_process_use_websocket_url(websocket_url: str) -> str:
    normalized = websocket_url.strip()
    if normalized == "":
        raise typer.BadParameter("--websocket-url cannot be empty")
    parsed = urlparse(normalized)
    if parsed.scheme not in ("ws", "wss") or parsed.netloc == "":
        raise typer.BadParameter("--websocket-url must be a ws:// or wss:// URL")
    return normalized


def _websocket_iap_cookie_headers(
    *,
    token: str | None,
) -> dict[str, str]:
    if token is None or token.strip() == "":
        raise typer.BadParameter(
            "a room participant token is required for --websocket-auth=iap"
        )
    return {"Cookie": f"__meshagent_iap={token.strip()}"}


def _process_use_websocket_headers(
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


async def _open_process_use_websocket_chat_session(
    *,
    websocket_url: str,
    agent_name: str,
    user: str,
    websocket_auth: WebSocketAuthMode,
    iap_token: str | None = None,
    thread_path: str | None,
) -> ChatThreadSession:
    local_participant_name = user.strip()
    chat_client = WebSocketChatClient(
        url=_normalize_process_use_websocket_url(websocket_url),
        headers=_process_use_websocket_headers(
            agent_name=agent_name,
            user=user,
            websocket_auth=websocket_auth,
            iap_token=iap_token,
        ),
    )
    try:
        await chat_client.__aenter__()
        resolved_thread_path = _normalized_annotation_string(thread_path)
        if resolved_thread_path is None:
            return ChatThreadSession(
                client=chat_client,
                thread_path=None,
                local_participant_name=local_participant_name,
                close_client_on_close=True,
            )
        return await chat_client.open_thread(
            resolved_thread_path,
            local_participant_name=local_participant_name,
            close_client_on_close=True,
            load=True,
        )
    except Exception:
        await chat_client.__aexit__(None, None, None)
        raise


def _thread_dir_from_thread_list_path(thread_list_path: str) -> str | None:
    normalized_path = thread_list_path.strip().rstrip("/")
    if normalized_path == "":
        return None
    if "/" not in normalized_path:
        return None
    return normalized_path.rsplit("/", 1)[0]


def _process_use_thread_sidebar_options(
    *,
    room: RoomClient,
    agent_name: str,
) -> tuple[Any, str] | None:
    if not isinstance(room, RoomClient):
        return None
    participant: RemoteParticipant | None = None
    for candidate in room.messaging.get_participants():
        if candidate.get_attribute("name") != agent_name:
            continue
        participant = candidate
        break
    if participant is None:
        return None

    thread_dir = _normalized_annotation_string(
        participant.get_attribute("meshagent.chatbot.thread-dir")
    )
    thread_list_path = _normalized_annotation_string(
        participant.get_attribute("meshagent.chatbot.thread-list")
    )
    if thread_dir is None and thread_list_path is not None:
        thread_dir = _thread_dir_from_thread_list_path(thread_list_path)
    if thread_dir is None:
        return None
    if thread_dir.startswith("tmp://"):
        return None
    if thread_dir.startswith("dataset://") or (
        thread_list_path is not None and thread_list_path.startswith("dataset://")
    ):
        storage_class = _thread_storage_class_for_backend("dataset")
    else:
        storage_class = _thread_storage_class_for_backend("meshdocument")
    if storage_class is None:
        return None
    return storage_class, thread_dir


async def _run_process_use_tui(
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
    from meshagent.cli import ask as ask_module

    async def _handle_model_command(command: str) -> str | None:
        if session is None:
            raise RoomException("process use session not started")
        return await _handle_process_model_command(command, session=session)

    user_client: RoomClient | None = None
    chat_client: ChatThreadSession | None = None
    session: _ChatChannelUseSession | None = None
    thread_sidebar: _ProcessThreadSidebar | None = None
    try:
        if websocket_url is None:
            if account_client is None:
                raise RoomException("process use account client is unavailable")
            user_client, chat_client = await _open_process_use_chat_session(
                account_client=account_client,
                project_id=project_id,
                room=room,
                participant_name=agent_name,
                thread_path=thread_path,
            )
        else:
            chat_client = await _open_process_use_websocket_chat_session(
                websocket_url=websocket_url,
                agent_name=agent_name,
                user=user,
                websocket_auth=websocket_auth,
                iap_token=iap_token,
                thread_path=thread_path,
            )
        session = _ChatChannelUseSession(chat_client=chat_client)
        await session.start()
        if chat_client.has_thread_path:
            await _request_initial_models(session=session)

        if message is not None:

            def _write_message(agent_message: AgentMessage) -> None:
                if isinstance(agent_message, AgentTextContentDelta):
                    click.echo(agent_message.text, nl=False)

            await session.ask(
                prompt=message,
                on_message=_write_message,
            )
            click.echo()
            return

        if session is not None:
            thread_sidebar = _ProcessThreadSidebar(
                list_threads=lambda: session.list_threads(limit=100, offset=0),
                subscribe_thread_events=session.add_thread_list_event_listener,
                current_thread_path=lambda: (
                    _process_session_thread_path(session)
                    if session is not None
                    else None
                ),
                switch_thread=session.switch_thread,
                delete_thread=session.delete_thread,
                rename_thread=session.rename_thread,
            )
            await thread_sidebar.start()

        await ask_module._run_ask_tui(
            model="remote",
            session=session,
            title=f"meshagent process use: {agent_name}",
            assistant_name=agent_name,
            command_handler=_handle_model_command,
            model_label_provider=lambda: _current_model_label(
                current_model=session.current_model if session is not None else None,
                fallback="remote",
            ),
            command_options_provider=lambda prompt: _process_command_options(
                prompt,
                response=session.models_response if session is not None else None,
                current_model=session.current_model if session is not None else None,
                current_output_modalities=(
                    session.output_modalities if session is not None else ("text",)
                ),
            ),
            output_label_provider=lambda: (
                session.output_modalities_label if session is not None else "text"
            ),
            side_panel_renderer=(
                thread_sidebar.render if thread_sidebar is not None else None
            ),
            side_panel_key_handler=(
                thread_sidebar.handle_key if thread_sidebar is not None else None
            ),
            side_panel_mouse_handler=(
                thread_sidebar.handle_click if thread_sidebar is not None else None
            ),
        )
    finally:
        if thread_sidebar is not None:
            await thread_sidebar.close()
        if session is not None:
            await session.close()
        await _close_process_use_chat_client(chat_client)
        await _close_process_use_room_client(user_client)


app = async_typer.AsyncTyper(help="Join a process-backed agent to a room")
app.add_deprecated_option_aliases(
    {**DEPRECATED_REQUIRE_OPTION_ALIASES, "--database-namespace": "--dataset-namespace"}
)

ThreadingMode = Literal["none", "default-new"]
ThreadStorageBackend = Literal["meshdocument", "dataset", "none"]
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

PreambleRuleOption = Annotated[
    bool,
    typer.Option(
        "--preamble-rule/--no-preamble-rule",
        help=(
            "Include the default rule asking the model to send concise pre-tool "
            "preambles when no custom rules are configured."
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
            "Defaults to /agents/<agent-name>/threads for process agents when "
            "threading mode is enabled."
        ),
    ),
]

ThreadStorageOption = Annotated[
    ThreadStorageBackend,
    typer.Option(
        "--thread-storage",
        help="Thread storage backend for process agents.",
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

ReasoningEffortOption = Annotated[
    Optional[str],
    typer.Option(
        "--reasoning-effort",
        help="Reasoning effort to request from OpenAI Responses models.",
    ),
]

DecisionModelOption = Annotated[
    Optional[str],
    typer.Option(
        "--decision-model",
        help="Model used for thread naming and other secondary LLM decisions",
    ),
]

TranscriptionModelOption = Annotated[
    str,
    typer.Option(
        "--transcription-model",
        help="Realtime input audio transcription model.",
    ),
]

VoiceOption = Annotated[
    Optional[str],
    typer.Option("--voice", help="Default OpenAI Realtime voice preset."),
]

TurnDetectionOption = Annotated[
    Literal["none", "automatic"],
    typer.Option(
        "--turn-detection",
        help="OpenAI Realtime audio turn detection mode: none or automatic.",
    ),
]

RealtimeProtocolOption = Annotated[
    list[str],
    typer.Option(
        "--realtime-protocol",
        help=(
            "Realtime connection protocol to advertise for OpenAI Realtime. "
            "Pass multiple times to set an ordered preference list."
        ),
    ),
]

OutputModalityOption = Annotated[
    list[str],
    typer.Option(
        "--output-modality",
        help=(
            "Restrict supported response output modalities to text or audio. "
            "Pass multiple times to allow multiple output modalities; omit to allow all."
        ),
    ),
]

InputAudioFormatOption = Annotated[
    str,
    typer.Option("--input-audio-format", help="Realtime input audio MIME type."),
]

InputAudioSampleRateOption = Annotated[
    Optional[int],
    typer.Option(
        "--input-audio-sample-rate",
        help="Realtime input audio sample rate.",
    ),
]

InputAudioBitrateOption = Annotated[
    Optional[int],
    typer.Option("--input-audio-bitrate", help="Realtime input audio bitrate."),
]

OutputAudioFormatOption = Annotated[
    str,
    typer.Option("--output-audio-format", help="Realtime output audio MIME type."),
]

OutputAudioSampleRateOption = Annotated[
    Optional[int],
    typer.Option(
        "--output-audio-sample-rate",
        help="Realtime output audio sample rate.",
    ),
]

OutputAudioBitrateOption = Annotated[
    Optional[int],
    typer.Option("--output-audio-bitrate", help="Realtime output audio bitrate."),
]

ChannelOption = Annotated[
    list[str],
    typer.Option(
        "--channel",
        help=(
            "Attach a channel to the agent process. "
            "Can be repeated. Currently supported: chat, mail:EMAIL_ADDRESS, "
            "queue:QUEUE_NAME, toolkit:NAME, websocket:PORT, "
            "websocket://HOST:PORT."
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


@dataclass(frozen=True, slots=True)
class _WebSocketChannelConfig:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class _WebSocketChannelServer:
    runner: web.AppRunner

    async def stop(self) -> None:
        await self.runner.cleanup()


def _current_command_runtime() -> Literal["process"]:
    return "process"


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


def _default_process_thread_dir(*, agent_name: str | None) -> str | None:
    if agent_name is None:
        return None
    normalized = agent_name.strip()
    if normalized == "":
        return None
    return f"/agents/{normalized}/threads"


def _resolve_process_threading_options(
    *,
    agent_name: str | None,
    threading_mode: ThreadingMode,
    thread_dir: str | None,
    thread_storage: ThreadStorageBackend = "meshdocument",
) -> tuple[ThreadingMode, str | None]:
    if thread_dir is not None:
        return threading_mode, thread_dir

    default_thread_dir = _default_process_thread_dir(agent_name=agent_name)
    if default_thread_dir is None:
        return threading_mode, thread_dir

    if threading_mode == "none" and thread_storage not in ("dataset", "none"):
        return threading_mode, thread_dir

    if thread_storage == "dataset":
        return threading_mode, _dataset_thread_url_for_path(path=default_thread_dir)
    if thread_storage == "none":
        return threading_mode, _thread_url_for_path(
            scheme="tmp",
            path=default_thread_dir,
        )

    return threading_mode, default_thread_dir


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

        if normalized[:10].casefold() == "websocket:":
            websocket_config = _parse_websocket_channel(channel=normalized)
            channel_key = _websocket_channel_key(websocket_config)
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


def _process_run_websocket_channel(
    *,
    channels: list[str],
) -> _WebSocketChannelConfig | None:
    if _has_chat_channel(channels=channels):
        return None
    for channel in channels:
        if _is_websocket_channel(channel):
            return _parse_websocket_channel(channel=channel)
    return None


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


def _process_run_websocket_headers(
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
    local_agent_name = _local_process_participant_name(room)
    if local_agent_name is None:
        raise typer.BadParameter("local process participant name is unavailable")
    token = ParticipantToken(name=normalized_user)
    token.add_agent_grant(local_agent_name)
    return {"Authorization": f"Bearer {token.to_jwt(token=secret)}"}


def _token_from_websocket_protocol_header(request: web.Request) -> str | None:
    protocols = request.headers.get("Sec-WebSocket-Protocol")
    if protocols is None:
        return None

    for protocol in protocols.split(","):
        normalized = protocol.strip()
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


def _local_process_participant_name(room: RoomClient) -> str | None:
    local_participant = room.local_participant
    name = local_participant.get_attribute("name")
    if isinstance(name, str) and name.strip() != "":
        return name.strip()
    if isinstance(local_participant.id, str) and local_participant.id.strip() != "":
        return local_participant.id.strip()
    return None


def _has_matching_agent_grant(
    *,
    token: ParticipantToken,
    agent_name: str,
) -> bool:
    for grant in token.grants:
        if grant.name == "agent" and grant.scope == agent_name:
            return True
    return False


def _authorize_process_websocket_request(
    *,
    request: web.Request,
    room: RoomClient,
    websocket_auth: WebSocketAuthMode,
) -> Participant:
    if websocket_auth == "none":
        return Participant(
            id="websocket-user",
            attributes={
                "name": "websocket-user",
                "role": "user",
            },
        )

    if websocket_auth == "iap":
        user_name = request.headers.get("X-MESHAGENT-USER")
        if user_name is None or user_name.strip() == "":
            raise web.HTTPUnauthorized(text="X-MESHAGENT-USER header is required")
        user_name = user_name.strip()
        return Participant(
            id=user_name,
            attributes={
                "name": user_name,
                "role": "user",
            },
        )

    secret = os.getenv("MESHAGENT_SECRET")
    if secret is None or secret == "":
        raise web.HTTPServiceUnavailable(text="MESHAGENT_SECRET is required")

    local_agent_name = _local_process_participant_name(room)
    if local_agent_name is None:
        raise web.HTTPServiceUnavailable(text="local participant name is unavailable")

    token_str = _participant_token_from_websocket_request(request)
    try:
        token = ParticipantToken.from_jwt(token_str, token=secret)
    except jwt.PyJWTError as exc:
        raise web.HTTPUnauthorized(text="invalid websocket participant token") from exc

    if not _has_matching_agent_grant(token=token, agent_name=local_agent_name):
        raise web.HTTPForbidden(text="token is missing the required agent grant")

    return Participant(
        id=token.name,
        attributes={
            "name": token.name,
            "role": token.role,
        },
    )


async def _start_process_websocket_channel_server(
    *,
    config: _WebSocketChannelConfig,
    channel,
) -> _WebSocketChannelServer:
    app = web.Application()
    app.router.add_get("/healthz", _process_websocket_channel_healthz)
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


async def _process_websocket_channel_healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


def _has_chat_channel(*, channels: list[str]) -> bool:
    return "chat" in channels


def _require_resolved_room(room: str | None) -> str:
    if room is None or room.strip() == "":
        print("[bold red]--room is required (or set MESHAGENT_ROOM)[/bold red]")
        raise typer.Exit(1)
    return room.strip()


def _normalized_thread_dir(*, thread_dir: Optional[str]) -> Optional[str]:
    if thread_dir is None:
        return None

    normalized = thread_dir.strip().rstrip("/")
    if normalized == "":
        return None

    return normalized


def _thread_url_for_path(*, scheme: str, path: str) -> str:
    return f"{scheme}://{path.strip().lstrip('/')}"


def _dataset_thread_url_for_path(*, path: str) -> str:
    return _thread_url_for_path(scheme="dataset", path=path)


def _normalized_annotation_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()
    if normalized == "":
        return None

    return normalized


def _chat_thread_path_for_dir(thread_dir: str) -> str:
    if thread_dir.startswith("dataset://") or thread_dir.startswith("tmp://"):
        return f"{thread_dir}/main"
    return f"{thread_dir}/main.thread"


def _process_thread_path_for_dir(
    *,
    thread_dir: str,
    thread_storage: "ThreadStorageBackend",
) -> str:
    if thread_storage == "dataset" and not thread_dir.startswith("dataset://"):
        return _dataset_thread_url_for_path(path=f"{thread_dir}/main")
    if thread_storage == "none" and not thread_dir.startswith("tmp://"):
        return _thread_url_for_path(scheme="tmp", path=f"{thread_dir}/main")
    return _chat_thread_path_for_dir(thread_dir)


def _new_process_thread_path_for_dir(
    *,
    thread_dir: str,
    thread_storage: "ThreadStorageBackend",
) -> str:
    path = posixpath.join(thread_dir.strip().strip("/"), str(uuid.uuid4()))
    if thread_storage == "dataset" and not thread_dir.startswith("dataset://"):
        return _dataset_thread_url_for_path(path=path)
    if thread_storage == "none" and not thread_dir.startswith("tmp://"):
        return _thread_url_for_path(scheme="tmp", path=path)
    if thread_dir.startswith("dataset://"):
        return f"{thread_dir}/{posixpath.basename(path)}"
    if thread_dir.startswith("tmp://"):
        return f"{thread_dir}/{posixpath.basename(path)}"
    return f"/{path}.thread"


def _default_chat_thread_path_for_agent(agent_name: str | None) -> str | None:
    normalized_agent_name = _normalized_annotation_string(agent_name)
    if normalized_agent_name is None:
        return None
    return f".threads/{normalized_agent_name}/main.thread"


def _default_process_thread_path_for_agent(
    *,
    agent_name: str | None,
    thread_storage: "ThreadStorageBackend",
) -> str | None:
    normalized_agent_name = _normalized_annotation_string(agent_name)
    if normalized_agent_name is None:
        return None

    thread_dir = f".threads/{normalized_agent_name}"
    return _process_thread_path_for_dir(
        thread_dir=thread_dir,
        thread_storage=thread_storage,
    )


def _participant_chat_threading_mode(
    *,
    participant: RemoteParticipant,
) -> str | None:
    return _normalized_annotation_string(
        participant.get_attribute("meshagent.chatbot.threading")
    )


def _participant_supports_agent_messages(*, participant: RemoteParticipant) -> bool:
    return participant.get_attribute("supports_agent_messages") is True


def _participant_chat_thread_path(
    *,
    participant: RemoteParticipant,
    participant_name: str,
) -> str:
    thread_path = _normalized_annotation_string(
        participant.get_attribute("meshagent.chatbot.thread-path")
    )
    if thread_path is not None:
        return thread_path

    thread_dir = _normalized_annotation_string(
        participant.get_attribute("meshagent.chatbot.thread-dir")
    )
    if thread_dir is not None:
        return _chat_thread_path_for_dir(thread_dir)

    default_thread_path = _default_chat_thread_path_for_agent(participant_name)
    if default_thread_path is not None:
        return default_thread_path

    return ".threads/main.thread"


def _normalized_decision_model(*, decision_model: Optional[str]) -> Optional[str]:
    if not isinstance(decision_model, str):
        return None

    normalized = decision_model.strip()
    if normalized == "":
        return None

    return normalized


_OPENAI_REALTIME_MODEL_ALIASES = {
    "openai realtime",
    "openai-realtime",
    "openai:realtime",
    "openai/realtime",
}
_DEFAULT_OPENAI_REALTIME_MODEL = "gpt-realtime"
_DEFAULT_OPENAI_REALTIME_DECISION_MODEL = "gpt-5.4-mini"


def _openai_realtime_session_options(
    *,
    output_modalities: tuple[OutputModality, ...],
) -> dict[str, Any]:
    return {"output_modalities": list(output_modalities)}


def _openai_realtime_response_options(
    *,
    output_modalities: tuple[OutputModality, ...],
) -> dict[str, Any]:
    return {"output_modalities": list(output_modalities)}


def _normalize_output_modalities(
    output_modalities: Iterable[str] | None,
) -> tuple[OutputModality, ...]:
    selected: list[OutputModality] = []
    for raw_modality in output_modalities or ():
        normalized_modality = raw_modality.strip().lower()
        if normalized_modality not in {"text", "audio"}:
            raise typer.BadParameter("output modality must be one of: text, audio")
        modality: OutputModality = "audio" if normalized_modality == "audio" else "text"
        if modality not in selected:
            selected.append(modality)
    if len(selected) == 0:
        return ("text", "audio")
    return tuple(selected)


def _default_realtime_output_modalities(
    output_modalities: tuple[OutputModality, ...],
) -> tuple[OutputModality, ...]:
    if "text" in output_modalities:
        return ("text",)
    if len(output_modalities) > 0:
        return (output_modalities[0],)
    return ("text",)


def _audio_format_option(
    *,
    audio_format: str,
    sample_rate: int | None,
    bitrate: int | None,
) -> LLMAudioFormat:
    normalized_format = audio_format.strip()
    if normalized_format == "":
        normalized_format = "audio/pcm"
    return LLMAudioFormat(
        type=normalized_format,
        sample_rate=sample_rate,
        bitrate=bitrate,
    )


def _normalize_realtime_protocols(
    protocols: list[str] | tuple[str, ...] | None,
) -> tuple[Literal["websocket", "webrtc"], ...]:
    values = protocols or DEFAULT_OPENAI_REALTIME_PROTOCOLS
    normalized: list[Literal["websocket", "webrtc"]] = []
    for raw_protocol in values:
        protocol = raw_protocol.strip().lower()
        if protocol not in {"websocket", "webrtc"}:
            raise typer.BadParameter(
                "realtime protocol must be one of: websocket, webrtc"
            )
        typed_protocol: Literal["websocket", "webrtc"] = (
            "webrtc" if protocol == "webrtc" else "websocket"
        )
        if typed_protocol not in normalized:
            normalized.append(typed_protocol)
    return tuple(normalized) or DEFAULT_OPENAI_REALTIME_PROTOCOLS


def _realtime_adapter_audio_kwargs(
    *,
    voice: str | None,
    input_format: LLMAudioFormat,
    output_format: LLMAudioFormat,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if voice is not None:
        kwargs["voice"] = voice
    if input_format != DEFAULT_OPENAI_REALTIME_INPUT_FORMAT:
        kwargs["input_format"] = input_format
    if output_format != DEFAULT_OPENAI_REALTIME_OUTPUT_FORMAT:
        kwargs["output_format"] = output_format
    return kwargs


def _agent_audio_format_from_llm(
    format: LLMAudioFormat | None,
) -> AgentAudioFormat | None:
    if format is None:
        return None
    return AgentAudioFormat(
        type=format.type,
        sample_rate=format.sample_rate,
        bitrate=format.bitrate,
    )


def _resolve_openai_realtime_model(*, model: str) -> str | None:
    normalized = model.strip()
    if normalized == "":
        return None

    normalized_lower = normalized.lower()
    if normalized_lower in _OPENAI_REALTIME_MODEL_ALIASES:
        return os.getenv("OPENAI_REALTIME_MODEL") or _DEFAULT_OPENAI_REALTIME_MODEL

    if normalized_lower.startswith(("gpt-realtime", "gpt-4o-realtime")):
        return normalized

    return None


def _normalize_model_options(model: str | list[str]) -> list[str]:
    if isinstance(model, str):
        models = [model]
    else:
        models = model

    normalized = [item.strip() for item in models if item.strip() != ""]
    if len(normalized) == 0:
        return ["gpt-5.5"]
    return normalized


def _provider_name_for_model(model: str) -> str:
    if _resolve_openai_realtime_model(model=model) is not None:
        return "openai-realtime"
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


def _provider_model_display_name(*, provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _active_model_from_models_response(
    response: ModelsResponse,
    *,
    thread_id: str,
) -> AgentModelChanged | None:
    for provider in response.providers:
        for model in provider.models:
            if not model.active:
                continue
            return AgentModelChanged(
                type=AGENT_EVENT_MODEL_CHANGED,
                thread_id=thread_id,
                source_message_id=response.source_message_id,
                provider=provider.name,
                model=model.name,
                voice=model.default_output_voice,
                input_format=model.input_format,
                output_format=model.output_format,
                turn_detection=model.turn_detection,
                realtime_protocols=model.realtime_protocols,
                output_modalities=_default_output_modalities_for_model_info(model),
            )
    return None


def _default_output_modalities_for_model_info(
    model: AgentModelInfo,
) -> list[OutputModality]:
    return [model.modalities[0]] if len(model.modalities) > 0 else ["text"]


def _selected_model_from_models_response(
    *,
    response: ModelsResponse,
    thread_id: str,
    provider: str | None,
    model: str,
) -> AgentModelChanged | None:
    for provider_info in response.providers:
        if provider is not None and provider_info.name != provider:
            continue
        selected_model = next(
            (
                model_info
                for model_info in provider_info.models
                if model_info.name == model
            ),
            None,
        )
        if selected_model is None:
            continue
        return AgentModelChanged(
            type=AGENT_EVENT_MODEL_CHANGED,
            thread_id=thread_id,
            provider=provider_info.name,
            model=model,
            voice=selected_model.default_output_voice,
            input_format=selected_model.input_format,
            output_format=selected_model.output_format,
            turn_detection=selected_model.turn_detection,
            realtime_protocols=selected_model.realtime_protocols,
            output_modalities=_default_output_modalities_for_model_info(selected_model),
        )
    return None


def _selected_default_model_for_provider(
    *,
    response: ModelsResponse,
    thread_id: str,
    provider_name: str,
) -> AgentModelChanged | None:
    for provider in response.providers:
        if provider.name != provider_name:
            continue
        model_name = provider.default_model
        if model_name is None and len(provider.models) > 0:
            model_name = provider.models[0].name
        if model_name is None:
            return None
        selected_model = next(
            (
                model_info
                for model_info in provider.models
                if model_info.name == model_name
            ),
            None,
        )
        if selected_model is None:
            return None
        return AgentModelChanged(
            type=AGENT_EVENT_MODEL_CHANGED,
            thread_id=thread_id,
            provider=provider.name,
            model=model_name,
            voice=selected_model.default_output_voice,
            input_format=selected_model.input_format,
            output_format=selected_model.output_format,
            turn_detection=selected_model.turn_detection,
            realtime_protocols=selected_model.realtime_protocols,
            output_modalities=_default_output_modalities_for_model_info(selected_model),
        )
    return None


def _current_model_label(
    *,
    current_model: AgentModelChanged | None,
    fallback: str,
) -> str:
    if current_model is None:
        return fallback
    return _provider_model_display_name(
        provider=current_model.provider,
        model=current_model.model,
    )


def _model_info_for_current_selection(
    *,
    response: ModelsResponse | None,
    current_model: AgentModelChanged | None,
) -> AgentModelInfo | None:
    if response is None or current_model is None:
        return None
    for provider in response.providers:
        if provider.name != current_model.provider:
            continue
        for model in provider.models:
            if model.name == current_model.model:
                return model
    return None


def _agent_model_changed_for_model(
    *,
    model: str,
    thread_id: str,
    voice: str | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
) -> AgentModelChanged:
    provider = _provider_name_for_model(model)
    input_format, output_format = _configured_realtime_audio_formats(
        provider_name=provider,
        input_audio_format=input_audio_format,
        input_audio_sample_rate=input_audio_sample_rate,
        input_audio_bitrate=input_audio_bitrate,
        output_audio_format=output_audio_format,
        output_audio_sample_rate=output_audio_sample_rate,
        output_audio_bitrate=output_audio_bitrate,
    )
    selected_voice = None
    selected_output_modalities: list[OutputModality] = ["text"]
    if provider == "openai-realtime":
        selected_voice = voice or DEFAULT_OPENAI_REALTIME_VOICE
        selected_output_modalities = [
            _normalize_output_modalities(output_modalities)[0]
        ]
    return AgentModelChanged(
        type=AGENT_EVENT_MODEL_CHANGED,
        thread_id=thread_id,
        provider=provider,
        model=model,
        output_modalities=selected_output_modalities,
        voice=selected_voice,
        input_format=input_format,
        output_format=output_format,
        turn_detection=turn_detection if provider == "openai-realtime" else None,
        realtime_protocols=(
            list(realtime_protocols) if provider == "openai-realtime" else []
        ),
    )


def _configured_realtime_audio_formats(
    *,
    provider_name: str,
    input_audio_format: str,
    input_audio_sample_rate: int | None,
    input_audio_bitrate: int | None,
    output_audio_format: str,
    output_audio_sample_rate: int | None,
    output_audio_bitrate: int | None,
) -> tuple[AgentAudioFormat | None, AgentAudioFormat | None]:
    if provider_name != "openai-realtime":
        return None, None
    input_format = _audio_format_option(
        audio_format=input_audio_format,
        sample_rate=input_audio_sample_rate,
        bitrate=input_audio_bitrate,
    )
    output_format = _audio_format_option(
        audio_format=output_audio_format,
        sample_rate=output_audio_sample_rate,
        bitrate=output_audio_bitrate,
    )
    return (
        _agent_audio_format_from_llm(
            input_format or DEFAULT_OPENAI_REALTIME_INPUT_FORMAT
        ),
        _agent_audio_format_from_llm(
            output_format or DEFAULT_OPENAI_REALTIME_OUTPUT_FORMAT
        ),
    )


def _configured_models_response(
    *,
    models: list[str],
    current_model: AgentModelChanged | None,
    voice: str | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
) -> ModelsResponse:
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    grouped: dict[str, list[str]] = {}
    for model in models:
        grouped.setdefault(_provider_name_for_model(model), []).append(model)
    providers: list[AgentProviderInfo] = []
    for provider_name, provider_models in grouped.items():
        providers.append(
            AgentProviderInfo(
                name=provider_name,
                friendly_name=provider_name,
                default_model=provider_models[0],
                models=[
                    _agent_model_info_for_configured_model(
                        provider_name=provider_name,
                        model=model,
                        current_model=current_model,
                        voice=voice,
                        turn_detection=turn_detection,
                        realtime_protocols=realtime_protocols,
                        output_modalities=selected_output_modalities,
                        input_audio_format=input_audio_format,
                        input_audio_sample_rate=input_audio_sample_rate,
                        input_audio_bitrate=input_audio_bitrate,
                        output_audio_format=output_audio_format,
                        output_audio_sample_rate=output_audio_sample_rate,
                        output_audio_bitrate=output_audio_bitrate,
                    )
                    for model in provider_models
                ],
            )
        )
    return ModelsResponse(
        type=AGENT_MESSAGE_MODELS_RESPONSE,
        source_message_id="configured-models",
        providers=providers,
    )


def _agent_model_info_for_configured_model(
    *,
    provider_name: str,
    model: str,
    current_model: AgentModelChanged | None,
    voice: str | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
) -> AgentModelInfo:
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    input_format, output_format = _configured_realtime_audio_formats(
        provider_name=provider_name,
        input_audio_format=input_audio_format,
        input_audio_sample_rate=input_audio_sample_rate,
        input_audio_bitrate=input_audio_bitrate,
        output_audio_format=output_audio_format,
        output_audio_sample_rate=output_audio_sample_rate,
        output_audio_bitrate=output_audio_bitrate,
    )
    return AgentModelInfo(
        name=model,
        friendly_name=model,
        modalities=(
            list(selected_output_modalities)
            if provider_name == "openai-realtime"
            else ["text"]
        ),
        active=(
            current_model is not None
            and current_model.provider == provider_name
            and current_model.model == model
        ),
        available_voices=(
            list(OPENAI_REALTIME_VOICES) if provider_name == "openai-realtime" else []
        ),
        default_output_voice=(
            voice or DEFAULT_OPENAI_REALTIME_VOICE
            if provider_name == "openai-realtime"
            else None
        ),
        input_format=input_format,
        output_format=output_format,
        turn_detection=turn_detection if provider_name == "openai-realtime" else None,
        realtime_protocols=(
            list(realtime_protocols) if provider_name == "openai-realtime" else []
        ),
    )


def _model_command_options(
    *,
    response: ModelsResponse | None,
    current_model: AgentModelChanged | None,
) -> tuple["AskCommandOption", ...]:
    if response is None:
        return ()
    options: list[AskCommandOption] = []
    for provider in response.providers:
        for model in provider.models:
            is_active = (
                current_model is not None
                and current_model.provider == provider.name
                and current_model.model == model.name
            ) or (current_model is None and model.active)
            command_value = _provider_model_display_name(
                provider=provider.name,
                model=model.name,
            )
            options.append(
                AskCommandOption(
                    command=f"/model {command_value}",
                    label=command_value,
                    description=model.description,
                    active=is_active,
                )
            )
    return tuple(sorted(options, key=lambda option: (not option.active, option.label)))


def _voice_command_options(
    *,
    response: ModelsResponse | None,
    current_model: AgentModelChanged | None,
) -> tuple["AskCommandOption", ...]:
    model_info = _model_info_for_current_selection(
        response=response,
        current_model=current_model,
    )
    if model_info is None or len(model_info.available_voices) == 0:
        return ()
    current_voice = current_model.voice if current_model is not None else None
    if current_voice is None:
        current_voice = model_info.default_output_voice
    return tuple(
        AskCommandOption(
            command=f"/voice {voice}",
            label=voice,
            description="Output voice",
            active=voice == current_voice,
        )
        for voice in model_info.available_voices
    )


def _process_command_options(
    prompt: str,
    *,
    response: ModelsResponse | None,
    current_model: AgentModelChanged | None,
    current_output_modalities: tuple[Literal["text", "audio"], ...] = ("text",),
) -> tuple["AskCommandOption", ...]:
    stripped = prompt.strip()
    if stripped.startswith("/output"):
        model_info = _model_info_for_current_selection(
            response=response,
            current_model=current_model,
        )
        supported = (
            tuple(model_info.modalities) if model_info is not None else ("text",)
        )
        return tuple(
            AskCommandOption(
                command=f"/output {output}",
                label=output,
                description=(
                    "Voice responses" if output == "audio" else "Text responses"
                ),
                active=output in current_output_modalities,
            )
            for output in ("text", "audio")
            if output in supported
        )

    if stripped.startswith("/voice"):
        parts = stripped.split(maxsplit=1)
        filter_text = parts[1].lower() if len(parts) > 1 else ""
        options = _voice_command_options(
            response=response,
            current_model=current_model,
        )
        if filter_text == "":
            return options
        return tuple(
            option for option in options if filter_text in option.label.lower()
        )

    if not stripped.startswith("/model"):
        return ()
    parts = stripped.split(maxsplit=1)
    if len(parts) > 1 and "/" not in parts[1]:
        filter_text = parts[1].lower()
    else:
        filter_text = ""
    options = _model_command_options(
        response=response,
        current_model=current_model,
    )
    if filter_text == "":
        return options
    return tuple(
        option
        for option in options
        if filter_text in option.label.lower()
        or (
            option.description is not None and filter_text in option.description.lower()
        )
    )


def _format_provider_list(
    *,
    providers: list[AgentProviderInfo],
    current_model: AgentModelChanged | None,
) -> str:
    lines = ["Providers:"]
    current_provider = current_model.provider if current_model is not None else None
    if current_provider is None:
        current_provider = next(
            (
                provider.name
                for provider in providers
                if any(model.active for model in provider.models)
            ),
            None,
        )
    for provider in providers:
        marker = "*" if provider.name == current_provider else " "
        lines.append(f"{marker} {provider.name} - {provider.friendly_name}")
    return "\n".join(lines)


def _format_model_list(
    *,
    providers: list[AgentProviderInfo],
    current_model: AgentModelChanged | None,
) -> str:
    lines = ["Models:"]
    current_provider = current_model.provider if current_model is not None else None
    current_model_name = current_model.model if current_model is not None else None
    for provider in providers:
        for model in provider.models:
            marker = (
                "*"
                if (
                    provider.name == current_provider
                    and model.name == current_model_name
                )
                or (current_model is None and model.active)
                else " "
            )
            lines.append(
                f"{marker} {_provider_model_display_name(provider=provider.name, model=model.name)}"
            )
    return "\n".join(lines)


def _supports_openai_responses_builtin_tools(*, model: str) -> bool:
    return _provider_name_for_model(model) == "openai"


def _supports_anthropic_builtin_tools(*, model: str) -> bool:
    return _provider_name_for_model(model) == "anthropic"


def _has_openai_responses_provider(
    *, models: list[str], llm_participant: str | None
) -> bool:
    if llm_participant is not None:
        return False
    return any(_provider_name_for_model(model) == "openai" for model in models)


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
        if threading_mode == "none":
            annotations["meshagent.chatbot.thread-path"] = (
                f"{normalized_thread_dir}/main.thread"
            )
        else:
            annotations["meshagent.chatbot.thread-dir"] = normalized_thread_dir
            annotations["meshagent.chatbot.thread-list"] = (
                f"{normalized_thread_dir}/index.threadl"
            )

    return annotations


def _process_agent_annotations(
    *,
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
    thread_storage: ThreadStorageBackend,
    channel: list[str],
) -> dict[str, str]:
    if not _has_chat_channel(channels=channel):
        return {}
    annotations = _chatbot_agent_annotations(
        threading_mode=threading_mode,
        thread_dir=thread_dir,
    )
    if thread_storage == "dataset":
        normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
        if normalized_thread_dir is not None:
            storage_class = _thread_storage_class_for_backend(thread_storage)
            if threading_mode == "none":
                annotations["meshagent.chatbot.thread-path"] = (
                    _process_thread_path_for_dir(
                        thread_dir=normalized_thread_dir,
                        thread_storage=thread_storage,
                    )
                )
            else:
                thread_dir_url = (
                    normalized_thread_dir
                    if normalized_thread_dir.startswith("dataset://")
                    else _dataset_thread_url_for_path(path=normalized_thread_dir)
                )
                annotations["meshagent.chatbot.thread-dir"] = thread_dir_url
                if storage_class is not None:
                    annotations["meshagent.chatbot.thread-list"] = (
                        storage_class.thread_list_path_for_dir(
                            thread_dir=thread_dir_url
                        )
                    )
    elif thread_storage == "none":
        normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
        if normalized_thread_dir is not None:
            if threading_mode == "none":
                annotations["meshagent.chatbot.thread-path"] = _thread_url_for_path(
                    scheme="tmp",
                    path=f"{normalized_thread_dir}/main",
                )
            else:
                annotations["meshagent.chatbot.thread-dir"] = _thread_url_for_path(
                    scheme="tmp",
                    path=normalized_thread_dir,
                )
    return annotations


def _agent_annotations_for_runtime(
    *,
    runtime: Literal["chatbot", "process"],
    threading_mode: ThreadingMode,
    thread_dir: Optional[str],
    thread_storage: ThreadStorageBackend,
    channel: list[str],
) -> dict[str, str]:
    if runtime == "process":
        return _process_agent_annotations(
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_storage=thread_storage,
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
    model: str | list[str],
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
    thread_storage: ThreadStorageBackend,
    context_management: ContextManagementMode,
    compaction_threshold: Optional[int],
    max_output_tokens: Optional[int],
    reasoning_effort: Optional[str],
    transcription_model: str | None,
    working_dir: Optional[str],
    dataset_namespace: Optional[list[str]],
    skill_dirs: Optional[list[str]],
    shell_image: Optional[str],
    delegate_shell_token: Optional[bool],
    shell_copy_env: Optional[list[str]],
    shell_set_env: Optional[list[str]],
    log_llm_requests: Optional[bool],
    channels: Optional[list[str]],
    websocket_auth: WebSocketAuthMode = "jwt",
    voice: str | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    starting_url: Optional[str] = None,
    allow_goto_url: bool = False,
    room_rules_path: Optional[list[str]] = None,
    verbose_dataset: bool = False,
    save_audio_input: bool = False,
    preamble_rule: bool = True,
):
    builder = _builder_for_runtime(runtime)
    selected_models = _normalize_model_options(model)
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    builder_model: str | list[str]
    if runtime == "process":
        builder_model = selected_models
    else:
        builder_model = selected_models[0]
    builder_kwargs: dict[str, Any] = {
        "computer_use": False,
        "require_computer_use": normalized_tool_options["require_computer_use"],
        "api_key": api_key,
        "starting_url": starting_url,
        "allow_goto_url": allow_goto_url,
        "model": builder_model,
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
        "transcription_model": transcription_model,
        "voice": voice,
        "turn_detection": turn_detection,
        "realtime_protocols": realtime_protocols,
        "output_modalities": selected_output_modalities,
        "input_audio_format": input_audio_format,
        "input_audio_sample_rate": input_audio_sample_rate,
        "input_audio_bitrate": input_audio_bitrate,
        "output_audio_format": output_audio_format,
        "output_audio_sample_rate": output_audio_sample_rate,
        "output_audio_bitrate": output_audio_bitrate,
    }
    if runtime == "process":
        builder_kwargs["thread_storage"] = thread_storage
        builder_kwargs["context_management"] = context_management
        builder_kwargs["compaction_threshold"] = compaction_threshold
        builder_kwargs["max_output_tokens"] = max_output_tokens
        builder_kwargs["reasoning_effort"] = reasoning_effort
        builder_kwargs["verbose_dataset"] = verbose_dataset
        builder_kwargs["save_audio_input"] = save_audio_input
        builder_kwargs["preamble_rule"] = preamble_rule
        builder_kwargs["websocket_auth"] = websocket_auth
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
    transcription_model: str | None = DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    voice: str | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    preamble_rule: bool = True,
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

    realtime_model = _resolve_openai_realtime_model(model=model)
    is_openai_realtime_model = realtime_model is not None
    is_claude_model = model.startswith("claude-")
    supports_openai_tools = (
        llm_participant is None and not is_claude_model and not is_openai_realtime_model
    )
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
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    default_realtime_output_modalities = _default_realtime_output_modalities(
        selected_output_modalities
    )
    realtime_input_format = _audio_format_option(
        audio_format=input_audio_format,
        sample_rate=input_audio_sample_rate,
        bitrate=input_audio_bitrate,
    )
    realtime_output_format = _audio_format_option(
        audio_format=output_audio_format,
        sample_rate=output_audio_sample_rate,
        bitrate=output_audio_bitrate,
    )
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
        elif realtime_model is not None:
            llm_adapter = OpenAIRealtimeAdapter(
                model=realtime_model,
                api_key=api_key,
                log_requests=log_llm_requests,
                session_options=_openai_realtime_session_options(
                    output_modalities=default_realtime_output_modalities
                ),
                response_options=_openai_realtime_response_options(
                    output_modalities=default_realtime_output_modalities
                ),
                supported_output_modalities=selected_output_modalities,
                transcription_model=transcription_model,
                turn_detection=turn_detection,
                realtime_protocols=realtime_protocols,
                **_realtime_adapter_audio_kwargs(
                    voice=voice,
                    input_format=realtime_input_format,
                    output_format=realtime_output_format,
                ),
            )
            if resolved_decision_model is None:
                resolved_decision_model = _DEFAULT_OPENAI_REALTIME_DECISION_MODEL
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
    model: str | list[str],
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
    thread_storage: ThreadStorageBackend = "meshdocument",
    context_management: ContextManagementMode = "auto",
    compaction_threshold: Optional[int] = None,
    max_output_tokens: Optional[int] = 32000,
    reasoning_effort: Optional[str] = None,
    skill_dirs: Optional[list[str]] = None,
    threading_mode: ThreadingMode = "default-new",
    shell_image: Optional[str] = None,
    log_llm_requests: Optional[bool] = None,
    delegate_shell_token: Optional[bool] = None,
    shell_copy_env: Optional[list[str]] = None,
    shell_set_env: Optional[list[str]] = None,
    channels: Optional[list[str]] = None,
    websocket_auth: WebSocketAuthMode = "jwt",
    transcription_model: str | None = DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL,
    voice: str | None = None,
    turn_detection: Literal[
        "none", "automatic"
    ] = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocols: tuple[
        Literal["websocket", "webrtc"], ...
    ] = DEFAULT_OPENAI_REALTIME_PROTOCOLS,
    output_modalities: Iterable[str] | None = None,
    input_audio_format: str = "audio/pcm",
    input_audio_sample_rate: int | None = 24000,
    input_audio_bitrate: int | None = None,
    output_audio_format: str = "audio/pcm",
    output_audio_sample_rate: int | None = 24000,
    output_audio_bitrate: int | None = None,
    verbose_dataset: bool = False,
    save_audio_input: bool = False,
    preamble_rule: bool = True,
):
    from meshagent.agents import (
        AgentMessageThreadStatusPublisher,
        DatasetThreadStorage,
        MeshDocumentThreadStorage,
        MessagingChatChannel,
        MailChannel,
        QueueChannel,
        SingleRoomAgent,
        ToolkitChannel,
        WebSocketChatChannel,
    )
    from meshagent.agents.messages import TurnStart, TurnSteer
    from meshagent.agents.process import (
        AgentSupervisor,
        LLMAgentProcess,
        Message,
        agent_provider_info,
    )
    from meshagent.tools import RoomToolContext, Toolkit
    from meshagent.tools.hosting import _RemoteToolkitWrapper, _start_hosted_toolkit

    requirements = []
    toolkits = []

    if storage_tool_local_paths is None:
        storage_tool_local_paths = []
    if storage_tool_room_paths is None:
        storage_tool_room_paths = []

    thread_url_scheme = (
        "dataset"
        if thread_storage == "dataset"
        else "tmp"
        if thread_storage == "none"
        else None
    )
    thread_path_extension = "" if thread_storage in ("dataset", "none") else ".thread"
    channel_thread_url_kwargs = (
        {
            "thread_url_scheme": thread_url_scheme,
            "thread_path_extension": thread_path_extension,
        }
        if thread_url_scheme is not None
        else {}
    )

    def create_thread_storage(*, room: RoomClient, thread_id: str):
        if thread_storage == "none":
            return None
        if thread_storage == "dataset":
            if not thread_id.startswith("dataset://"):
                raise ValueError(
                    "dataset thread storage requires a dataset:// thread id"
                )
            return DatasetThreadStorage(
                room=room,
                path=thread_id,
                persist_deltas=verbose_dataset,
                persist_audio_input=save_audio_input,
            )
        return MeshDocumentThreadStorage(
            room=room,
            path=thread_id,
        )

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

    selected_models = _normalize_model_options(model)
    realtime_input_format = _audio_format_option(
        audio_format=input_audio_format,
        sample_rate=input_audio_sample_rate,
        bitrate=input_audio_bitrate,
    )
    realtime_output_format = _audio_format_option(
        audio_format=output_audio_format,
        sample_rate=output_audio_sample_rate,
        bitrate=output_audio_bitrate,
    )
    selected_output_modalities = _normalize_output_modalities(output_modalities)
    default_realtime_output_modalities = _default_realtime_output_modalities(
        selected_output_modalities
    )
    supports_openai_responses_tools = _has_openai_responses_provider(
        models=selected_models,
        llm_participant=llm_participant,
    )
    if reasoning_effort is not None and not supports_openai_responses_tools:
        print(
            "[red]--reasoning-effort is only supported by OpenAI Responses models[/red]"
        )
        raise typer.Exit(1)
    base_shell_env = _copy_shell_env_vars(copy_env=shell_copy_env)
    base_shell_env.update(_set_shell_env_vars(set_env=shell_set_env))
    resolved_shell_image = resolve_shell_image(shell_image)
    if not supports_openai_responses_tools:
        if require_image_generation:
            print(
                "[red]image generation tool is only supported by OpenAI Responses models[/red]"
            )
            raise typer.Exit(1)
        if require_apply_patch:
            print(
                "[red]apply patch tool is only supported by OpenAI Responses models[/red]"
            )
            raise typer.Exit(1)
        if computer_use or require_computer_use:
            print(
                "[red]computer use tool is currently only supported by OpenAI Responses models[/red]"
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

    llm_providers: list[LLMProvider] = []
    if llm_participant:
        llm_providers.append(
            LLMProvider(
                name="remote",
                adapter=MessageStreamLLMAdapter(
                    participant_name=llm_participant,
                ),
            )
        )
    else:
        openai_models: list[str] = []
        realtime_models: list[str] = []
        anthropic_models: list[str] = []
        provider_order: list[str] = []
        for selected_model in selected_models:
            provider_name = _provider_name_for_model(selected_model)
            if provider_name not in provider_order:
                provider_order.append(provider_name)
            if provider_name == "openai-realtime":
                realtime_model = _resolve_openai_realtime_model(model=selected_model)
                if realtime_model is not None and realtime_model not in realtime_models:
                    realtime_models.append(realtime_model)
            elif provider_name == "anthropic":
                if selected_model not in anthropic_models:
                    anthropic_models.append(selected_model)
            elif selected_model not in openai_models:
                openai_models.append(selected_model)

        if computer_use or require_computer_use:
            if not openai_models:
                print(
                    "[red]computer use tool is currently only supported by OpenAI Responses models[/red]"
                )
                raise typer.Exit(1)
            default_openai_model = openai_models[0]
            llm_providers.append(
                LLMProvider(
                    name="openai",
                    adapter=OpenAIResponsesAdapter(
                        model=default_openai_model,
                        api_key=api_key,
                        response_options={
                            "reasoning": {"summary": "concise"},
                        },
                        log_requests=log_llm_requests,
                        context_management=context_management,
                        compaction_threshold=compaction_threshold,
                        max_output_tokens=max_output_tokens,
                        reasoning_effort=reasoning_effort,
                        allowed_models=openai_models,
                    ),
                )
            )
        else:
            providers_by_name: dict[str, LLMProvider] = {}
            if openai_models:
                providers_by_name["openai"] = LLMProvider(
                    name="openai",
                    adapter=OpenAIResponsesAdapter(
                        model=openai_models[0],
                        api_key=api_key,
                        log_requests=log_llm_requests,
                        context_management=context_management,
                        compaction_threshold=compaction_threshold,
                        max_output_tokens=max_output_tokens,
                        reasoning_effort=reasoning_effort,
                        allowed_models=openai_models,
                    ),
                )
            if realtime_models:
                providers_by_name["openai-realtime"] = LLMProvider(
                    name="openai-realtime",
                    adapter=OpenAIRealtimeAdapter(
                        model=realtime_models[0],
                        api_key=api_key,
                        log_requests=log_llm_requests,
                        session_options=_openai_realtime_session_options(
                            output_modalities=default_realtime_output_modalities
                        ),
                        response_options=_openai_realtime_response_options(
                            output_modalities=default_realtime_output_modalities
                        ),
                        supported_output_modalities=selected_output_modalities,
                        allowed_models=realtime_models,
                        transcription_model=transcription_model,
                        turn_detection=turn_detection,
                        realtime_protocols=realtime_protocols,
                        **_realtime_adapter_audio_kwargs(
                            voice=voice,
                            input_format=realtime_input_format,
                            output_format=realtime_output_format,
                        ),
                    ),
                )
            if anthropic_models:
                providers_by_name["anthropic"] = LLMProvider(
                    name="anthropic",
                    adapter=AnthropicOpenAIResponsesStreamAdapter(
                        model=anthropic_models[0],
                        api_key=api_key,
                        log_requests=log_llm_requests,
                        allowed_models=anthropic_models,
                    ),
                )
            for provider_name in provider_order:
                provider = providers_by_name.get(provider_name)
                if provider is not None:
                    llm_providers.append(provider)

    default_provider = llm_providers[0]

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
            self._websocket_channels: list[WebSocketChatChannel] = []
            self._websocket_channel_servers: list[_WebSocketChannelServer] = []
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
            channels.extend(self._websocket_channels)
            for channel in channels:
                if channel.state != "started":
                    continue
                exposed_toolkits.extend(channel.get_exposed_toolkits())
            return exposed_toolkits

        async def start(self, *, room: RoomClient) -> None:
            if self._room is not None:
                raise RoomException("agent is already started")

            self._room = room
            if require_image_generation:
                for provider in llm_providers:
                    if isinstance(provider.adapter, OpenAIResponsesAdapter):
                        provider.adapter.set_images_dataset(
                            ImagesDataset(room.datasets)
                        )
            if require_mcp:
                await room.local_participant.set_attribute("supports_mcp", True)
            if _has_chat_channel(channels=resolved_channels):
                self._chat_channel = MessagingChatChannel(
                    room=room,
                    threading_mode=self._resolved_threading_mode,
                    thread_dir=thread_dir,
                    **channel_thread_url_kwargs,
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
                        **channel_thread_url_kwargs,
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
                        **channel_thread_url_kwargs,
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
            self._websocket_channels = []
            for channel_spec in resolved_channels:
                if not _is_websocket_channel(channel_spec):
                    continue
                self._websocket_channels.append(
                    WebSocketChatChannel(
                        room=room,
                        authorize=lambda request: _authorize_process_websocket_request(
                            request=request,
                            room=room,
                            websocket_auth=websocket_auth,
                        ),
                        threading_mode=self._resolved_threading_mode,
                        thread_dir=thread_dir,
                        **channel_thread_url_kwargs,
                        llm_adapter=channel_llm_adapter,
                    )
                )
            started_remote_toolkits: list[_RemoteToolkitWrapper] = []
            started_websocket_servers: list[_WebSocketChannelServer] = []
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
                for websocket_channel in self._websocket_channels:
                    supervisor.add_channel(websocket_channel)
                await supervisor.start()
                self._supervisor = supervisor

                for channel_spec, websocket_channel in zip(
                    [
                        channel_spec
                        for channel_spec in resolved_channels
                        if _is_websocket_channel(channel_spec)
                    ],
                    self._websocket_channels,
                ):
                    websocket_config = _parse_websocket_channel(channel=channel_spec)
                    server = await _start_process_websocket_channel_server(
                        config=websocket_config,
                        channel=websocket_channel,
                    )
                    started_websocket_servers.append(server)
                    print(
                        "[bold green]WebSocket channel listening on "
                        f"ws://{websocket_config.host}:{websocket_config.port}[/bold green]",
                        flush=True,
                    )
                self._websocket_channel_servers = started_websocket_servers

                self._exposed_toolkits = await self.get_exposed_toolkits()
                for toolkit in self._exposed_toolkits:
                    hosted_toolkit = await _start_hosted_toolkit(
                        room=room,
                        toolkit=toolkit,
                    )
                    started_remote_toolkits.append(hosted_toolkit)
                self._hosted_exposed_toolkits = started_remote_toolkits
            except Exception:
                for server in reversed(started_websocket_servers):
                    await server.stop()
                self._websocket_channel_servers = []
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
                self._websocket_channels = []
                self._advanced_shell_toolkit = None
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
            self._mail_channels = []
            self._queue_channels = []
            self._toolkit_channels = []
            self._websocket_channels = []
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

            context = default_provider.adapter.create_session()
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

            if preamble_rule and len(rules) == 0:
                rules.append(DEFAULT_PREAMBLE_RULE)

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
                if not _supports_openai_responses_builtin_tools(model=model):
                    raise ValueError(
                        "image generation tool is only supported by OpenAI Responses models"
                    )
                add_tool(
                    toolkit_name="image_generation",
                    tool=ImageGenerationTool(
                        model=require_image_generation,
                        partial_images=3,
                    ),
                )

            if require_apply_patch:
                if not _supports_openai_responses_builtin_tools(model=model):
                    raise ValueError(
                        "apply patch tool is only supported by OpenAI Responses models"
                    )
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
                if _supports_anthropic_builtin_tools(model=model):
                    add_toolkit(AnthropicMessagesMCPToolkit())
                elif _supports_openai_responses_builtin_tools(model=model):
                    add_toolkit(OpenAIResponsesMCPToolkit())
                else:
                    raise ValueError(
                        "MCP tools are only supported by OpenAI Responses and Anthropic models"
                    )

            if require_web_search:
                if _supports_anthropic_builtin_tools(model=model):
                    add_tool(
                        toolkit_name="web_search",
                        tool=AnthropicWebSearchTool(),
                    )
                elif _supports_openai_responses_builtin_tools(model=model):
                    add_tool(
                        toolkit_name="web_search",
                        tool=WebSearchTool(),
                    )
                else:
                    raise ValueError(
                        "web search is only supported by OpenAI Responses and Anthropic models"
                    )

            if require_web_fetch:
                if _supports_anthropic_builtin_tools(model=model):
                    add_tool(
                        toolkit_name="web_fetch",
                        tool=AnthropicWebFetchTool(),
                    )
                elif _supports_openai_responses_builtin_tools(model=model):
                    add_tool(toolkit_name="web_fetch", tool=WebFetchTool())
                else:
                    raise ValueError(
                        "web fetch is only supported by OpenAI Responses and Anthropic models"
                    )

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
            if process.supervisor is not None:
                for channel in process.supervisor.channels:
                    if channel.state != "started":
                        continue
                    turn_id = turns[-1].turn_id if len(turns) > 0 else None
                    if process.thread_id is not None:
                        combined_toolkits.extend(
                            channel.get_turn_toolkits(
                                thread_id=process.thread_id,
                                turn_id=turn_id,
                            )
                        )
            if process.thread_storage is not None:
                combined_toolkits.append(process.thread_storage.make_toolkit())
            return combined_toolkits

    class _ProcessSupervisor(AgentSupervisor):
        def __init__(self, *, agent: CustomProcessAgent) -> None:
            super().__init__()
            self._agent = agent
            self._local_event_queues: list[asyncio.Queue[Message]] = []
            self._thread_delete_watch_task: asyncio.Task[None] | None = None

        async def on_start(self) -> None:
            await super().on_start()
            storage_class = _thread_storage_class_for_backend(thread_storage)
            if storage_class is None or thread_dir is None:
                return
            self._thread_delete_watch_task = asyncio.create_task(
                self._watch_thread_deletes(storage_class=storage_class)
            )

        async def on_stop(self) -> None:
            watch_task = self._thread_delete_watch_task
            self._thread_delete_watch_task = None
            if watch_task is not None:
                watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch_task
            await super().on_stop()

        async def _watch_thread_deletes(self, *, storage_class) -> None:
            if thread_dir is None:
                return
            try:
                async for event in storage_class.watch_threads(
                    room=self._agent.room,
                    thread_dir=thread_dir,
                ):
                    if event.type != "deleted":
                        continue
                    async with self._route_lock:
                        await self._stop_thread_process(thread_id=event.path)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug("thread delete watch stopped: %s", ex)

        async def on_thread_deleted(
            self,
            *,
            delete_thread: DeleteThread,
            sender: Participant | None,
        ) -> None:
            del sender
            storage_class = _thread_storage_class_for_backend(thread_storage)
            normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
            if storage_class is None or normalized_thread_dir is None:
                return
            await storage_class.delete_thread(
                room=self._agent.room,
                thread_dir=normalized_thread_dir,
                path=delete_thread.thread_id,
            )

        async def on_thread_renamed(
            self,
            *,
            rename_thread: RenameThread,
            sender: Participant | None,
        ) -> ThreadListEntry | None:
            del sender
            storage_class = _thread_storage_class_for_backend(thread_storage)
            normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
            if storage_class is None or normalized_thread_dir is None:
                return
            name = " ".join(rename_thread.name.split())
            if name == "":
                return
            await storage_class.rename_thread(
                room=self._agent.room,
                thread_dir=normalized_thread_dir,
                path=rename_thread.thread_id,
                name=name,
            )
            return ThreadListEntry(
                path=rename_thread.thread_id,
                name=name,
                created_at="",
                modified_at=datetime.now(timezone.utc).isoformat(),
            )

        async def list_threads(
            self,
            *,
            list_threads: ListThreads,
            sender: Participant | None,
        ) -> ThreadListPage:
            del sender
            storage_class = _thread_storage_class_for_backend(thread_storage)
            normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
            if storage_class is None or normalized_thread_dir is None:
                return ThreadListPage(
                    threads=[],
                    total=0,
                    offset=list_threads.offset,
                    limit=list_threads.limit,
                )
            return await storage_class.list_threads(
                room=self._agent.room,
                thread_dir=normalized_thread_dir,
                limit=list_threads.limit,
                offset=list_threads.offset,
            )

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

        def send(self, message: Message) -> None:
            if message.source is not None:
                self._send_to_local_event_queues(message)
            super().send(message)

        def _emit_thread_started(
            self,
            *,
            start_thread: StartThread,
            sender: Participant | None,
            thread_id: str,
            realtime_connection: AgentRealtimeConnectionInfo | None = None,
        ) -> None:
            thread_started = ThreadStarted(
                type=AGENT_EVENT_THREAD_STARTED,
                source_message_id=start_thread.message_id,
                thread_id=thread_id,
                realtime_connection=realtime_connection,
            )
            self._send_to_local_event_queues(
                Message(data=thread_started, sender=sender)
            )
            self._send_to_channels(Message(data=thread_started, sender=sender))

        async def create_thread_id(
            self,
            *,
            start_thread: StartThread,
            sender: Participant | None,
        ) -> str:
            del start_thread
            del sender
            normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
            if normalized_thread_dir is not None:
                return _new_process_thread_path_for_dir(
                    thread_dir=normalized_thread_dir,
                    thread_storage=thread_storage,
                )
            generated_path = f"process-run/{uuid.uuid4()}"
            if thread_storage == "dataset":
                return _dataset_thread_url_for_path(path=generated_path)
            if thread_storage == "none":
                return _thread_url_for_path(scheme="tmp", path=generated_path)
            return f"/{generated_path}.thread"

        async def on_thread_started(
            self,
            *,
            thread_id: str,
            start_thread: StartThread,
            sender: Participant | None,
        ) -> ThreadListEntry | None:
            del sender
            storage_class = _thread_storage_class_for_backend(thread_storage)
            normalized_thread_dir = _normalized_thread_dir(thread_dir=thread_dir)
            if storage_class is None or normalized_thread_dir is None:
                return
            await storage_class.upsert_thread(
                room=self._agent.room,
                thread_dir=normalized_thread_dir,
                path=thread_id,
                name=_start_thread_list_name(start_thread),
            )
            now = datetime.now(timezone.utc).isoformat()
            return ThreadListEntry(
                path=thread_id,
                name=_start_thread_list_name(start_thread),
                created_at=now,
                modified_at=now,
            )

        async def on_models_request(self, message: Message) -> None:
            if not isinstance(message.data, ModelsRequest):
                return
            default_model = default_provider.adapter.default_model()
            response_message = Message(
                data=ModelsResponse(
                    type=AGENT_MESSAGE_MODELS_RESPONSE,
                    source_message_id=message.data.message_id,
                    providers=[
                        agent_provider_info(
                            provider=provider,
                            current_provider=default_provider.name,
                            current_model=default_model,
                        )
                        for provider in llm_providers
                    ],
                ),
                sender=message.sender,
            )
            self._send_to_local_event_queues(response_message)
            self._send_to_channels(response_message)

        async def validate_turn_start(self, turn_start: TurnStart) -> AgentError | None:
            provider = default_provider
            if turn_start.provider is not None and turn_start.provider.strip() != "":
                provider = next(
                    (
                        candidate
                        for candidate in llm_providers
                        if candidate.name == turn_start.provider
                    ),
                    None,
                )
                if provider is None:
                    return AgentError(
                        message=f"unknown provider {turn_start.provider!r}",
                        code="unknown_provider",
                    )

            model = turn_start.model
            if model is None or model.strip() == "":
                resolved_model = provider.adapter.default_model()
            else:
                resolved_model = model

            model_info = next(
                (
                    candidate
                    for candidate in provider.adapter.list_models()
                    if candidate.name == resolved_model
                ),
                None,
            )
            if model_info is None:
                names = ", ".join(
                    model_info.name for model_info in provider.adapter.list_models()
                )
                return AgentError(
                    message=(
                        f"unknown model {resolved_model!r} for provider {provider.name!r}; "
                        f"available models: {names}"
                    ),
                    code="unknown_model",
                )
            unsupported_output_modalities = [
                output
                for output in (turn_start.output_modalities or [])
                if output not in model_info.modalities
            ]
            if len(unsupported_output_modalities) > 0:
                unsupported = ", ".join(
                    repr(item) for item in unsupported_output_modalities
                )
                return AgentError(
                    message=(
                        f"model {model_info.name!r} does not support "
                        f"{unsupported} output modalities"
                    ),
                    code="unsupported_modality",
                )
            return None

        async def create_realtime_connection(
            self,
            *,
            thread_id: str,
            start_thread: StartThread,
            sender: Participant | None,
        ) -> AgentRealtimeConnectionInfo | None:
            del thread_id
            del sender
            protocol = start_thread.realtime_protocol
            if protocol is None:
                return None
            provider = default_provider
            if (
                start_thread.provider is not None
                and start_thread.provider.strip() != ""
            ):
                provider = next(
                    (
                        candidate
                        for candidate in llm_providers
                        if candidate.name == start_thread.provider
                    ),
                    None,
                )
                if provider is None:
                    raise RoomException(f"unknown provider {start_thread.provider!r}")
            model = start_thread.model
            resolved_model = (
                provider.adapter.default_model()
                if model is None or model.strip() == ""
                else model
            )
            connection = await provider.adapter.create_realtime_connection(
                protocol=protocol,
                model=resolved_model,
            )
            return AgentRealtimeConnectionInfo(
                protocol=connection.protocol,
                url=connection.url,
                headers=connection.headers,
                web_only_protocol=connection.web_only_protocol,
            )

        def create_thread_process(self, thread_id: str) -> LLMAgentProcess:
            async def _turn_instructions_provider(
                participant: Participant | None,
            ) -> str | None:
                rules = await self._agent.get_rules(participant=participant)
                if len(rules) == 0:
                    return None

                return "\n".join(rules)

            def publish_thread_status(message) -> None:
                self.send(Message(data=message, source=process))

            process = LLMAgentProcess(
                thread_id=thread_id,
                participant=self._agent.room.local_participant,
                llm_providers=llm_providers,
                default_provider=default_provider,
                toolkits=[*toolkits],
                thread_storage=create_thread_storage(
                    room=self._agent.room,
                    thread_id=thread_id,
                ),
                thread_status_publisher=AgentMessageThreadStatusPublisher(
                    thread_id=thread_id,
                    publish=publish_thread_status,
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


@app.async_command("join", help="Join a room and run a process-backed agent.")
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
    preamble_rule: PreambleRuleOption = True,
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
        list[str],
        typer.Option(
            "--model",
            help="Name of an LLM model to make available. Can be repeated.",
        ),
    ] = ["gpt-5.5"],
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
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
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
    transcription_model: TranscriptionModelOption = (
        DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL
    ),
    voice: VoiceOption = None,
    turn_detection: TurnDetectionOption = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocol: RealtimeProtocolOption = [],
    output_modality: OutputModalityOption = [],
    input_audio_format: InputAudioFormatOption = "audio/pcm",
    input_audio_sample_rate: InputAudioSampleRateOption = 24000,
    input_audio_bitrate: InputAudioBitrateOption = None,
    output_audio_format: OutputAudioFormatOption = "audio/pcm",
    output_audio_sample_rate: OutputAudioSampleRateOption = 24000,
    output_audio_bitrate: OutputAudioBitrateOption = None,
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
    threading_mode: ThreadingModeOption = "default-new",
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = 32000,
    reasoning_effort: ReasoningEffortOption = None,
    channel: ChannelOption = [],
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for websocket channels: jwt, iap, or none."),
        ),
    ] = "jwt",
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
    verbose_dataset: Annotated[
        bool,
        typer.Option(
            "--verbose-dataset",
            help="Persist streaming delta events to dataset thread storage for debugging",
        ),
    ] = False,
    save_audio_input: Annotated[
        bool,
        typer.Option(
            "--save-audio-input",
            help="Persist realtime audio input chunks to dataset thread storage as binary attachments.",
        ),
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
    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_storage=thread_storage,
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
    room = _require_resolved_room(resolve_room(room))

    key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

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

            token = ParticipantToken(
                name=agent_name,
            )
            token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        print("[bold green]Connecting to room...[/bold green]", flush=True)

        default_room_storage_mount = bool(
            normalized_tool_options["require_storage"] or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            project_paths=merge_option_lists(
                shell_project_mount,
                shell_tool_project_path,
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
            transcription_model=transcription_model,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=_normalize_realtime_protocols(realtime_protocol),
            output_modalities=output_modality,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
            always_reply=always_reply,
            threading_mode=resolved_threading_mode,
            thread_dir=resolved_thread_dir,
            thread_storage=thread_storage,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            websocket_auth=websocket_auth,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            verbose_dataset=verbose_dataset,
            save_audio_input=save_audio_input,
            preamble_rule=preamble_rule,
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


@app.async_command("service", help="Add a process-backed agent service to the host.")
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
        list[str],
        typer.Option(
            "--model",
            help="Name of an LLM model to make available. Can be repeated.",
        ),
    ] = ["gpt-5.5"],
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
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
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
    transcription_model: TranscriptionModelOption = (
        DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL
    ),
    voice: VoiceOption = None,
    turn_detection: TurnDetectionOption = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocol: RealtimeProtocolOption = [],
    output_modality: OutputModalityOption = [],
    input_audio_format: InputAudioFormatOption = "audio/pcm",
    input_audio_sample_rate: InputAudioSampleRateOption = 24000,
    input_audio_bitrate: InputAudioBitrateOption = None,
    output_audio_format: OutputAudioFormatOption = "audio/pcm",
    output_audio_sample_rate: OutputAudioSampleRateOption = 24000,
    output_audio_bitrate: OutputAudioBitrateOption = None,
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
    threading_mode: ThreadingModeOption = "default-new",
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = 32000,
    reasoning_effort: ReasoningEffortOption = None,
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
    verbose_dataset: Annotated[
        bool,
        typer.Option(
            "--verbose-dataset",
            help="Persist streaming delta events to dataset thread storage for debugging",
        ),
    ] = False,
    save_audio_input: Annotated[
        bool,
        typer.Option(
            "--save-audio-input",
            help="Persist realtime audio input chunks to dataset thread storage as binary attachments.",
        ),
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
    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_storage=thread_storage,
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
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
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
                threading_mode=resolved_threading_mode,
                thread_dir=resolved_thread_dir,
                thread_storage=thread_storage,
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
            transcription_model=transcription_model,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=_normalize_realtime_protocols(realtime_protocol),
            output_modalities=output_modality,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
            always_reply=always_reply,
            threading_mode=resolved_threading_mode,
            thread_dir=resolved_thread_dir,
            thread_storage=thread_storage,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
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
            verbose_dataset=verbose_dataset,
            save_audio_input=save_audio_input,
        ),
    )

    if not get_deferred():
        await run_services()


@app.async_command(
    "spec", help="Generate a service spec for deploying a process-backed agent."
)
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
        list[str],
        typer.Option(
            "--model",
            help="Name of an LLM model to make available. Can be repeated.",
        ),
    ] = ["gpt-5.5"],
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
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
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
    transcription_model: TranscriptionModelOption = (
        DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL
    ),
    voice: VoiceOption = None,
    turn_detection: TurnDetectionOption = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocol: RealtimeProtocolOption = [],
    output_modality: OutputModalityOption = [],
    input_audio_format: InputAudioFormatOption = "audio/pcm",
    input_audio_sample_rate: InputAudioSampleRateOption = 24000,
    input_audio_bitrate: InputAudioBitrateOption = None,
    output_audio_format: OutputAudioFormatOption = "audio/pcm",
    output_audio_sample_rate: OutputAudioSampleRateOption = 24000,
    output_audio_bitrate: OutputAudioBitrateOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "default-new",
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = 32000,
    reasoning_effort: ReasoningEffortOption = None,
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
    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_storage=thread_storage,
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
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
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
                threading_mode=resolved_threading_mode,
                thread_dir=resolved_thread_dir,
                thread_storage=thread_storage,
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
            transcription_model=transcription_model,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=_normalize_realtime_protocols(realtime_protocol),
            output_modalities=output_modality,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
            always_reply=always_reply,
            threading_mode=resolved_threading_mode,
            thread_dir=resolved_thread_dir,
            thread_storage=thread_storage,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
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


@app.async_command("deploy", help="Deploy a process-backed agent service.")
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
        list[str],
        typer.Option(
            "--model",
            help="Name of an LLM model to make available. Can be repeated.",
        ),
    ] = ["gpt-5.5"],
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
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
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
    transcription_model: TranscriptionModelOption = (
        DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL
    ),
    voice: VoiceOption = None,
    turn_detection: TurnDetectionOption = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocol: RealtimeProtocolOption = [],
    output_modality: OutputModalityOption = [],
    input_audio_format: InputAudioFormatOption = "audio/pcm",
    input_audio_sample_rate: InputAudioSampleRateOption = 24000,
    input_audio_bitrate: InputAudioBitrateOption = None,
    output_audio_format: OutputAudioFormatOption = "audio/pcm",
    output_audio_sample_rate: OutputAudioSampleRateOption = 24000,
    output_audio_bitrate: OutputAudioBitrateOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "default-new",
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = 32000,
    reasoning_effort: ReasoningEffortOption = None,
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
    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_storage=thread_storage,
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
        project_paths=merge_option_lists(
            shell_project_mount,
            shell_tool_project_path,
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
                threading_mode=resolved_threading_mode,
                thread_dir=resolved_thread_dir,
                thread_storage=thread_storage,
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
            transcription_model=transcription_model,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=_normalize_realtime_protocols(realtime_protocol),
            output_modalities=output_modality,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
            always_reply=always_reply,
            threading_mode=resolved_threading_mode,
            thread_dir=resolved_thread_dir,
            thread_storage=thread_storage,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
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
        from rich.style import Style
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

    @dataclass(frozen=True, slots=True)
    class _ThreadDetailEntry:
        index: int
        item: Element

    @dataclass(frozen=True, slots=True)
    class _ThreadDetailGroup:
        id: str
        indexes: tuple[int, ...]
        items: tuple[Element, ...]
        collapsed_text: str

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

        @property
        def thread_status(self) -> AgentThreadStatus | None:
            if self._thread is None:
                return None
            return self._thread.thread_status

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
            self._expanded_detail_group_ids: set[str] = set()

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

        def action_toggle_detail_group(self, group_id: str) -> None:
            if group_id in self._expanded_detail_group_ids:
                self._expanded_detail_group_ids.remove(group_id)
            else:
                self._expanded_detail_group_ids.add(group_id)
            self._render_from_thread_document()

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
            for entry in self._thread_feed_entries(items):
                if isinstance(entry, _ThreadDetailGroup):
                    expanded = entry.id in self._expanded_detail_group_ids
                    rendered_items.append(
                        self._render_detail_group_header(entry, expanded=expanded)
                    )
                    if not expanded:
                        continue
                    for index, item in zip(entry.indexes, entry.items, strict=True):
                        for renderable in self._render_thread_item(
                            item,
                            is_last_item=index == last_index,
                            has_active_event=self._has_active_events,
                        ):
                            rendered_items.append(renderable)
                    continue

                for renderable in self._render_thread_item(
                    entry.item,
                    is_last_item=entry.index == last_index,
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
            status = self._chat_client.thread_status
            if status is None:
                return _thread_status_text(self._chat_client.thread_status_text)
            text = _thread_status_text(status.status)
            if text is None:
                return None
            return _format_thread_status_text(
                text,
                total_bytes=status.total_bytes,
                lines_added=status.lines_added,
                lines_removed=status.lines_removed,
            )

        def _thread_feed_entries(
            self, items
        ) -> list[_ThreadDetailEntry | _ThreadDetailGroup]:
            entries: list[_ThreadDetailEntry | _ThreadDetailGroup] = []
            index = 0
            item_count = len(items)
            while index < item_count:
                next_user_index = self._next_user_message_index(items, index + 1)
                segment_end = (
                    next_user_index if next_user_index is not None else item_count
                )
                detail_indexes: set[int] = set()
                self._add_detail_indexes_for_segment(
                    items, start=index, end=segment_end, detail_indexes=detail_indexes
                )
                grouped_indexes = tuple(sorted(detail_indexes))
                grouped_items = tuple(
                    item
                    for detail_index in grouped_indexes
                    if isinstance(item := items[detail_index], Element)
                )
                inserted_group = False
                for segment_index in range(index, segment_end):
                    item = items[segment_index]
                    if not isinstance(item, Element):
                        continue
                    if segment_index not in detail_indexes:
                        entries.append(_ThreadDetailEntry(segment_index, item))
                        continue
                    if inserted_group or len(grouped_items) == 0:
                        continue
                    next_message = self._next_non_detail_item(
                        items,
                        detail_indexes=detail_indexes,
                        start=segment_index + 1,
                        end=segment_end,
                    )
                    entries.append(
                        self._detail_group_for_items(
                            indexes=grouped_indexes,
                            items=grouped_items,
                            next_item=next_message,
                        )
                    )
                    inserted_group = True
                index = segment_end
            return entries

        def _next_non_detail_item(
            self, items, *, detail_indexes: set[int], start: int, end: int
        ) -> Element | None:
            for index in range(start, end):
                item = items[index]
                if index not in detail_indexes and isinstance(item, Element):
                    return item
            return None

        def _next_user_message_index(self, items, start: int) -> int | None:
            for index in range(start, len(items)):
                item = items[index]
                if isinstance(item, Element) and self._is_user_message(item):
                    return index
            return None

        def _add_detail_indexes_for_segment(
            self, items, *, start: int, end: int, detail_indexes: set[int]
        ) -> None:
            final_agent_message_index = self._final_agent_message_index_for_segment(
                items, start=start, end=end
            )
            for index in range(start, end):
                item = items[index]
                if not isinstance(item, Element):
                    continue
                if self._is_intrinsic_detail(item):
                    detail_indexes.add(index)
                    continue
                if (
                    index != final_agent_message_index
                    and self._can_collapse_as_commentary(item)
                ):
                    detail_indexes.add(index)

        def _final_agent_message_index_for_segment(
            self, items, *, start: int, end: int
        ) -> int:
            explicit_final_index = -1
            for index in range(start, end):
                item = items[index]
                if (
                    isinstance(item, Element)
                    and self._can_render_as_final_answer(item)
                    and self._item_string_attribute(item, "phase") == "final_answer"
                ):
                    explicit_final_index = index
            if explicit_final_index != -1:
                return explicit_final_index

            inferred_final_index = -1
            for index in range(start, end):
                item = items[index]
                if isinstance(item, Element) and self._can_render_as_final_answer(item):
                    inferred_final_index = index
            return inferred_final_index

        def _detail_group_for_items(
            self,
            *,
            indexes: tuple[int, ...],
            items: tuple[Element, ...],
            next_item: Element | None,
        ) -> _ThreadDetailGroup:
            first = items[0]
            first_index = indexes[0] if len(indexes) > 0 else 0
            group_id = ":".join(
                (
                    "details",
                    self._item_string_attribute(first, "turn_id"),
                    self._item_string_attribute(first, "id")
                    or self._item_string_attribute(first, "item_id")
                    or str(first_index),
                    self._item_string_attribute(first, "created_at"),
                )
            )
            return _ThreadDetailGroup(
                id=group_id,
                indexes=indexes,
                items=items,
                collapsed_text=self._detail_group_collapsed_text(
                    items, next_item=next_item
                ),
            )

        def _detail_group_collapsed_text(
            self, items: tuple[Element, ...], *, next_item: Element | None
        ) -> str:
            first = items[0]
            if self._detail_group_has_final_response(items, next_item=next_item):
                end = (
                    self._item_created_at(next_item) if next_item is not None else None
                )
                active_turn_id = self._active_thread_status_turn_id()
                first_turn_id = self._item_string_attribute(first, "turn_id")
                if (
                    active_turn_id is not None
                    and first_turn_id != ""
                    and first_turn_id == active_turn_id
                ):
                    end = datetime.now(timezone.utc)
                start = self._item_created_at(first)
                if start is not None and end is not None:
                    return (
                        f"Worked for {self._format_detail_group_duration(end - start)}"
                    )
                return "Worked"

            collapsed_item = self._detail_group_collapsed_item(items)
            text = (
                self._detail_item_text(collapsed_item)
                if collapsed_item is not None
                else ""
            )
            first_line = self._first_non_empty_line(text)
            return first_line if first_line is not None else "Working"

        def _detail_group_has_final_response(
            self, items: tuple[Element, ...], *, next_item: Element | None
        ) -> bool:
            return (
                next_item is not None
                and self._can_render_as_final_answer(next_item)
                and self._items_share_turn(items[0], next_item)
            )

        def _detail_group_collapsed_item(
            self, items: tuple[Element, ...]
        ) -> Element | None:
            for item in reversed(items):
                if (
                    self._can_collapse_as_commentary(item)
                    and self._item_string_attribute(item, "text").strip() != ""
                ):
                    return item
            for item in reversed(items):
                if (
                    item.tag_name == "reasoning"
                    and self._detail_item_text(item).strip() != ""
                ):
                    return item
            for item in reversed(items):
                if self._detail_item_text(item).strip() != "":
                    return item
            return None

        def _is_intrinsic_detail(self, item: Element) -> bool:
            if item.tag_name in {"event", "reasoning", "exec", "ui"}:
                return True
            return (
                self._can_collapse_as_commentary(item)
                and self._item_string_attribute(item, "phase") == "commentary"
            )

        def _can_collapse_as_commentary(self, item: Element) -> bool:
            if item.tag_name != "message":
                return False
            if self._item_string_attribute(item, "phase") == "final_answer":
                return False
            if self._message_role(item) not in {"agent", "assistant"}:
                return False
            return not self._message_has_attachments(item)

        def _can_render_as_final_answer(self, item: Element) -> bool:
            if item.tag_name != "message":
                return False
            if self._message_role(item) not in {"agent", "assistant"}:
                return False
            if self._item_string_attribute(item, "phase") == "commentary":
                return False
            return self._item_string_attribute(
                item, "text"
            ).strip() != "" or self._message_has_attachments(item)

        def _is_user_message(self, item: Element) -> bool:
            return item.tag_name == "message" and self._message_role(item) == "user"

        def _message_role(self, item: Element) -> str:
            role = self._item_string_attribute(item, "role").lower()
            if role in {"user", "agent", "assistant"}:
                return role
            author = self._item_string_attribute(item, "author_name")
            return "user" if author == self._local_user_name else "agent"

        def _message_has_attachments(self, item: Element) -> bool:
            for child in item.get_children():
                if isinstance(child, Element) and child.tag_name in {"file", "image"}:
                    return True
            return False

        def _items_share_turn(self, left: Element, right: Element) -> bool:
            left_turn_id = self._item_string_attribute(left, "turn_id")
            right_turn_id = self._item_string_attribute(right, "turn_id")
            if left_turn_id == "" or right_turn_id == "":
                return True
            return left_turn_id == right_turn_id

        def _active_thread_status_turn_id(self) -> str | None:
            status = self._chat_client.thread_status
            if status is None or status.turn_id is None:
                return None
            normalized = status.turn_id.strip()
            return normalized if normalized != "" else None

        def _item_string_attribute(self, item: Element, name: str) -> str:
            value = item.get_attribute(name)
            return value.strip() if isinstance(value, str) else ""

        def _item_created_at(self, item: Element | None) -> datetime | None:
            if item is None:
                return None
            created_at = self._item_string_attribute(item, "created_at")
            if created_at == "":
                return None
            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed

        def _detail_item_text(self, item: Element | None) -> str:
            if item is None:
                return ""
            if item.tag_name == "reasoning":
                return self._item_string_attribute(
                    item, "summary"
                ) or self._item_string_attribute(item, "text")
            if item.tag_name == "event":
                return (
                    self._item_string_attribute(item, "headline")
                    or self._item_string_attribute(item, "summary")
                    or self._item_string_attribute(item, "name")
                    or self._item_string_attribute(item, "details")
                )
            return self._item_string_attribute(item, "text")

        def _first_non_empty_line(self, text: str) -> str | None:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped != "":
                    return stripped
            return None

        def _format_detail_group_duration(self, duration) -> str:
            seconds = int(duration.total_seconds())
            if seconds < 0:
                seconds = 0
            if seconds < 60:
                return f"{seconds}s"
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            if minutes < 60:
                if remaining_seconds == 0:
                    return f"{minutes}m"
                return f"{minutes}m {remaining_seconds}s"
            hours = minutes // 60
            remaining_minutes = minutes % 60
            if remaining_minutes == 0:
                return f"{hours}h"
            return f"{hours}h {remaining_minutes}m"

        def _render_detail_group_header(
            self, group: _ThreadDetailGroup, *, expanded: bool
        ) -> RenderableType:
            table = Table.grid(expand=True, padding=(0, 0))
            table.add_column(width=2, no_wrap=True)
            table.add_column(ratio=1)
            table.add_column(width=2, no_wrap=True)

            marker = "▾" if expanded else "▸"
            label = Text()
            label.append(f"{marker} ", style="dim")
            label.append(group.collapsed_text, style="bold magenta")
            label.stylize(
                Style(
                    underline=not expanded,
                    meta={
                        "@click": (
                            "app.toggle_detail_group",
                            (group.id,),
                        )
                    },
                )
            )
            table.add_row(Text("  "), Text(" "), Text("  "))
            table.add_row(Text("  "), label, Text("  "))
            table.add_row(Text("  "), Text(" "), Text("  "))
            return table

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


@app.async_command(
    "run", help="Join a room, run a process-backed agent, and wait for messages."
)
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
    preamble_rule: PreambleRuleOption = True,
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
        list[str],
        typer.Option(
            "--model",
            help="Name of an LLM model to make available. Can be repeated.",
        ),
    ] = ["gpt-5.5"],
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
    shell_project_mount: ShellProjectMountOption = [],
    shell_tool_project_path: ShellProjectMountLegacyOption = [],
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
    transcription_model: TranscriptionModelOption = (
        DEFAULT_OPENAI_REALTIME_TRANSCRIPTION_MODEL
    ),
    voice: VoiceOption = None,
    turn_detection: TurnDetectionOption = DEFAULT_OPENAI_REALTIME_TURN_DETECTION,
    realtime_protocol: RealtimeProtocolOption = [],
    output_modality: OutputModalityOption = [],
    input_audio_format: InputAudioFormatOption = "audio/pcm",
    input_audio_sample_rate: InputAudioSampleRateOption = 24000,
    input_audio_bitrate: InputAudioBitrateOption = None,
    output_audio_format: OutputAudioFormatOption = "audio/pcm",
    output_audio_sample_rate: OutputAudioSampleRateOption = 24000,
    output_audio_bitrate: OutputAudioBitrateOption = None,
    always_reply: Annotated[
        Optional[bool],
        typer.Option(..., help="Always reply"),
    ] = None,
    threading_mode: ThreadingModeOption = "default-new",
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    context_management: ContextManagementOption = "auto",
    compaction_threshold: CompactionThresholdOption = None,
    max_output_tokens: MaxOutputTokensOption = 32000,
    reasoning_effort: ReasoningEffortOption = None,
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
    verbose_dataset: Annotated[
        bool,
        typer.Option(
            "--verbose-dataset",
            help="Persist streaming delta events to dataset thread storage for debugging",
        ),
    ] = False,
    save_audio_input: Annotated[
        bool,
        typer.Option(
            "--save-audio-input",
            help="Persist realtime audio input chunks to dataset thread storage as binary attachments.",
        ),
    ] = False,
    websocket_auth: Annotated[
        WebSocketAuthMode,
        typer.Option(
            "--websocket-auth",
            help=("Authentication mode for websocket channels: jwt, iap, or none."),
        ),
    ] = "jwt",
    user: Annotated[
        str,
        typer.Option(
            "--user",
            help="User name for the local websocket process run client.",
        ),
    ] = "you",
    thread_path: Annotated[
        Optional[str],
        typer.Option(..., help="log all requests to the llm"),
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
    )
    websocket_run_channel = _process_run_websocket_channel(channels=resolved_channels)
    if runtime == "process" and not _has_chat_channel(channels=resolved_channels):
        if websocket_run_channel is None:
            raise typer.BadParameter(
                "--channel=chat or --channel=websocket:PORT is required"
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
    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_storage=thread_storage,
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
    room = _require_resolved_room(resolve_room(room))

    key = await resolve_key(project_id=project_id, key=key)
    account_client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)

        jwt = os.getenv("MESHAGENT_TOKEN")
        if jwt is None:
            if agent_name is None:
                print(
                    "[bold red]--agent-name must be specified when the MESHAGENT_TOKEN environment variable is not set[/bold red]"
                )
                raise typer.Exit(1)

            token = ParticipantToken(
                name=agent_name,
            )

            token.add_api_grant(ApiScope.agent_default(tunnels=require_computer_use))

            token.add_role_grant(role=role)
            token.add_room_grant(room)

            jwt = token.to_jwt(api_key=key)

        default_room_storage_mount = bool(
            normalized_tool_options["require_storage"] or require_read_only_storage
        )
        shell_tool_mounts = parse_shell_tool_mounts(
            room_paths=merge_option_lists(
                shell_room_mount,
                shell_tool_room_path,
            ),
            project_paths=merge_option_lists(
                shell_project_mount,
                shell_tool_project_path,
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
            transcription_model=transcription_model,
            voice=voice,
            turn_detection=turn_detection,
            realtime_protocols=_normalize_realtime_protocols(realtime_protocol),
            output_modalities=output_modality,
            input_audio_format=input_audio_format,
            input_audio_sample_rate=input_audio_sample_rate,
            input_audio_bitrate=input_audio_bitrate,
            output_audio_format=output_audio_format,
            output_audio_sample_rate=output_audio_sample_rate,
            output_audio_bitrate=output_audio_bitrate,
            always_reply=always_reply,
            threading_mode=resolved_threading_mode,
            thread_dir=resolved_thread_dir,
            thread_storage=thread_storage,
            context_management=context_management,
            compaction_threshold=compaction_threshold,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            verbose_dataset=verbose_dataset,
            working_dir=working_dir,
            dataset_namespace=resolved_dataset_namespace,
            skill_dirs=skill_dir,
            shell_image=shell_image,
            delegate_shell_token=delegate_shell_token,
            shell_copy_env=shell_copy_env,
            shell_set_env=shell_set_env,
            log_llm_requests=log_llm_requests,
            channels=resolved_channels,
            websocket_auth=websocket_auth,
            starting_url=starting_url,
            allow_goto_url=allow_goto_url,
            room_rules_path=room_rules,
            save_audio_input=save_audio_input,
            preamble_rule=preamble_rule,
        )

        bot = CustomChatbot()

        async def run_interactive_session(client: RoomClient) -> None:
            if runtime == "process":
                process_tui_kwargs: dict[str, Any] = {
                    "bot": bot,
                    "room": client,
                    "model": model,
                    "thread_path": thread_path,
                    "thread_storage": thread_storage,
                    "agent_name": agent_name,
                    "thread_dir": resolved_thread_dir,
                    "threading_mode": resolved_threading_mode,
                    "message": message,
                    "working_dir": working_dir,
                }
                if voice is not None:
                    process_tui_kwargs["voice"] = voice
                process_tui_kwargs["turn_detection"] = turn_detection
                process_tui_kwargs["realtime_protocols"] = (
                    _normalize_realtime_protocols(realtime_protocol)
                )
                process_tui_kwargs["output_modalities"] = output_modality
                audio_format_kwargs = _realtime_adapter_audio_kwargs(
                    voice=None,
                    input_format=_audio_format_option(
                        audio_format=input_audio_format,
                        sample_rate=input_audio_sample_rate,
                        bitrate=input_audio_bitrate,
                    ),
                    output_format=_audio_format_option(
                        audio_format=output_audio_format,
                        sample_rate=output_audio_sample_rate,
                        bitrate=output_audio_bitrate,
                    ),
                )
                if "input_format" in audio_format_kwargs:
                    process_tui_kwargs["input_audio_format"] = input_audio_format
                    process_tui_kwargs["input_audio_sample_rate"] = (
                        input_audio_sample_rate
                    )
                    process_tui_kwargs["input_audio_bitrate"] = input_audio_bitrate
                if "output_format" in audio_format_kwargs:
                    process_tui_kwargs["output_audio_format"] = output_audio_format
                    process_tui_kwargs["output_audio_sample_rate"] = (
                        output_audio_sample_rate
                    )
                    process_tui_kwargs["output_audio_bitrate"] = output_audio_bitrate
                if websocket_run_channel is not None:
                    process_tui_kwargs[
                        "chat_client"
                    ] = await _open_process_run_websocket_chat_session(
                        room=client,
                        websocket_config=websocket_run_channel,
                        user=user,
                        websocket_auth=websocket_auth,
                        iap_token=jwt,
                        thread_path=thread_path,
                        thread_storage=thread_storage,
                        agent_name=agent_name,
                        thread_dir=resolved_thread_dir,
                        threading_mode=resolved_threading_mode,
                    )
                interaction_task = asyncio.create_task(
                    _run_process_run_tui(**process_tui_kwargs)
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

            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
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
    "threads",
    help="List threads for a process-backed agent.",
)
async def list_threads_command(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    agent_name: Annotated[
        Optional[str], typer.Option(..., help="Name of the agent to list threads for")
    ] = None,
    thread_dir: ThreadDirOption = None,
    thread_storage: ThreadStorageOption = "meshdocument",
    limit: Annotated[int, typer.Option("--limit", help="Maximum threads to show")] = 20,
    offset: Annotated[int, typer.Option("--offset", help="Thread list offset")] = 0,
):
    runtime = _current_command_runtime()
    if runtime != "process":
        print("[bold red]threads is only supported for process agents[/bold red]")
        raise typer.Exit(1)
    if agent_name is None or agent_name.strip() == "":
        print("[bold red]--agent-name must be specified for process threads[/bold red]")
        raise typer.Exit(1)

    storage_class = _thread_storage_class_for_backend(thread_storage)
    if storage_class is None:
        print(
            "[bold red]thread listing is not available for --thread-storage=none[/bold red]"
        )
        raise typer.Exit(1)

    resolved_threading_mode, resolved_thread_dir = _resolve_process_threading_options(
        agent_name=agent_name,
        threading_mode="default-new",
        thread_dir=thread_dir,
        thread_storage=thread_storage,
    )
    del resolved_threading_mode
    if resolved_thread_dir is None:
        print("[bold red]unable to resolve a thread directory[/bold red]")
        raise typer.Exit(1)

    room = _require_resolved_room(resolve_room(room))
    account_client = await get_client()
    user_client: RoomClient | None = None
    try:
        project_id = await resolve_project_id(project_id=project_id)
        user_client = await _open_process_room_client(
            account_client=account_client,
            project_id=project_id,
            room=room,
        )
        page = await storage_class.list_threads(
            room=user_client,
            thread_dir=resolved_thread_dir,
            limit=limit,
            offset=offset,
        )
        if page.total == 0:
            print("No threads found.")
            return

        from rich.table import Table

        table = Table(title=f"{agent_name.strip()} threads")
        table.add_column("Name")
        table.add_column("Path")
        table.add_column("Modified")
        for entry in page.threads:
            table.add_row(entry.name, entry.path, entry.modified_at)
        print(table)
        if page.offset + len(page.threads) < page.total:
            print(
                f"Showing {page.offset + 1}-{page.offset + len(page.threads)} "
                f"of {page.total} threads."
            )
    finally:
        await _close_process_use_room_client(user_client)
        await account_client.close()


@app.async_command(
    "use",
    help="Send a one-shot or interactive message to a running process-backed agent.",
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
        typer.Option(..., help="log all requests to the llm"),
    ] = None,
    message: Annotated[
        Optional[str],
        typer.Option(..., help="the input message to use"),
    ] = None,
    websocket_url: Annotated[
        Optional[str],
        typer.Option(
            "--websocket-url",
            help="Connect to a process websocket channel instead of room chat.",
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
            help="User name for the websocket process use client.",
        ),
    ] = "you",
):
    runtime = _current_command_runtime()
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    resolved_websocket_url = (
        _normalize_process_use_websocket_url(websocket_url)
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
                    raise RoomException("process use account client is unavailable")
                connection = await account_client.connect_room(
                    project_id=project_id,
                    room=room,
                )
                iap_token = connection.jwt

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
                websocket_url=resolved_websocket_url,
                user=user,
                websocket_auth=websocket_auth,
                iap_token=iap_token,
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
        if account_client is not None:
            await account_client.close()


strip_command_options(app, option_names=_HIDDEN_REQUIRE_OPTION_NAMES)
