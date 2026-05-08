from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, runtime_checkable

import click
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.messages import (
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AGENT_EVENT_TURN_START_REJECTED,
    AGENT_EVENT_TURN_STEER_ACCEPTED,
    AGENT_EVENT_TURN_STEER_REJECTED,
    AGENT_EVENT_TURN_STEERED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_EVENT_USAGE_UPDATED,
    AGENT_MESSAGE_TURN_INTERRUPT,
    AGENT_MESSAGE_THREAD_START,
    AGENT_MESSAGE_TURN_START,
    AGENT_MESSAGE_TURN_STEER,
    AgentFileContent,
    AgentFileContentDelta,
    AgentMessage,
    AgentTextContent,
    AgentTextContentDelta,
    AgentThreadStatus,
    AgentUsageUpdated,
    StartThread,
    TurnEnded,
    TurnInterrupt,
    TurnStart,
    TurnStartAccepted,
    TurnStartRejected,
    TurnStarted,
    TurnSteer,
    TurnSteerAccepted,
    TurnSteerRejected,
    TurnSteered,
)
from meshagent.agents.process import (
    AgentSupervisor,
    LLMAgentProcess,
    Message,
    TurnInstructionsProvider,
)
from meshagent.api import Participant, RoomException
from meshagent.cli import async_typer, auth_async
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import resolve_project_id
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolLocalMount, StorageToolkit

_MESHAGENT_PROJECT_ID_HEADER = "Meshagent-Project-Id"
_MESHAGENT_TOKEN_ENV = "MESHAGENT_TOKEN"
_DEFAULT_ASK_MODEL = "gpt-5.5"
_ASK_ACTIVE_STATUS_STATES = {"queued", "in_progress", "running", "pending"}
_ASK_TERMINAL_STATUS_STATES = {
    "completed",
    "complete",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
}

app = async_typer.AsyncTyper(no_args_is_help=False)


@dataclass(frozen=True, slots=True)
class _AskConversationMessage:
    message_id: str
    role: str
    text: str


@dataclass(frozen=True, slots=True)
class _PendingSteerCallback:
    prompt: str
    on_accepted: Callable[[], Awaitable[None] | None] | None
    on_applied: Callable[[], Awaitable[None] | None] | None
    on_rejected: Callable[[RoomException], Awaitable[None] | None] | None


@runtime_checkable
class _AskExternalThreadState(Protocol):
    @property
    def thread_status_text(self) -> str | None: ...

    @property
    def queued_message_labels(self) -> tuple[str, ...]: ...

    @property
    def messages(self) -> tuple[_AskConversationMessage, ...]: ...


class _AgentMessageChannelClient(Protocol):
    @property
    def has_thread_path(self) -> bool: ...

    @property
    def thread_path(self) -> str: ...

    @property
    def thread_status_text(self) -> str | None: ...

    @property
    def queued_message_labels(self) -> tuple[str, ...]: ...

    def clear_applied_queued_agent_inputs(self) -> None: ...

    async def send(self, payload: Any) -> None: ...

    async def start_thread(self, payload: Any) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _format_token_count(value: float | int) -> str:
    count = float(value)
    magnitude = abs(count)
    if magnitude >= 1_000_000:
        formatted = f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}M"
    if magnitude >= 1_000:
        formatted = f"{count / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}K"
    return str(int(count))


def _thread_status_text(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    normalized = status.strip()
    if normalized == "":
        return None
    return normalized


async def _maybe_await(callback_result: Any) -> None:
    if inspect.isawaitable(callback_result):
        await callback_result


async def _emit_agent_message(
    callback: Callable[[AgentMessage], Awaitable[None] | None] | None,
    message: AgentMessage,
) -> None:
    if callback is not None:
        await _maybe_await(callback(message))


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


class _AskSupervisor(AgentSupervisor):
    def __init__(self, *, process: LLMAgentProcess) -> None:
        super().__init__()
        self._process = process
        self.add_process(process)
        self.events: asyncio.Queue[Message] = asyncio.Queue()

    def create_thread_process(self, thread_id: str):
        if thread_id != self._process.thread_id:
            raise ValueError(f"unknown thread_id {thread_id}")
        return self._process

    def send(self, message: Message) -> None:
        if message.source is not None:
            self.events.put_nowait(message)
        super().send(message)


def _status_text_from_custom_event(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type not in ("agent.event", "codex.event"):
        return None

    raw_state = event.get("state")
    if not isinstance(raw_state, str):
        return None
    state = raw_state.strip().lower()
    if state == "":
        return None

    if state in _ASK_ACTIVE_STATUS_STATES:
        for candidate in (event.get("headline"), event.get("summary")):
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized != "":
                    return normalized
        return "Working"

    if state in _ASK_TERMINAL_STATUS_STATES:
        return "Working"

    return None


class _StatusAwareLLMAdapter(LLMAdapter[Any]):
    def __init__(self, *, delegate: LLMAdapter) -> None:
        self._delegate = delegate
        self._status_callback: Callable[[str], None] | None = None

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        self._status_callback = callback

    def default_model(self) -> str:
        return self._delegate.default_model()

    def create_session(self):
        return self._delegate.create_session()

    def get_additional_instructions(self) -> str | None:
        return self._delegate.get_additional_instructions()

    def on_turn_steer(self, *, context, interrupted: bool) -> None:
        self._delegate.on_turn_steer(context=context, interrupted=interrupted)

    def context_window_size(self, model: str) -> float:
        return self._delegate.context_window_size(model)

    def needs_compaction(self, *, context) -> bool:
        return self._delegate.needs_compaction(context=context)

    async def compact(self, *, context, model: str | None = None) -> None:
        await self._delegate.compact(context=context, model=model)

    async def get_input_tokens(
        self,
        *,
        context,
        model: str,
        toolkits: list | None = None,
        output_schema: dict | None = None,
    ) -> int:
        return await self._delegate.get_input_tokens(
            context=context,
            model=model,
            toolkits=toolkits,
            output_schema=output_schema,
        )

    async def check_for_termination(self, *, context):
        return await self._delegate.check_for_termination(context=context)

    def set_tool_call_approval_handler(self, handler) -> None:
        self._delegate.set_tool_call_approval_handler(handler)

    def make_agent_event_publisher(
        self,
        turn_id: str,
        thread_id: str,
        callback,
        custom_event_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        def _handle_custom_event(event: dict[str, Any]) -> None:
            if custom_event_callback is not None:
                custom_event_callback(event)

            if self._status_callback is None:
                return

            status_text = _status_text_from_custom_event(event)
            if status_text is not None:
                self._status_callback(status_text)

        return self._delegate.make_agent_event_publisher(
            turn_id=turn_id,
            thread_id=thread_id,
            callback=callback,
            custom_event_callback=_handle_custom_event,
        )

    async def next(
        self,
        *,
        context,
        caller: Participant,
        toolkits,
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of: Participant | None = None,
        tool_choice=None,
        options: dict | None = None,
    ) -> Any:
        return await self._delegate.next(
            context=context,
            caller=caller,
            toolkits=toolkits,
            output_schema=output_schema,
            event_handler=event_handler,
            steering_callback=steering_callback,
            model=model,
            on_behalf_of=on_behalf_of,
            tool_choice=tool_choice,
            options=options,
        )


class _LocalAgentMessageChannelClient:
    def __init__(
        self,
        *,
        thread_path: str,
        send_message: Callable[[Message], None],
        events: asyncio.Queue[Message],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._thread_path = thread_path
        self._send_message = send_message
        self._events = events
        self._on_close = on_close

    @property
    def has_thread_path(self) -> bool:
        return True

    @property
    def thread_path(self) -> str:
        return self._thread_path

    @property
    def thread_status_text(self) -> str | None:
        return None

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return ()

    def clear_applied_queued_agent_inputs(self) -> None:
        return

    async def send(self, payload: Any) -> None:
        self._send_message(Message(data=payload))

    async def start_thread(self, payload: Any) -> None:
        del payload
        raise RoomException("local agent message client already has a thread")

    async def receive(self) -> dict[str, Any]:
        event = await self._events.get()
        return event.data.model_dump(mode="python")

    async def close(self) -> None:
        if self._on_close is not None:
            self._on_close()


class _AgentMessageSession:
    def __init__(
        self,
        *,
        client: _AgentMessageChannelClient,
        model: str | None,
        current_working_directory: str | None = None,
        local_participant_name: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._local_participant_name = (
            local_participant_name.strip()
            if isinstance(local_participant_name, str)
            and local_participant_name.strip() != ""
            else None
        )
        self._active_turn_id: str | None = None
        self._thread_status_text: str | None = None
        self._messages: list[_AskConversationMessage] = []
        self._message_ids: set[str] = set()
        self._pending_input_messages: dict[str, _AskConversationMessage] = {}
        self._pending_steer_callbacks: dict[str, _PendingSteerCallback] = {}

    @property
    def current_working_directory(self) -> str:
        return self._current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        if self._thread_status_text is not None:
            return self._thread_status_text
        return self._client.thread_status_text

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._client.queued_message_labels

    @property
    def messages(self) -> tuple[_AskConversationMessage, ...]:
        return tuple(self._messages)

    def add_message(self, *, message_id: str, role: str, text: str) -> None:
        normalized_message_id = message_id.strip()
        normalized_text = text.strip()
        if normalized_message_id == "" or normalized_text == "":
            return
        if normalized_message_id in self._message_ids:
            return
        self._message_ids.add(normalized_message_id)
        self._messages.append(
            _AskConversationMessage(
                message_id=normalized_message_id,
                role=role.strip() or "user",
                text=normalized_text,
            )
        )

    def add_agent_message(self, message: AgentMessage) -> None:
        if isinstance(message, (TurnStart, TurnSteer)):
            text = self._agent_input_content_text(message.content)
            if text == "":
                return
            role = self._role_for_sender(message.sender_name, default="user")
            self.add_message(message_id=message.message_id, role=role, text=text)
            return

        if isinstance(message, AgentTextContentDelta):
            role = self._role_for_sender(message.sender_name, default="assistant")
            self.add_message(
                message_id=message.item_id,
                role=role,
                text=message.text,
            )
            return

        if isinstance(message, AgentFileContentDelta):
            role = self._role_for_sender(message.sender_name, default="assistant")
            self.add_message(
                message_id=message.item_id,
                role=role,
                text=f"[attachment] {message.url}",
            )

    def _role_for_sender(self, sender_name: object, *, default: str) -> str:
        if not isinstance(sender_name, str):
            return default
        normalized_sender_name = sender_name.strip()
        if normalized_sender_name == "":
            return default
        if normalized_sender_name == self._local_participant_name:
            return "you"
        return normalized_sender_name

    @staticmethod
    def _agent_input_content_text(
        content: list[AgentTextContent | AgentFileContent],
    ) -> str:
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, AgentTextContent) and item.text.strip() != "":
                text_parts.append(item.text)
                continue
            if isinstance(item, AgentFileContent) and item.url.strip() != "":
                text_parts.append(f"[attachment] {item.url}")

        return "\n\n".join(text_parts).strip()

    async def close(self, *, close_client: bool = True) -> None:
        self._pending_steer_callbacks.clear()
        if close_client:
            await self._client.close()

    async def ask(
        self,
        *,
        prompt: str,
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        content = [
            AgentTextContent(
                type="text",
                text=prompt,
            )
        ]
        if self._client.has_thread_path:
            turn_start_args: dict[str, Any] = {
                "type": AGENT_MESSAGE_TURN_START,
                "thread_id": self._client.thread_path,
                "content": content,
            }
            if self._model is not None:
                turn_start_args["model"] = self._model
            input_message = TurnStart.model_validate(turn_start_args)
        else:
            start_thread_args: dict[str, Any] = {
                "type": AGENT_MESSAGE_THREAD_START,
                "content": content,
            }
            if self._model is not None:
                start_thread_args["model"] = self._model
            input_message = StartThread.model_validate(start_thread_args)

        self._pending_input_messages[input_message.message_id] = (
            _AskConversationMessage(
                message_id=input_message.message_id,
                role="you",
                text=prompt,
            )
        )
        if isinstance(input_message, StartThread):
            await self._client.start_thread(input_message)
        else:
            await self._client.send(input_message)

        await _emit_agent_message(
            on_message,
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id=self._client.thread_path,
                status="Working",
            ),
        )

        output_parts: list[str] = []
        active_turn_id: str | None = None
        try:
            while True:
                payload = await self._client.receive()
                if payload.get("thread_id") != self._client.thread_path:
                    continue
                event_type = payload.get("type")

                if event_type == AGENT_EVENT_TURN_START_ACCEPTED:
                    turn_start_accepted = TurnStartAccepted.model_validate(payload)
                    self._add_accepted_agent_input(turn_start_accepted)
                    await _emit_agent_message(on_message, turn_start_accepted)
                    continue

                if event_type == AGENT_EVENT_TURN_STARTED:
                    turn_started = TurnStarted.model_validate(payload)
                    if turn_started.source_message_id == input_message.message_id:
                        active_turn_id = turn_started.turn_id
                        self._active_turn_id = active_turn_id
                        await _emit_agent_message(on_message, turn_started)
                    continue

                if event_type == AGENT_EVENT_TURN_START_REJECTED:
                    turn_start_rejected = TurnStartRejected.model_validate(payload)
                    if (
                        turn_start_rejected.source_message_id
                        != input_message.message_id
                    ):
                        continue
                    await _emit_agent_message(on_message, turn_start_rejected)
                    raise RoomException(
                        turn_start_rejected.error.message,
                        code=turn_start_rejected.error.code,
                    )

                if event_type == AGENT_EVENT_TURN_STEER_ACCEPTED:
                    steer_accepted = TurnSteerAccepted.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.get(
                        steer_accepted.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    if pending_callbacks.on_accepted is not None:
                        await _maybe_await(pending_callbacks.on_accepted())
                    await _emit_agent_message(on_message, steer_accepted)
                    continue

                if event_type == AGENT_EVENT_TURN_STEERED:
                    steer_applied = TurnSteered.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_applied.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    self.add_message(
                        message_id=steer_applied.source_message_id,
                        role="you",
                        text=pending_callbacks.prompt,
                    )
                    if pending_callbacks.on_applied is not None:
                        await _maybe_await(pending_callbacks.on_applied())
                    await _emit_agent_message(on_message, steer_applied)
                    continue

                if event_type == AGENT_EVENT_TURN_STEER_REJECTED:
                    steer_rejected = TurnSteerRejected.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_rejected.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    if pending_callbacks.on_rejected is not None:
                        await _maybe_await(
                            pending_callbacks.on_rejected(
                                RoomException(
                                    steer_rejected.error.message,
                                    code=steer_rejected.error.code,
                                )
                            )
                        )
                    await _emit_agent_message(on_message, steer_rejected)
                    continue

                if event_type == AGENT_EVENT_THREAD_STATUS:
                    thread_status = AgentThreadStatus.model_validate(payload)
                    if active_turn_id is not None and thread_status.turn_id not in (
                        None,
                        active_turn_id,
                    ):
                        continue
                    self._thread_status_text = _thread_status_text(thread_status.status)
                    await _emit_agent_message(on_message, thread_status)
                    continue

                if event_type == AGENT_EVENT_TEXT_CONTENT_DELTA:
                    text_delta = AgentTextContentDelta.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and text_delta.turn_id != active_turn_id
                    ):
                        continue
                    output_parts.append(text_delta.text)
                    await _emit_agent_message(on_message, text_delta)
                    continue

                if event_type == AGENT_EVENT_USAGE_UPDATED:
                    usage_update = AgentUsageUpdated.model_validate(payload)
                    if active_turn_id is not None and usage_update.turn_id not in (
                        None,
                        active_turn_id,
                    ):
                        continue
                    await _emit_agent_message(on_message, usage_update)
                    continue

                if event_type == AGENT_EVENT_TURN_ENDED:
                    turn_ended = TurnEnded.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and turn_ended.turn_id != active_turn_id
                    ):
                        continue
                    if turn_ended.error is not None:
                        raise RoomException(
                            turn_ended.error.message,
                            code=turn_ended.error.code,
                        )
                    await _emit_agent_message(on_message, turn_ended)
                    return "".join(output_parts)
        finally:
            self._active_turn_id = None
            self._pending_steer_callbacks.clear()
            self._pending_input_messages.clear()
            self._thread_status_text = None
            self._client.clear_applied_queued_agent_inputs()
            await _emit_agent_message(
                on_message,
                AgentThreadStatus(
                    type=AGENT_EVENT_THREAD_STATUS,
                    thread_id=self._client.thread_path,
                    status=None,
                ),
            )

    def _add_accepted_agent_input(self, accepted: TurnStartAccepted) -> None:
        pending_input = self._pending_input_messages.pop(
            accepted.source_message_id,
            None,
        )
        if pending_input is not None:
            self.add_message(
                message_id=pending_input.message_id,
                role=pending_input.role,
                text=pending_input.text,
            )
            return

        text = self._agent_input_content_text(accepted.content)
        if text == "":
            return

        role = self._role_for_sender(accepted.sender_name, default="user")
        self.add_message(message_id=accepted.source_message_id, role=role, text=text)

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        if self._active_turn_id is None:
            return None

        turn_steer = TurnSteer(
            type=AGENT_MESSAGE_TURN_STEER,
            thread_id=self._client.thread_path,
            turn_id=self._active_turn_id,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
        )
        self._pending_steer_callbacks[turn_steer.message_id] = _PendingSteerCallback(
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

        async def _send_steer() -> None:
            try:
                await self._client.send(turn_steer)
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
            thread_id=self._client.thread_path,
            turn_id=self._active_turn_id,
        )

        async def _send_interrupt() -> None:
            await self._client.send(turn_interrupt)

        task = asyncio.create_task(_send_interrupt())
        task.add_done_callback(_consume_task_exception)
        return True


class _AskSession:
    def __init__(
        self,
        *,
        model: str,
        llm_adapter: LLMAdapter,
        current_working_directory: str | None = None,
        interactive: bool = True,
    ) -> None:
        self._model = model
        self._thread_id = f"/ask/{uuid.uuid4()}"
        self._participant = Participant(
            id="meshagent-ask",
            attributes={"name": "meshagent-ask"},
        )
        resolved_current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._toolkits = _build_ask_toolkits(
            model=model,
            current_working_directory=resolved_current_working_directory,
        )
        self._status_adapter = _StatusAwareLLMAdapter(delegate=llm_adapter)
        self._process = LLMAgentProcess(
            thread_id=self._thread_id,
            participant=self._participant,
            llm_adapter=self._status_adapter,
            toolkits=self._toolkits,
            turn_instructions_provider=_build_ask_turn_instructions_provider(
                current_working_directory=resolved_current_working_directory,
                interactive=interactive,
            ),
        )
        self._supervisor = _AskSupervisor(process=self._process)
        self._channel_client = _LocalAgentMessageChannelClient(
            thread_path=self._thread_id,
            send_message=self._supervisor.send,
            events=self._supervisor.events,
        )
        self._session = _AgentMessageSession(
            client=self._channel_client,
            model=model,
            current_working_directory=resolved_current_working_directory,
        )

    @property
    def current_working_directory(self) -> str:
        return self._session.current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        return self._session.thread_status_text

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._session.queued_message_labels

    @property
    def messages(self) -> tuple[_AskConversationMessage, ...]:
        return self._session.messages

    async def __aenter__(self) -> _AskSession:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.stop()

    async def start(self) -> None:
        await self._supervisor.start()

    async def stop(self) -> None:
        await self._session.close()
        await self._supervisor.stop()

    async def ask(
        self,
        *,
        prompt: str,
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        async def _handle_adapter_status(status: str) -> None:
            await _emit_agent_message(
                on_message,
                AgentThreadStatus(
                    type=AGENT_EVENT_THREAD_STATUS,
                    thread_id=self._thread_id,
                    status=status,
                ),
            )

        self._status_adapter.set_status_callback(
            None
            if on_message is None
            else lambda status: asyncio.create_task(_handle_adapter_status(status))
        )
        try:
            return await self._session.ask(
                prompt=prompt,
                on_message=on_message,
            )
        finally:
            self._status_adapter.set_status_callback(None)

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


def _build_ask_adapter(
    *,
    model: str,
    project_id: str,
    access_token: str,
) -> LLMAdapter:
    default_headers = {_MESHAGENT_PROJECT_ID_HEADER: project_id}

    if model.startswith("claude-"):
        from anthropic import AsyncAnthropic
        from meshagent.anthropic import AnthropicOpenAIResponsesStreamAdapter
        from meshagent.anthropic.proxy import resolve_base_url

        client = AsyncAnthropic(
            base_url=resolve_base_url(None),
            api_key=access_token,
            default_headers=default_headers,
        )
        return AnthropicOpenAIResponsesStreamAdapter(
            model=model,
            api_key=access_token,
            client=client,
        )

    from meshagent.openai import OpenAIResponsesAdapter
    from meshagent.openai.proxy import resolve_base_url
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=resolve_base_url(None),
        api_key=access_token,
        default_headers=default_headers,
    )
    return OpenAIResponsesAdapter(
        model=model,
        api_key=access_token,
        client=client,
    )


def _should_launch_tui(
    *,
    message: str | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    return message is None and stdin_is_tty and stdout_is_tty


async def _resolve_ask_access_token() -> str | None:
    env_token = os.environ.get(_MESHAGENT_TOKEN_ENV)
    if env_token is not None:
        normalized_env_token = env_token.strip()
        if normalized_env_token != "":
            return normalized_env_token

    access_token = await auth_async.get_access_token()
    if access_token is None:
        return None

    normalized_access_token = access_token.strip()
    if normalized_access_token == "":
        return None
    return normalized_access_token


def _build_ask_toolkits(
    *,
    model: str,
    current_working_directory: str,
) -> list[Toolkit]:
    from meshagent.anthropic.web_fetch import WebFetchTool as AnthropicWebFetchTool
    from meshagent.anthropic.web_search import (
        WebSearchTool as AnthropicWebSearchTool,
    )
    from meshagent.openai.tools.responses_adapter import ApplyPatchTool, WebSearchTool
    from meshagent.tools.web_toolkit import WebFetchTool

    storage_toolkit = StorageToolkit(
        mounts=[
            StorageToolLocalMount(
                path=current_working_directory,
                local_path=current_working_directory,
            )
        ]
    )
    toolkits: list[Toolkit] = [storage_toolkit]

    if model.startswith("claude-"):
        toolkits.append(Toolkit(name="web_fetch", tools=[AnthropicWebFetchTool()]))
        toolkits.append(Toolkit(name="web_search", tools=[AnthropicWebSearchTool()]))
        return toolkits

    toolkits.append(
        Toolkit(name="apply_patch", tools=[ApplyPatchTool(storage=storage_toolkit)])
    )
    toolkits.append(Toolkit(name="web_fetch", tools=[WebFetchTool()]))
    toolkits.append(Toolkit(name="web_search", tools=[WebSearchTool()]))
    return toolkits


def _build_ask_instructions(
    *,
    current_working_directory: str,
    interactive: bool = True,
) -> str:
    sections = [
        (
            "You are the MeshAgent assistant. You can find out more about "
            "meshagent at docs.meshagent.com and www.meshagent.com."
        ),
        (
            "If asked for coding assistance, refer to the installed meshagent sdk "
            "for questions about agents, rooms, datasets, queues, file storage, "
            "and related APIs. If the project doesn't already have the meshagent "
            "sdk installed, ask the user if they would like you to add it to "
            "their project for them if it seems like they are asking for "
            "meshagent coding help."
        ),
        (
            "You can also use the meshagent cli to perform tasks. You can get "
            "help for the sdk and its subcommands with the --help flag."
        ),
        (
            "The current working directory is "
            f"{current_working_directory}. "
            "You have a storage toolkit mounted at that same absolute path, "
            "so read and write files there using that exact path."
        ),
    ]
    if not interactive:
        sections.append(
            "You are not being run interactively. Return useful and actionable "
            "information rather than asking for user input."
        )
    return "\n\n".join(sections)


def _build_ask_turn_instructions_provider(
    *,
    current_working_directory: str,
    interactive: bool,
) -> TurnInstructionsProvider:
    instructions = _build_ask_instructions(
        current_working_directory=current_working_directory,
        interactive=interactive,
    )

    async def provide_instructions(sender: Participant | None) -> str:
        del sender
        return instructions

    return provide_instructions


async def _run_ask_process(
    *,
    prompt: str,
    model: str,
    llm_adapter: LLMAdapter,
    on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
) -> str:
    with _suppress_ask_process_logs():
        async with _AskSession(
            model=model,
            llm_adapter=llm_adapter,
            interactive=False,
        ) as session:
            return await session.ask(prompt=prompt, on_message=on_message)


def _is_cancelled_turn_error(error: Exception) -> bool:
    return isinstance(error, RoomException) and error.code == "cancelled"


@contextlib.contextmanager
def _suppress_ask_process_logs() -> Any:
    logger = logging.getLogger("agent-process")
    previous_disabled = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous_disabled


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None or raw_features.strip() == "":
        return

    parsed = [value.strip() for value in raw_features.split(",") if value.strip() != ""]
    if len(parsed) == 0:
        return

    filtered = [value for value in parsed if value.lower() not in ("debug", "devtools")]
    if len(filtered) == len(parsed):
        return

    if len(filtered) == 0:
        os.environ.pop("TEXTUAL", None)
    else:
        os.environ["TEXTUAL"] = ",".join(filtered)


async def _run_ask_tui(
    *,
    model: str,
    llm_adapter: LLMAdapter | None = None,
    session: Any | None = None,
    title: str = "meshagent ask",
    assistant_name: str = "assistant",
) -> None:
    try:
        from rich.console import Group
        from rich.text import Text
        from textual._context import active_app
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.widgets import Markdown as TextualMarkdown, Static, TextArea
    except ImportError as exc:
        click.echo(
            "Textual is required for interactive ask mode. Install meshagent-cli dependencies and retry."
        )
        raise typer.Exit(1) from exc

    _suppress_textual_debug_features()

    @dataclass(slots=True)
    class _AskFeedEntry:
        role: str
        text: str = ""
        pending: bool = False

    @dataclass(slots=True)
    class _AskUsageState:
        context_used_tokens: int
        context_total_tokens: int | None
        compaction_mode: str | None
        compaction_threshold: int | None
        total_tokens: float

    @dataclass(slots=True)
    class _QueuedAskTurn:
        prompt: str
        sent: bool = False
        accepted: bool = False
        message_id: str | None = None

    class _AskTextualApp(App[None]):
        CSS = """
        Screen {
            layout: grid;
            grid-size: 1 6;
            grid-rows: auto 1fr 2 auto auto auto;
            padding: 0;
            background: #101114;
            color: white;
        }
        #header {
            width: 100%;
            padding: 1 2 0 2;
            color: #cfd3dc;
        }
        #feed-scroll {
            width: 100%;
            height: 1fr;
            align: left bottom;
        }
        #feed {
            width: 100%;
            height: auto;
        }
        .feed-entry {
            width: 100%;
            height: auto;
            padding: 1 2 0 2;
        }
        .feed-entry--you {
            background: #2d3138;
        }
        .feed-entry-header {
            width: 100%;
            color: cyan;
            text-style: bold;
        }
        .feed-entry--you .feed-entry-header {
            color: white;
        }
        .feed-entry--error .feed-entry-header {
            color: red;
        }
        .feed-entry-body {
            width: 100%;
        }
        .feed-entry--you .feed-entry-body {
            color: white;
        }
        .feed-entry--error .feed-entry-body {
            color: red;
        }
        .feed-entry-markdown {
            width: 100%;
            padding: 0;
            background: transparent;
        }
        .feed-entry--you .feed-entry-markdown {
            color: white;
        }
        .feed-entry--error .feed-entry-markdown {
            color: red;
        }
        #active-assistant-header {
            display: none;
            width: 100%;
            padding: 1 2 0 2;
            color: cyan;
        }
        #active-assistant-body {
            display: none;
            width: 100%;
            padding: 0 2 0 2;
            background: transparent;
        }
        #status-line {
            width: 100%;
            padding: 0 2 0 2;
            color: #9aa5b8;
        }
        #turn-queue {
            display: none;
            width: 100%;
            padding: 0 2 1 2;
            color: #9aa5b8;
        }
        #input-row {
            margin: 0;
            background: #1a1d22;
            padding: 1 0 1 0;
        }
        #session-meta {
            width: 100%;
            padding: 0 2 0 2;
            color: #7e8699;
            background: #101114;
        }
        #input-prompt {
            width: 2;
            height: auto;
            content-align: center top;
            color: #8b93a5;
            background: #1a1d22;
        }
        #ask-input {
            width: 1fr;
            height: 1;
            min-height: 1;
            max-height: 8;
            border: none;
            outline: none;
            padding: 0;
            margin: 0;
            color: white;
            background: #1a1d22;
            background-tint: 0%;
        }
        #ask-input:focus {
            border: none;
            background: #1a1d22;
            background-tint: 0%;
        }
        #ask-input .text-area--cursor-line {
            background: #1a1d22;
        }
        #ask-input .text-area--gutter {
            background: #1a1d22;
        }
        #ask-input .text-area--cursor-gutter {
            background: #1a1d22;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit_app", "Quit", priority=True),
            Binding("escape", "interrupt_turn", "Interrupt", priority=True),
            Binding("shift+enter", "insert_newline", show=False, priority=True),
            Binding("enter", "submit_prompt", "Send", priority=True),
        ]

        def __init__(self, *, session: Any, title: str, assistant_name: str) -> None:
            super().__init__()
            self._session = session
            self._title = title
            self._assistant_name = assistant_name
            self._entries: list[_AskFeedEntry] = []
            self._feed_view: Vertical | None = None
            self._feed_scroll: VerticalScroll | None = None
            self._active_assistant_header: Static | None = None
            self._active_assistant_body: TextualMarkdown | None = None
            self._active_assistant_stream = None
            self._active_assistant_text = ""
            self._status_view: Static | None = None
            self._queue_view: Static | None = None
            self._input_row: Horizontal | None = None
            self._input_view: TextArea | None = None
            self._session_meta_view: Static | None = None
            self._input_height = 1
            self._rendered_entry_count = 0
            self._submit_task: asyncio.Task[None] | None = None
            self._pending = False
            self._queued_turns: list[_QueuedAskTurn] = []
            self._external_queued_messages: list[str] = []
            self._rendered_session_message_ids: set[str] = set()
            self._external_thread_active = False
            self._active_assistant_entry: _AskFeedEntry | None = None
            self._status_text: str | None = None
            self._usage_state: _AskUsageState | None = None
            self._status_started_at: float | None = None
            self._status_gradient_offset = 0
            self._spinner_timer = None

        def compose(self) -> ComposeResult:
            yield Static(
                Text(
                    f"{self._title}\nEnter to send. Shift+Enter inserts a newline. Ctrl+C quits.",
                    style="bold",
                ),
                id="header",
            )
            with VerticalScroll(id="feed-scroll"):
                yield Vertical(id="feed")
                yield Static("", id="active-assistant-header")
                yield TextualMarkdown("", id="active-assistant-body")
            yield Static("", id="status-line")
            yield Static("", id="turn-queue")
            with Horizontal(id="input-row"):
                yield Static("›", id="input-prompt")
                yield TextArea(
                    "",
                    id="ask-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                )
            yield Static("", id="session-meta")

        async def on_mount(self) -> None:
            self._feed_view = self.query_one("#feed", Vertical)
            self._feed_scroll = self.query_one("#feed-scroll", VerticalScroll)
            self._active_assistant_header = self.query_one(
                "#active-assistant-header", Static
            )
            self._active_assistant_body = self.query_one(
                "#active-assistant-body", TextualMarkdown
            )
            self._status_view = self.query_one("#status-line", Static)
            self._queue_view = self.query_one("#turn-queue", Static)
            self._input_row = self.query_one("#input-row", Horizontal)
            self._input_view = self.query_one("#ask-input", TextArea)
            self._session_meta_view = self.query_one("#session-meta", Static)
            self._input_view.focus()
            self._resize_input(self._input_view)
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._sync_external_thread_state()
            self._render_feed()
            self._render_status_line()
            self._render_turn_queue()
            self._render_session_meta()

        async def on_unmount(self) -> None:
            await self._stop_active_assistant_stream()
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            if self._submit_task is not None and not self._submit_task.done():
                self._submit_task.cancel()
                await asyncio.gather(self._submit_task, return_exceptions=True)
                self._submit_task = None

        async def action_quit_app(self) -> None:
            self.exit()

        async def action_interrupt_turn(self) -> None:
            if not self._pending:
                return
            if self._session.interrupt():
                self._set_status_text("Interrupting")

        async def action_insert_newline(self) -> None:
            if self._input_view is None or self.focused is not self._input_view:
                return
            self._input_view.insert("\n")
            self._resize_input(self._input_view)

        async def action_submit_prompt(self) -> None:
            if self._input_view is None:
                return

            prompt = self._input_view.text.rstrip()
            self._input_view.load_text("")
            self._resize_input(self._input_view)
            self._input_view.focus()

            if prompt.strip() == "":
                return
            if prompt.strip() in {"/quit", "/exit"}:
                self.exit()
                return

            if self._pending:
                queued_turn = _QueuedAskTurn(prompt=prompt)
                self._queued_turns.append(queued_turn)
                self._render_turn_queue()
                await self._dispatch_pending_queued_turns()
                return

            self._start_turn(prompt=prompt)

        def _start_turn(self, *, prompt: str) -> None:
            self._pending = True
            self._begin_active_assistant()
            self._status_started_at = time.monotonic()
            self._set_status_text("Working")
            self._render_feed()
            self._render_turn_queue()
            self._scroll_to_end()
            self._submit_task = asyncio.create_task(
                self._run_prompt(
                    prompt=prompt,
                )
            )

        def _begin_active_assistant(self) -> None:
            self._active_assistant_text = ""
            if self._active_assistant_header is not None:
                self._active_assistant_header.styles.display = "block"
            if self._active_assistant_body is not None:
                self._active_assistant_body.styles.display = "block"
                self._active_assistant_body.update("")
                self._active_assistant_stream = TextualMarkdown.get_stream(
                    self._active_assistant_body
                )
            self._render_active_assistant_header()

        async def _stop_active_assistant_stream(self) -> None:
            if self._active_assistant_stream is not None:
                await self._active_assistant_stream.stop()
                self._active_assistant_stream = None

        async def _run_prompt(
            self,
            *,
            prompt: str,
        ) -> None:
            try:
                await self._session.ask(
                    prompt=prompt,
                    on_message=self._handle_agent_message,
                )
            except asyncio.CancelledError:
                raise
            except RoomException as ex:
                if not _is_cancelled_turn_error(ex):
                    self._entries.append(_AskFeedEntry(role="error", text=str(ex)))
            except Exception as ex:
                self._entries.append(_AskFeedEntry(role="error", text=str(ex)))
            finally:
                await self._stop_active_assistant_stream()
                self._pending = False
                self._active_assistant_entry = None
                self._sync_session_messages()
                self._finalize_active_assistant()
                self._render_feed()
                self._status_started_at = None
                self._set_status_text(None)
                self._render_turn_queue()
                self._scroll_to_end()
                if self._input_view is not None:
                    self._input_view.focus()

        def _finalize_active_assistant(self) -> None:
            if self._active_assistant_header is not None:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
            if self._active_assistant_body is not None:
                self._active_assistant_body.styles.display = "none"
                self._active_assistant_body.update("")
            if self._active_assistant_text.strip() != "":
                self._entries.append(
                    _AskFeedEntry(
                        role=self._assistant_name,
                        text=self._active_assistant_text,
                    )
                )
            self._active_assistant_text = ""

        async def _dispatch_pending_queued_turns(self) -> None:
            for queued_turn in self._queued_turns:
                if queued_turn.sent:
                    continue
                message_id = self._session.steer(
                    prompt=queued_turn.prompt,
                    on_accepted=lambda queued_turn=queued_turn: (
                        self._accept_queued_turn(queued_turn)
                    ),
                    on_applied=lambda queued_turn=queued_turn: self._apply_queued_turn(
                        queued_turn
                    ),
                    on_rejected=lambda error, queued_turn=queued_turn: (
                        self._reject_queued_turn(
                            queued_turn=queued_turn,
                            error=error,
                        )
                    ),
                )
                if message_id is None:
                    break
                queued_turn.sent = True
                queued_turn.message_id = message_id
                self._render_turn_queue()

        def _accept_queued_turn(self, queued_turn: _QueuedAskTurn) -> None:
            queued_turn.accepted = True
            self._render_turn_queue()

        async def _apply_queued_turn(self, queued_turn: _QueuedAskTurn) -> None:
            if queued_turn not in self._queued_turns:
                return
            await self._stop_active_assistant_stream()
            self._finalize_active_assistant()
            self._queued_turns.remove(queued_turn)
            self._render_turn_queue()
            self._sync_session_messages()
            self._render_feed()
            if self._pending:
                self._begin_active_assistant()
            self._scroll_to_end()

        def _reject_queued_turn(
            self,
            *,
            queued_turn: _QueuedAskTurn,
            error: RoomException,
        ) -> None:
            if queued_turn in self._queued_turns:
                self._queued_turns.remove(queued_turn)
            self._entries.append(
                _AskFeedEntry(
                    role="error",
                    text=f"Unable to queue prompt: {error}",
                )
            )
            self._render_turn_queue()
            self._render_feed()
            self._scroll_to_end()

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if self._input_view is None or event.text_area is not self._input_view:
                return
            self._resize_input(event.text_area)

        async def _append_delta(self, text: str) -> None:
            self._active_assistant_text += text
            if self._active_assistant_stream is not None:
                await self._active_assistant_stream.write(text)
            self._scroll_to_end()

        async def _handle_agent_message(self, message: AgentMessage) -> None:
            if isinstance(message, TurnStarted):
                await self._dispatch_pending_queued_turns()
                return
            if isinstance(message, AgentThreadStatus):
                self._set_status_text(_thread_status_text(message.status))
                return
            if isinstance(message, AgentTextContentDelta):
                await self._append_delta(message.text)
                return
            if isinstance(message, AgentUsageUpdated):
                self._set_usage(message)

        def _resize_input(self, input_view: TextArea) -> None:
            if input_view.text == "":
                target_height = 1
            else:
                target_height = max(1, min(6, input_view.virtual_size.height))
            if target_height == self._input_height:
                self._sync_input_row_padding()
                return
            self._input_height = target_height
            input_view.styles.height = target_height
            self._sync_input_row_padding()

        def _sync_input_row_padding(self) -> None:
            if self._input_row is None:
                return
            if self._input_height <= 1:
                self._input_row.styles.padding = (1, 0, 1, 0)
            else:
                self._input_row.styles.padding = (0, 0, 0, 0)

        def _on_spinner_tick(self) -> None:
            self._sync_external_thread_state()
            if not (self._pending or self._external_thread_active):
                return
            self._status_gradient_offset = self._status_gradient_offset + 1
            self._render_status_line()

        def _scroll_to_end(self) -> None:
            if self._feed_scroll is not None:
                self._feed_scroll.anchor()
                self._feed_scroll.scroll_end(animate=False)

        def _set_status_text(self, status: str | None) -> None:
            self._status_text = status
            self._render_status_line()
            self._render_turn_queue()

        def _sync_external_thread_state(self) -> None:
            if not isinstance(self._session, _AskExternalThreadState):
                return

            status = self._session.thread_status_text
            labels = list(self._session.queued_message_labels)
            active = status is not None and status.strip() != ""
            status_changed = active != self._external_thread_active
            queue_changed = labels != self._external_queued_messages
            self._external_thread_active = active
            self._external_queued_messages = labels

            if self._pending:
                if status is not None and status.strip() != "":
                    status_changed = status_changed or status != self._status_text
                    self._status_text = status
            else:
                next_status = status if active else None
                status_changed = status_changed or next_status != self._status_text
                self._status_text = next_status

            if status_changed:
                self._render_status_line()
            if status_changed or queue_changed:
                self._render_turn_queue()
            self._sync_session_messages()

        def _sync_session_messages(self) -> None:
            if not isinstance(self._session, _AskExternalThreadState):
                return
            queued_message_ids = {
                queued_turn.message_id
                for queued_turn in self._queued_turns
                if queued_turn.message_id is not None
            }
            changed = False
            for message in self._session.messages:
                if message.message_id in self._rendered_session_message_ids:
                    continue
                if message.message_id in queued_message_ids:
                    continue
                self._rendered_session_message_ids.add(message.message_id)
                self._entries.append(
                    _AskFeedEntry(role=message.role, text=message.text)
                )
                changed = True
            if changed:
                self._render_feed()
                self._scroll_to_end()

        def _set_usage(self, usage_update: AgentUsageUpdated) -> None:
            total_tokens = usage_update.usage.get("total_tokens")
            if total_tokens is None:
                total_tokens = sum(usage_update.usage.values())
            self._usage_state = _AskUsageState(
                context_used_tokens=usage_update.context_window.used_tokens,
                context_total_tokens=usage_update.context_window.total_tokens,
                compaction_mode=usage_update.context_window.compaction_mode,
                compaction_threshold=usage_update.context_window.compaction_threshold,
                total_tokens=total_tokens,
            )
            self._render_session_meta()

        def _render_status_line(self) -> None:
            if self._status_view is None:
                return
            if (
                self._pending or self._external_thread_active
            ) and self._status_text is not None:
                self._status_view.update(
                    Group(
                        self._status_renderable(),
                        Text(" "),
                    )
                )
                return

            if len(self._entries) == 0:
                self._status_view.update(
                    Group(
                        Text(
                            "Ask a question to start a conversation.",
                            style="dim",
                        ),
                        Text(" "),
                    )
                )
                return

            self._status_view.update(
                Group(
                    Text(
                        "Waiting for user input",
                        style="dim",
                    ),
                    Text(" "),
                )
            )

        def _render_turn_queue(self) -> None:
            if self._queue_view is None:
                return

            def _normalized_label(value: str) -> str:
                return " ".join(value.split())

            local_labels = [queued_turn.prompt for queued_turn in self._queued_turns]
            queued_labels = [*local_labels]
            normalized_local_labels = {
                _normalized_label(label) for label in local_labels
            }
            for external_label in self._external_queued_messages:
                normalized_external_label = _normalized_label(external_label)
                if normalized_external_label in normalized_local_labels:
                    continue
                if any(
                    normalized_external_label.endswith(f": {local_label}")
                    for local_label in normalized_local_labels
                ):
                    continue
                queued_labels.append(external_label)

            if len(queued_labels) == 0:
                self._queue_view.styles.display = "none"
                self._queue_view.update("")
                return

            queue_lines = Text()
            queue_lines.append("Queued\n", style="bold #8aa6cf")
            for index, label in enumerate(queued_labels, start=1):
                normalized_prompt = " ".join(label.split())
                if len(normalized_prompt) > 72:
                    normalized_prompt = normalized_prompt[:69].rstrip() + "..."
                queue_lines.append(f"{index}. ", style="dim")
                queue_lines.append(normalized_prompt)
                if index < len(queued_labels):
                    queue_lines.append("\n")

            self._queue_view.styles.display = "block"
            self._queue_view.update(queue_lines)

        def _render_session_meta(self) -> None:
            if self._session_meta_view is None:
                return
            table = Table.grid(expand=True)
            table.add_column(ratio=1)
            table.add_column(justify="right", no_wrap=True)
            table.add_row(
                Text.assemble(
                    ("model ", "bold #9aa5b8"),
                    (model, "#cfd3dc"),
                    ("  •  ", "#5f6778"),
                    ("cwd ", "bold #9aa5b8"),
                    (self._session.current_working_directory, "#cfd3dc"),
                ),
                self._usage_footer_text(),
            )
            self._session_meta_view.update(table)

        def _usage_footer_text(self) -> Text:
            usage_state = self._usage_state
            if usage_state is None:
                return Text("")

            context_label = _format_token_count(usage_state.context_used_tokens)
            context_limit_tokens = usage_state.compaction_threshold
            if context_limit_tokens is None:
                context_limit_tokens = usage_state.context_total_tokens
            if context_limit_tokens is not None:
                context_label = (
                    f"{context_label}/{_format_token_count(context_limit_tokens)}"
                )

            compaction_label = ""
            if usage_state.compaction_mode is not None:
                compaction_label = usage_state.compaction_mode

            if compaction_label != "":
                return Text.assemble(
                    ("compaction ", "bold #9aa5b8"),
                    (compaction_label, "#cfd3dc"),
                    ("  •  ", "#5f6778"),
                    ("context ", "bold #9aa5b8"),
                    (context_label, "#cfd3dc"),
                    ("  •  ", "#5f6778"),
                    ("tokens ", "bold #9aa5b8"),
                    (_format_token_count(usage_state.total_tokens), "#cfd3dc"),
                )

            return Text.assemble(
                ("context ", "bold #9aa5b8"),
                (context_label, "#cfd3dc"),
                ("  •  ", "#5f6778"),
                ("tokens ", "bold #9aa5b8"),
                (_format_token_count(usage_state.total_tokens), "#cfd3dc"),
            )

        def _render_active_assistant_header(self) -> None:
            if self._active_assistant_header is None:
                return
            if not self._pending:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
                return
            self._active_assistant_header.styles.display = "block"
            self._active_assistant_header.update(
                Text(
                    self._assistant_name,
                    style="bold cyan",
                )
            )

        def _status_renderable(self) -> Text:
            elapsed_label = "0s"
            if self._status_started_at is not None:
                elapsed_seconds = max(
                    0, int(time.monotonic() - self._status_started_at)
                )
                minutes, seconds = divmod(elapsed_seconds, 60)
                hours, minutes = divmod(minutes, 60)
                if hours > 0:
                    elapsed_label = f"{hours}h {minutes}m"
                elif minutes > 0:
                    elapsed_label = f"{minutes}m {seconds}s"
                else:
                    elapsed_label = f"{seconds}s"

            label = f"• {self._status_text} ({elapsed_label} • esc to interrupt)"
            if label == "":
                return Text("")

            palette = [
                "#6f7b90",
                "#8aa6cf",
                "#c4dbff",
                "#f4f8ff",
                "#c4dbff",
                "#8aa6cf",
                "#6f7b90",
            ]
            overlay_width = max(6, min(14, len(label)))
            text = Text()
            for index, character in enumerate(label):
                style = "#8892a3"
                offset = (index - self._status_gradient_offset) % len(label)
                if offset < overlay_width:
                    palette_index = int(
                        (offset / max(overlay_width - 1, 1)) * (len(palette) - 1)
                    )
                    color = palette[palette_index]
                    style = f"bold {color}"
                text.append(character, style=style)
            return text

        def _render_feed(self) -> None:
            if self._feed_view is None:
                return

            if self._rendered_entry_count >= len(self._entries):
                return

            for entry in self._entries[self._rendered_entry_count :]:
                self._feed_view.mount(self._render_entry(entry))
                self._rendered_entry_count += 1

        def _render_entry(self, entry: _AskFeedEntry) -> Vertical:
            header_style = "bold cyan"
            body_style = ""
            entry_classes = "feed-entry"
            if entry.role == "you":
                header_style = "bold white"
                body_style = "white"
                entry_classes += " feed-entry--you"
            elif entry.role == "error":
                header_style = "bold red"
                body_style = "red"
                entry_classes += " feed-entry--error"

            body_text = entry.text if entry.text.strip() != "" else " "
            if body_text.strip() != "":
                body_widget = TextualMarkdown(
                    body_text,
                    classes="feed-entry-body feed-entry-markdown",
                )
            else:
                body_widget = Static(
                    Text(
                        body_text,
                        style=body_style,
                        no_wrap=False,
                        overflow="fold",
                    ),
                    classes="feed-entry-body",
                )

            return Vertical(
                Static(
                    Text(entry.role, style=header_style),
                    classes="feed-entry-header",
                ),
                body_widget,
                classes=entry_classes,
            )

    async def _run_app(session_arg: Any) -> None:
        app = _AskTextualApp(
            session=session_arg,
            title=title,
            assistant_name=assistant_name,
        )
        token = active_app.set(app)
        try:
            await app.run_async()
        except KeyboardInterrupt:
            return
        finally:
            active_app.reset(token)

    with _suppress_ask_process_logs():
        if session is not None:
            await _run_app(session)
            return

        if llm_adapter is None:
            raise TypeError("llm_adapter is required when session is not supplied")

        async with _AskSession(
            model=model,
            llm_adapter=llm_adapter,
            interactive=True,
        ) as created_session:
            await _run_app(created_session)


@app.async_command("ask", help="Send a one-shot LLM prompt.")
async def ask(
    *,
    project_id: ProjectIdOption,
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Prompt to send to the LLM"),
    ] = None,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format for non-interactive responses.",
            case_sensitive=False,
        ),
    ] = "text",
    model: Annotated[
        str,
        typer.Option("--model", help="Name of the LLM model to use"),
    ] = _DEFAULT_ASK_MODEL,
) -> None:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    access_token = await _resolve_ask_access_token()
    if access_token is None:
        click.echo(
            "No MeshAgent token or OAuth access token available. "
            "Set MESHAGENT_TOKEN or run `meshagent auth login` first."
        )
        raise typer.Exit(1)

    llm_adapter = _build_ask_adapter(
        model=model,
        project_id=resolved_project_id,
        access_token=access_token,
    )
    if _should_launch_tui(
        message=message,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
    ):
        await _run_ask_tui(
            model=model,
            llm_adapter=llm_adapter,
        )
        return

    if message is None:
        click.echo(
            "Prompt required. Pass `-m/--message`, or run in a TTY for interactive mode."
        )
        raise typer.Exit(1)

    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "markdown"}:
        click.echo(f"Unsupported format: {format}. Expected one of: text, markdown.")
        raise typer.Exit(1)

    if normalized_format == "markdown":
        result = await _run_ask_process(
            prompt=message,
            model=model,
            llm_adapter=llm_adapter,
        )
        Console().print(Markdown(result))
        return

    wrote_output = False

    def _write_message(message: AgentMessage) -> None:
        nonlocal wrote_output
        if isinstance(message, AgentTextContentDelta):
            wrote_output = True
            click.echo(message.text, nl=False)

    result = await _run_ask_process(
        prompt=message,
        model=model,
        llm_adapter=llm_adapter,
        on_message=_write_message,
    )
    if wrote_output:
        click.echo()
    else:
        click.echo(result)


ask_command = async_typer.get_command(app, materialize_lazy=True)
