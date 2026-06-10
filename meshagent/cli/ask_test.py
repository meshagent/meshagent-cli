import asyncio
import base64
import io
import subprocess
from pathlib import Path

import pytest
import typer
from typer._click.testing import CliRunner
from PIL import Image

from meshagent.agents import AgentSessionContext
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.messages import (
    AGENT_EVENT_THREAD_EVENT,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_TEXT_CONTENT_STARTED,
    AGENT_EVENT_TOOL_CALL_ENDED,
    AGENT_EVENT_TOOL_CALL_STARTED,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AGENT_EVENT_TURN_START_REJECTED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_MESSAGE_THREAD_START,
    AgentError,
    AgentFileContent,
    AgentGeneratedImage,
    AgentThreadEvent,
    AgentThreadStatus,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentTextContentStarted,
    AgentToolCallEnded,
    AgentToolCallStarted,
    StartThread,
    TurnEnded,
    TurnStart,
    TurnStarted,
)
from meshagent.api import Participant, RoomException
from meshagent.cli import async_typer
from meshagent.cli import ask as ask_module
from meshagent.cli import cli as cli_module
from meshagent.cli.preamble_rules import DEFAULT_PREAMBLE_RULE
from meshagent.openai import OpenAIResponsesAdapter


class _PromptSendSession:
    def __init__(self) -> None:
        self.has_thread_path = True
        self.sent_text: str | None = None
        self.turn_finished = asyncio.Event()

    async def send_text(self, *, text: str, attachments=None) -> str:
        del attachments
        self.sent_text = text
        return "message-1"


class _FakeAskAdapter(LLMAdapter[object]):
    def default_model(self) -> str:
        return "gpt-5.5"

    def create_session(self, *, usage_callback=None) -> AgentSessionContext:
        return AgentSessionContext()

    async def create_response(
        self,
        *,
        context: AgentSessionContext,
        caller: Participant,
        toolkits: list,
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of=None,
        tool_choice=None,
        options=None,
    ) -> object:
        del caller
        del toolkits
        del output_schema
        del steering_callback
        del on_behalf_of
        del tool_choice
        del options
        assert model == "gpt-5.5"
        thread_id = str(context.metadata["thread_id"])
        turn_id = str(context.metadata["turn_id"])
        assert event_handler is not None
        event_handler(
            AgentTextContentStarted(
                type=AGENT_EVENT_TEXT_CONTENT_STARTED,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id="item-1",
            )
        )
        event_handler(
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id="item-1",
                text="hello",
            )
        )
        event_handler(
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id="item-1",
                text=" world",
            )
        )
        event_handler(
            AgentTextContentEnded(
                type=AGENT_EVENT_TEXT_CONTENT_ENDED,
                thread_id=thread_id,
                turn_id=turn_id,
                item_id="item-1",
            )
        )
        return {"ok": True}


class _FakeStatusAskAdapter(_FakeAskAdapter):
    async def create_response(
        self,
        *,
        context: AgentSessionContext,
        caller: Participant,
        toolkits: list,
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of=None,
        tool_choice=None,
        options=None,
    ) -> object:
        assert event_handler is not None
        event_handler(
            {
                "type": "agent.event",
                "state": "in_progress",
                "headline": "Searching files",
            }
        )
        return await super().create_response(
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


class _FakeTTY:
    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


async def _local_chat_session(
    send_message,
) -> tuple[ask_module.LocalChatClient, ask_module._AgentMessageSession]:
    events: asyncio.Queue[ask_module.Message] = asyncio.Queue()

    def _send(message: ask_module.Message) -> None:
        if message.data.type == "meshagent.agent.thread.close":
            return
        send_message(message.data, events)

    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=_send,
        events=events,
    )
    await client.start()
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
    )
    return client, session


def _queue_agent_event(
    events: asyncio.Queue[ask_module.Message],
    message,
) -> None:
    events.put_nowait(ask_module.Message(data=message))


def test_build_ask_instructions_includes_preamble_rule_by_default() -> None:
    instructions = ask_module._build_ask_instructions(
        current_working_directory="/tmp/project"
    )

    assert DEFAULT_PREAMBLE_RULE in instructions


def test_build_ask_instructions_can_disable_preamble_rule() -> None:
    instructions = ask_module._build_ask_instructions(
        current_working_directory="/tmp/project",
        preamble_rule=False,
    )

    assert DEFAULT_PREAMBLE_RULE not in instructions


def test_build_ask_instructions_prefers_meshagent_deployment() -> None:
    instructions = ask_module._build_ask_instructions(
        current_working_directory="/tmp/project",
    )

    assert "deploy with meshagent" in instructions
    assert "third-party deployment services" in instructions


def test_build_ask_instructions_prefers_interactive_create_mode() -> None:
    instructions = ask_module._build_ask_instructions(
        current_working_directory="/tmp/project",
    )

    assert "meshagent create has an interactive mode" in instructions
    assert "creating sample projects" in instructions


def test_build_ask_instructions_includes_create_samples_path() -> None:
    instructions = ask_module._build_ask_instructions(
        current_working_directory="/tmp/project",
        create_samples_path="/tmp/create-samples",
    )

    assert "meshagent create" in instructions
    assert "/tmp/create-samples" in instructions
    assert "Grep and read" in instructions


@pytest.mark.asyncio
async def test_build_ask_toolkits_mounts_create_samples_read_only(tmp_path) -> None:
    project_dir = tmp_path / "project"
    samples_dir = tmp_path / "create-samples"
    project_dir.mkdir()
    samples_dir.mkdir()
    sample_file = samples_dir / "sample.py"
    sample_file.write_text("print('hello from sample')\n", encoding="utf-8")

    toolkits = ask_module._build_ask_toolkits(
        model="gpt-5.5",
        current_working_directory=str(project_dir),
        create_samples_path=str(samples_dir),
    )
    storage = next(toolkit for toolkit in toolkits if toolkit.name == "storage")

    content = await storage.read_file(path=str(sample_file))
    assert content.data == b"print('hello from sample')\n"

    with pytest.raises(RoomException, match="read-only"):
        await storage.write_text(
            path=str(sample_file),
            text="print('edited')\n",
            overwrite=True,
        )


def test_ask_feed_previous_participant_role_skips_event_rows() -> None:
    assert (
        ask_module._ask_feed_previous_participant_role(
            ["you", "chatbot", "event", "event"],
            before_index=4,
        )
        == "chatbot"
    )


def test_ask_feed_previous_participant_role_keeps_errors_as_breaks() -> None:
    assert (
        ask_module._ask_feed_previous_participant_role(
            ["chatbot", "event", "error", "event"],
            before_index=4,
        )
        == "error"
    )


def test_ask_text_needs_markdown_ignores_plain_commentary() -> None:
    assert not ask_module._ask_text_needs_markdown(
        "The output directory is missing, so I will save the report nearby."
    )


def test_ask_text_needs_markdown_keeps_structured_content() -> None:
    assert ask_module._ask_text_needs_markdown("Generated `pie_chart.svg`.")
    assert ask_module._ask_text_needs_markdown("First line\nSecond line")


def test_format_ask_tool_call_entry_promotes_raw_tool_log_headline() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="openai",
        tool="shell",
        arguments=None,
        logs=[
            "\n".join(
                [
                    "Created /src/python_generated_report.html",
                    "-rw-r--r-- 1 root root 5311 May  8 07:35 /src/python_generated_report.html",
                    "<!doctype html>",
                    '<html lang="en">',
                    "<body>",
                ]
            )
        ],
        error_message=None,
    )

    assert text.splitlines() == [
        "Created /src/python_generated_report.html",
        "-rw-r--r-- 1 root root 5311 May  8 07:35 /src/python_generated_report.html",
        "<!doctype html>",
        '<html lang="en">',
    ]
    assert "Ran openai: shell" not in text


def test_format_ask_tool_call_entry_prefixes_path_only_log_headline() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="openai",
        tool="shell",
        arguments=None,
        logs=["/tmp/pie_chart.svg\n/data/pie_chart.svg"],
        error_message=None,
    )

    assert text.splitlines()[0] == "Output: /tmp/pie_chart.svg"


def test_format_ask_tool_call_entry_uses_friendly_openai_shell_fallback() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="openai",
        tool="shell",
        arguments=None,
        logs=[],
        error_message=None,
    )

    assert text == "Explored"


def test_thread_event_entry_text_uses_friendly_openai_shell_fallback() -> None:
    assert ask_module._friendly_ask_thread_event_text("Ran openai: shell") == "Explored"


def test_format_ask_tool_call_entry_keeps_parsed_summary_headline() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="shell",
        tool="shell",
        arguments={"command": "rg food"},
        logs=["match 1\nmatch 2\nmatch 3\nmatch 4\nmatch 5"],
        error_message=None,
    )

    assert text.splitlines() == [
        "Explored",
        "  └ Search food",
        "match 1",
        "match 2",
        "match 3",
        "match 4",
    ]


def test_merge_ask_tool_call_arguments_delta_restores_streamed_shell_command() -> None:
    arguments = ask_module._merge_ask_tool_call_arguments_delta(
        tool="shell",
        arguments=None,
        delta_text='{"action":{"command":"ls /data"}}',
    )

    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="openai",
        tool="shell",
        arguments=arguments,
        logs=[],
        error_message=None,
    )

    assert text.splitlines() == ["Explored", "  └ List data"]


def test_format_ask_tool_call_entry_uses_ended_tool_metadata_without_started_state() -> (
    None
):
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="openai",
        tool="shell",
        arguments={"action": {"command": "ls /data"}},
        logs=[],
        error_message=None,
    )

    assert text.splitlines() == ["Explored", "  └ List data"]


def test_format_ask_tool_call_entry_cleans_exception_error_message() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="chat",
        tool="attach_file",
        arguments={"path": "src/candy_report.md"},
        logs=[],
        error_message=(
            "meshagent.api.room_server_client.RoomException: "
            "attach_file could not find a room file at src/candy_report.md"
        ),
    )

    assert text.splitlines() == [
        "Failed: Attached file: src/candy_report.md",
        "attach_file could not find a room file at src/candy_report.md",
    ]


def test_format_ask_tool_call_entry_hides_traceback_logs_for_failed_tools() -> None:
    text = ask_module._format_ask_tool_call_entry_text(
        toolkit="chat",
        tool="attach_file",
        arguments={"path": "src/candy_report.md"},
        logs=[
            "\n".join(
                [
                    "Traceback (most recent call last):",
                    'File "/src/chat_channel.py", line 1159, in attach_file',
                    'f"attach_file could not find a room file at {room_storage_path}"',
                    "meshagent.api.room_server_client.RoomException: attach_file could not find a room file at src/candy_report.md",
                ]
            )
        ],
        error_message=(
            "meshagent.api.room_server_client.RoomException: "
            "attach_file could not find a room file at src/candy_report.md"
        ),
    )

    assert text.splitlines() == [
        "Failed: Attached file: src/candy_report.md",
        "attach_file could not find a room file at src/candy_report.md",
    ]
    assert "meshagent.api.room_server_client.RoomException" not in text


@pytest.mark.asyncio
async def test_run_ask_process_returns_text_output() -> None:
    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
    )

    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_ask_process_streams_text_deltas() -> None:
    deltas: list[str] = []

    def _on_message(message) -> None:
        if isinstance(message, ask_module.AgentTextContentDelta):
            deltas.append(message.text)

    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
        on_message=_on_message,
    )

    assert result == "hello world"
    assert deltas == ["hello", " world"]


@pytest.mark.asyncio
async def test_run_ask_process_awaits_async_text_delta_callback() -> None:
    deltas: list[str] = []

    async def _on_message(message) -> None:
        if isinstance(message, ask_module.AgentTextContentDelta):
            deltas.append(message.text)

    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
        on_message=_on_message,
    )

    assert result == "hello world"
    assert deltas == ["hello", " world"]


@pytest.mark.asyncio
async def test_ask_session_reuses_process_for_multiple_prompts() -> None:
    async with ask_module._AskSession(
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
    ) as session:
        first = await session.ask(prompt="first")
        second = await session.ask(prompt="second")

        assert first == "hello world"
        assert second == "hello world"


@pytest.mark.asyncio
async def test_ask_session_emits_custom_event_status_messages() -> None:
    statuses: list[str | None] = []

    async with ask_module._AskSession(
        model="gpt-5.5",
        llm_adapter=_FakeStatusAskAdapter(),
    ) as session:
        result = await session.ask(
            prompt="hello",
            on_message=lambda message: (
                statuses.append(message.status)
                if isinstance(message, AgentThreadStatus)
                else None
            ),
        )
        await asyncio.sleep(0)

    assert result == "hello world"
    assert "Searching files" in statuses


@pytest.mark.asyncio
async def test_agent_message_session_orders_inputs_by_accepted_events() -> None:
    def _send(payload, events: asyncio.Queue[ask_module.Message]) -> None:
        assert isinstance(payload, TurnStart)
        _queue_agent_event(
            events,
            ask_module.TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                thread_id=payload.thread_id,
                source_message_id="remote-message-1",
                sender_name="remote-user",
                content=[ask_module.AgentTextContent(type="text", text="remote first")],
            ),
        )
        _queue_agent_event(
            events,
            ask_module.TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                thread_id=payload.thread_id,
                source_message_id=payload.message_id,
            ),
        )
        _queue_agent_event(
            events,
            TurnStarted(
                type=AGENT_EVENT_TURN_STARTED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                source_message_id=payload.message_id,
            ),
        )
        _queue_agent_event(
            events,
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                item_id="text-1",
                text="response",
            ),
        )
        _queue_agent_event(
            events,
            TurnEnded(
                type=AGENT_EVENT_TURN_ENDED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                error=None,
            ),
        )

    client, session = await _local_chat_session(_send)
    try:
        result = await session.ask(prompt="local second")
    finally:
        await client.close()

    assert result == "response"
    assert [(message.role, message.text) for message in session.messages] == [
        ("remote-user", "remote first"),
        ("you", "local second"),
        ("assistant", "response"),
    ]


@pytest.mark.asyncio
async def test_agent_message_session_sends_selected_output_modalities() -> None:
    sent_payload: TurnStart | None = None

    def _send(payload, events: asyncio.Queue[ask_module.Message]) -> None:
        nonlocal sent_payload
        assert isinstance(payload, TurnStart)
        sent_payload = payload
        _queue_agent_event(
            events,
            TurnStarted(
                type=AGENT_EVENT_TURN_STARTED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                source_message_id=payload.message_id,
            ),
        )
        _queue_agent_event(
            events,
            TurnEnded(
                type=AGENT_EVENT_TURN_ENDED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                error=None,
            ),
        )

    client, session = await _local_chat_session(_send)
    session.set_output_modalities(("audio",))

    try:
        result = await session.ask(prompt="local second")
    finally:
        await client.close()

    assert result == ""
    assert sent_payload is not None
    assert sent_payload.output_modalities == ["audio"]


@pytest.mark.asyncio
async def test_agent_message_session_eagerly_records_local_start_message() -> None:
    session: ask_module._AgentMessageSession | None = None

    def _send(payload, events: asyncio.Queue[ask_module.Message]) -> None:
        assert isinstance(payload, TurnStart)
        assert session is not None
        assert [(message.role, message.text) for message in session.messages] == [
            ("you", "local second"),
        ]
        _queue_agent_event(
            events,
            ask_module.TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                thread_id=payload.thread_id,
                source_message_id=payload.message_id,
                content=[ask_module.AgentTextContent(type="text", text="local second")],
            ),
        )
        _queue_agent_event(
            events,
            TurnStarted(
                type=AGENT_EVENT_TURN_STARTED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                source_message_id=payload.message_id,
            ),
        )
        _queue_agent_event(
            events,
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                item_id="text-1",
                text="response",
            ),
        )
        _queue_agent_event(
            events,
            TurnEnded(
                type=AGENT_EVENT_TURN_ENDED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                error=None,
            ),
        )

    client, session = await _local_chat_session(_send)

    try:
        result = await session.ask(prompt="local second")
    finally:
        await client.close()

    assert result == "response"
    assert [(message.role, message.text) for message in session.messages] == [
        ("you", "local second"),
        ("assistant", "response"),
    ]


@pytest.mark.asyncio
async def test_ask_pending_turn_start_renders_inline_when_thread_is_idle() -> None:
    sent_payloads: list[object] = []
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=sent_payloads.append,
        events=asyncio.Queue(),
    )
    session = client.thread_session

    await session.send_text(text="queued prompt", message_id="message-1")

    assert ask_module._ask_inline_pending_message_ids(
        session,
        external_thread_active=False,
    ) == {"message-1"}
    assert (
        ask_module._ask_queued_message_labels(
            session,
            external_thread_active=False,
        )
        == []
    )
    assert [(message.message_id, message.type) for message in session.messages] == [
        ("message-1", ask_module.AGENT_MESSAGE_TURN_START)
    ]


@pytest.mark.asyncio
async def test_ask_pending_turn_start_stays_queued_when_thread_is_active() -> None:
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = client.thread_session

    await session.send_text(text="queued prompt", message_id="message-1")

    assert (
        ask_module._ask_inline_pending_message_ids(
            session,
            external_thread_active=True,
        )
        == set()
    )
    assert ask_module._ask_queued_message_labels(
        session,
        external_thread_active=True,
    ) == ["user: queued prompt"]


def test_ask_conversation_messages_render_new_thread_start_message() -> None:
    message = StartThread(
        type=AGENT_MESSAGE_THREAD_START,
        content=[ask_module.AgentTextContent(type="text", text="hello")],
    )

    rendered = ask_module._ask_conversation_messages_from_agent_messages(
        [message],
        local_participant_name=None,
    )

    assert len(rendered) == 1
    assert rendered[0].message_id == message.message_id
    assert rendered[0].role == "you"
    assert rendered[0].text == "hello"


def test_ask_conversation_messages_render_failed_turn_error() -> None:
    message = TurnEnded(
        type=AGENT_EVENT_TURN_ENDED,
        thread_id="thread-1",
        turn_id="turn-1",
        error=AgentError(
            code="RoomException",
            message="Error from OpenAI websocket: unknown parameter",
        ),
    )

    rendered = ask_module._ask_conversation_messages_from_agent_messages(
        [message],
        local_participant_name=None,
    )

    assert len(rendered) == 1
    assert rendered[0].message_id == message.message_id
    assert rendered[0].role == "error"
    assert rendered[0].text == "Error from OpenAI websocket: unknown parameter"


@pytest.mark.asyncio
async def test_agent_message_session_emits_intermediate_agent_events() -> None:
    def _send(payload, events: asyncio.Queue[ask_module.Message]) -> None:
        assert isinstance(payload, TurnStart)
        _queue_agent_event(
            events,
            ask_module.TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                thread_id=payload.thread_id,
                source_message_id=payload.message_id,
                content=[ask_module.AgentTextContent(type="text", text="local prompt")],
            ),
        )
        _queue_agent_event(
            events,
            TurnStarted(
                type=AGENT_EVENT_TURN_STARTED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                source_message_id=payload.message_id,
            ),
        )
        _queue_agent_event(
            events,
            AgentToolCallStarted(
                type=AGENT_EVENT_TOOL_CALL_STARTED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                item_id="tool-1",
                toolkit="shell",
                tool="shell",
                arguments={"command": "ruff check"},
            ),
        )
        _queue_agent_event(
            events,
            AgentThreadEvent(
                type=AGENT_EVENT_THREAD_EVENT,
                thread_id=payload.thread_id,
                event={"headline": "Ran ruff check"},
            ),
        )
        _queue_agent_event(
            events,
            AgentToolCallEnded(
                type=AGENT_EVENT_TOOL_CALL_ENDED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                item_id="tool-1",
                error=None,
            ),
        )
        _queue_agent_event(
            events,
            TurnEnded(
                type=AGENT_EVENT_TURN_ENDED,
                thread_id=payload.thread_id,
                turn_id="turn-1",
                error=None,
            ),
        )

    emitted: list[object] = []
    client, session = await _local_chat_session(_send)

    try:
        result = await session.ask(prompt="local prompt", on_message=emitted.append)
    finally:
        await client.close()

    assert result == ""
    assert any(isinstance(message, AgentToolCallStarted) for message in emitted)
    assert any(isinstance(message, AgentThreadEvent) for message in emitted)
    assert any(isinstance(message, AgentToolCallEnded) for message in emitted)


@pytest.mark.asyncio
async def test_agent_message_session_raises_turn_start_rejection() -> None:
    def _send(payload, events: asyncio.Queue[ask_module.Message]) -> None:
        assert isinstance(payload, TurnStart)
        _queue_agent_event(
            events,
            ask_module.TurnStartRejected(
                type=AGENT_EVENT_TURN_START_REJECTED,
                thread_id=payload.thread_id,
                source_message_id=payload.message_id,
                error=AgentError(
                    message="dataset thread storage requires a dataset:// thread id",
                    code="thread_process_creation_failed",
                ),
            ),
        )

    client, session = await _local_chat_session(_send)

    try:
        with pytest.raises(RoomException) as exc_info:
            await session.ask(prompt="local second")
    finally:
        await client.close()

    assert exc_info.value.code == "thread_process_creation_failed"
    assert (
        str(exc_info.value) == "dataset thread storage requires a dataset:// thread id"
    )


def test_agent_message_session_labels_loaded_local_participant_messages_as_you() -> (
    None
):
    events: asyncio.Queue[ask_module.Message] = asyncio.Queue()
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=events,
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
        local_participant_name="local-user",
    )

    session.add_agent_message(
        ask_module.TurnStart(
            type=ask_module.AGENT_MESSAGE_TURN_START,
            thread_id="/threads/test.thread",
            message_id="local-message",
            sender_name="local-user",
            content=[ask_module.AgentTextContent(type="text", text="local prompt")],
        )
    )
    session.add_agent_message(
        ask_module.TurnStart(
            type=ask_module.AGENT_MESSAGE_TURN_START,
            thread_id="/threads/test.thread",
            message_id="remote-message",
            sender_name="remote-user",
            content=[ask_module.AgentTextContent(type="text", text="remote prompt")],
        )
    )

    assert [(message.role, message.text) for message in session.messages] == [
        ("you", "local prompt"),
        ("remote-user", "remote prompt"),
    ]


def test_agent_message_session_labels_remote_sent_messages_with_sender_name() -> None:
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
        local_participant_name="local-user",
    )

    session.add_agent_message(
        ask_module.TurnStart(
            type=ask_module.AGENT_MESSAGE_TURN_START,
            thread_id="/threads/test.thread",
            message_id="remote-message",
            sender_name="remote-user",
            content=[ask_module.AgentTextContent(type="text", text="remote prompt")],
        )
    )

    assert [(message.role, message.text) for message in session.messages] == [
        ("remote-user", "remote prompt"),
    ]


def test_agent_thread_status_accepts_missing_status_for_clear_events() -> None:
    message = AgentThreadStatus.model_validate(
        {
            "type": AGENT_EVENT_THREAD_STATUS,
            "thread_id": "/threads/test.thread",
        }
    )

    assert message.status is None


def test_ask_thread_status_feed_text_formats_active_status() -> None:
    assert (
        ask_module._ask_thread_status_feed_text("Searching files")
        == "• Searching files"
    )
    assert ask_module._ask_thread_status_feed_text("   ") is None
    assert ask_module._ask_thread_status_feed_text(None) is None


def test_sync_status_timer_started_at_starts_for_external_active_status() -> None:
    assert (
        ask_module._sync_status_timer_started_at(
            started_at=None,
            active=True,
            pending=False,
            now=42.0,
        )
        == 42.0
    )


def test_sync_status_timer_started_at_preserves_running_timer() -> None:
    assert (
        ask_module._sync_status_timer_started_at(
            started_at=12.0,
            active=True,
            pending=False,
            now=42.0,
        )
        == 12.0
    )


def test_sync_status_timer_started_at_clears_inactive_external_status() -> None:
    assert (
        ask_module._sync_status_timer_started_at(
            started_at=12.0,
            active=False,
            pending=False,
            now=42.0,
        )
        is None
    )


def test_format_agent_thread_status_text_includes_change_counts() -> None:
    assert (
        ask_module._format_agent_thread_status_text(
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id="/threads/test.thread",
                status="Writing src/app.py",
                lines_added=1200,
                lines_removed=34,
            )
        )
        == "Writing src/app.py +1,200 -34"
    )


def test_format_agent_thread_status_text_includes_total_bytes() -> None:
    assert (
        ask_module._format_agent_thread_status_text(
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id="/threads/test.thread",
                status="Reading bundle",
                total_bytes=2048,
            )
        )
        == "Reading bundle 2,048 bytes"
    )


def test_ask_thread_status_feed_text_formats_agent_status_metadata() -> None:
    assert (
        ask_module._ask_thread_status_feed_text(
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id="/threads/test.thread",
                status="Writing src/app.py",
                lines_added=1200,
                lines_removed=34,
            )
        )
        == "• Writing src/app.py +1,200 -34"
    )


def _png_bytes(color: str) -> bytes:
    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(image_buffer, format="PNG")
    return image_buffer.getvalue()


def _png_data_uri(color: str) -> str:
    image_data = _png_bytes(color)
    return f"data:image/png;base64,{base64.b64encode(image_data).decode('ascii')}"


def _pdf_data_uri() -> str:
    pdf_data = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    return f"data:application/pdf;base64,{base64.b64encode(pdf_data).decode('ascii')}"


def test_agent_message_session_preserves_image_generation_data_uri() -> None:
    image_uri = _png_data_uri("red")
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
    )

    session.add_agent_message(
        ask_module.AgentImageGenerationCompleted(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            images=[AgentGeneratedImage(uri=image_uri, mime_type="image/png")],
        )
    )

    assert len(session.messages) == 1
    assert session.messages[0].role == "assistant"
    assert session.messages[0].kind == "image"
    assert session.messages[0].text == ""
    assert session.messages[0].attachment_uris == (image_uri,)


def test_agent_message_session_preserves_image_generation_dataset_uri() -> None:
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
    )

    session.add_agent_message(
        ask_module.AgentImageGenerationCompleted(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            images=[
                AgentGeneratedImage(
                    uri="dataset://agents/demo/images?id=image-1",
                    mime_type="image/png",
                )
            ],
        )
    )

    assert len(session.messages) == 1
    assert session.messages[0].role == "assistant"
    assert session.messages[0].kind == "image"
    assert session.messages[0].text == ""
    assert session.messages[0].attachment_uris == (
        "dataset://agents/demo/images?id=image-1",
    )


def test_agent_message_session_preserves_partial_image_generation_dataset_uri() -> None:
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
    )

    session.add_agent_message(
        ask_module.AgentImageGenerationPartial(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            image=AgentGeneratedImage(
                uri="dataset://agents/demo/images?id=image-1",
                mime_type="image/png",
            ),
        )
    )

    assert len(session.messages) == 1
    assert session.messages[0].role == "assistant"
    assert session.messages[0].kind == "image"
    assert session.messages[0].text == ""
    assert session.messages[0].attachment_uris == (
        "dataset://agents/demo/images?id=image-1",
    )


def test_agent_message_session_keeps_latest_image_generation_preview_or_final() -> None:
    first_uri = _png_data_uri("red")
    latest_uri = _png_data_uri("blue")
    final_uri = _png_data_uri("green")
    client = ask_module.LocalChatClient(
        thread_path="/threads/test.thread",
        send_message=lambda message: None,
        events=asyncio.Queue(),
    )
    session = ask_module._AgentMessageSession(
        client=client.thread_session,
        model=None,
    )

    session.add_agent_message(
        ask_module.AgentImageGenerationPartial(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            image=AgentGeneratedImage(uri=first_uri, mime_type="image/png"),
        )
    )
    first_preview = session.messages[0].attachment_uris
    session.add_agent_message(
        ask_module.AgentImageGenerationPartial(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            image=AgentGeneratedImage(uri=latest_uri, mime_type="image/png"),
        )
    )
    latest_preview = session.messages[0].attachment_uris
    session.add_agent_message(
        ask_module.AgentImageGenerationCompleted(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            images=[AgentGeneratedImage(uri=final_uri, mime_type="image/png")],
        )
    )

    assert len(session.messages) == 1
    assert session.messages[0].kind == "image"
    assert first_preview != latest_preview
    assert session.messages[0].attachment_uris not in {first_preview, latest_preview}


class _FakeImageDatasetRows:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict]:
        return self._rows


class _FakeImageDatasets:
    async def search(self, **kwargs):
        assert kwargs == {
            "table": "images",
            "namespace": ["agents", "demo"],
            "where": {"id": "image-1"},
            "limit": 1,
            "select": ["data", "mime_type"],
        }
        return _FakeImageDatasetRows(
            [{"data": _png_bytes("green"), "mime_type": "image/png"}]
        )


class _FakeImageRoom:
    def __init__(self) -> None:
        self.datasets = _FakeImageDatasets()


@pytest.mark.asyncio
async def test_ascii_image_renderer_loads_dataset_uri_from_image_dataset_client() -> (
    None
):
    image_dataset_client = ask_module.ImageDatasetClient(_FakeImageRoom().datasets)

    image_ascii = await ask_module._ascii_image_from_uri_async(
        "dataset://agents/demo/images?id=image-1",
        image_dataset_client=image_dataset_client,
    )

    assert image_ascii is not None
    assert image_ascii.strip() != ""
    assert "\x1b[" in image_ascii


def test_image_preview_from_record_normalizes_to_png() -> None:
    record = ask_module.ImageDatasetRecord(
        data=_png_bytes("blue"),
        mime_type="image/png",
    )

    image = ask_module._image_preview_from_record(record, columns=12)

    assert image is not None
    assert image.data.startswith(b"\x89PNG")
    assert image.columns == 12
    assert image.rows >= 1
    assert image.width_px >= 1
    assert image.height_px >= 1


def test_image_preview_caps_rows() -> None:
    with Image.new("RGB", (10, 100), "blue") as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    record = ask_module.ImageDatasetRecord(
        data=buffer.getvalue(),
        mime_type="image/png",
    )

    rendered = ask_module._image_preview_from_record(record, columns=72, max_rows=6)

    assert rendered is not None
    assert rendered.rows == 6


def test_attachment_uri_may_render_as_pdf() -> None:
    assert ask_module._attachment_uri_may_render_as_pdf(_pdf_data_uri())
    assert ask_module._attachment_uri_may_render_as_pdf("/tmp/report.pdf")
    assert not ask_module._attachment_uri_may_render_as_pdf(_png_data_uri("blue"))


def test_pdf_preview_from_record_uses_poppler_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_data = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"

    def fake_which(command: str) -> str | None:
        return f"/usr/bin/{command}" if command in {"pdftoppm", "pdftotext"} else None

    def fake_run(args, **kwargs):
        del kwargs
        executable = Path(args[0]).name
        if executable == "pdftoppm":
            output_prefix = Path(args[-1])
            output_prefix.with_name(output_prefix.name + "-1.png").write_bytes(
                _png_bytes("green")
            )
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if executable == "pdftotext":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=b"First page text\n",
                stderr=b"",
            )
        raise AssertionError(f"unexpected command {args}")

    monkeypatch.setattr(ask_module.shutil, "which", fake_which)
    monkeypatch.setattr(ask_module.subprocess, "run", fake_run)

    preview = ask_module._pdf_preview_from_record(
        ask_module.ImageDatasetRecord(data=pdf_data, mime_type="application/pdf"),
        name="report.pdf",
        columns=12,
        max_rows=4,
    )

    assert preview is not None
    assert preview.name == "report.pdf"
    assert preview.text == "First page text"
    assert len(preview.pages) == 1
    assert preview.pages[0].columns <= 12


def test_ask_input_attachment_helpers_remove_deleted_placeholders() -> None:
    attachment_1 = ask_module._AskInputAttachment(
        placeholder="[Image #1]",
        uri="dataset://images?id=one",
        path="/tmp/one.png",
    )
    attachment_2 = ask_module._AskInputAttachment(
        placeholder="[Image #2]",
        uri="dataset://images?id=two",
        path="/tmp/two.png",
    )

    prompt = "compare [Image #2] please"

    assert ask_module._ask_present_input_attachments(
        prompt,
        [attachment_1, attachment_2],
    ) == [attachment_2]
    assert (
        ask_module._ask_prompt_without_attachment_placeholders(
            prompt,
            [attachment_2],
        )
        == "compare please"
    )


def test_input_attachment_file_paths_from_text_accepts_images_and_pdfs(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "cat image.png"
    image_path.write_bytes(_png_bytes("blue"))
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    text_path = tmp_path / "notes.txt"
    text_path.write_text("not an image")

    assert ask_module._input_attachment_file_paths_from_text(
        f"file://{image_path}",
        current_working_directory=str(tmp_path),
    ) == [image_path]
    assert ask_module._input_attachment_file_paths_from_text(
        shlex_path := str(image_path).replace(" ", "\\ "),
        current_working_directory=str(tmp_path),
    ) == [image_path]
    assert "\\ " in shlex_path
    assert ask_module._input_attachment_file_paths_from_text(
        f"'{image_path}'",
        current_working_directory=str(tmp_path),
    ) == [image_path]
    assert ask_module._input_attachment_file_paths_from_text(
        str(image_path),
        current_working_directory=str(tmp_path),
    ) == [image_path]
    assert ask_module._input_attachment_file_paths_from_text(
        f"file://localhost{image_path}",
        current_working_directory=str(tmp_path),
    ) == [image_path]
    assert ask_module._input_attachment_file_paths_from_text(
        str(pdf_path),
        current_working_directory=str(tmp_path),
    ) == [pdf_path]
    assert ask_module._input_attachment_file_paths_from_text(
        str(tmp_path),
        current_working_directory=str(tmp_path),
    ) == [image_path, pdf_path]
    assert (
        ask_module._input_attachment_file_paths_from_text(
            str(text_path),
            current_working_directory=str(tmp_path),
        )
        == []
    )


@pytest.mark.asyncio
async def test_save_ask_input_image_attachment_uses_data_uri(tmp_path: Path) -> None:
    image_path = tmp_path / "cat.png"
    image_path.write_bytes(_png_bytes("blue"))

    attachment = await ask_module._save_ask_input_image_attachment(
        image_path=image_path,
        placeholder="[Image #1]",
    )

    assert attachment.placeholder == "[Image #1]"
    assert attachment.path == str(image_path)
    assert attachment.uri.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_save_ask_input_file_attachment_supports_pdf_data_uri(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    attachment = await ask_module._save_ask_input_file_attachment(
        path=pdf_path,
        placeholder="[report.pdf]",
    )

    assert attachment.placeholder == "[report.pdf]"
    assert attachment.name == "report.pdf"
    assert attachment.uri.startswith("data:application/pdf;base64,")


def test_agent_message_content_text_renders_non_image_attachment_as_filename() -> None:
    content = [
        AgentFileContent(
            type="file",
            url="data:application/pdf;base64,JVBERi0xLjQK",
            name="report.pdf",
        )
    ]

    assert ask_module._agent_message_content_text(content) == "[report.pdf]"


def test_conversation_message_preserves_pdf_attachment_name_for_preview() -> None:
    pdf_uri = _pdf_data_uri()
    message = StartThread(
        type=AGENT_MESSAGE_THREAD_START,
        content=[
            AgentFileContent(type="file", url=pdf_uri, name="optimus.pdf"),
        ],
    )

    rendered = ask_module._ask_conversation_message_from_agent_message(
        message,
        local_participant_name=None,
    )

    assert rendered is not None
    assert rendered.text == "[optimus.pdf]"
    assert rendered.attachment_uris == (pdf_uri,)
    assert rendered.attachment_references[0].name == "optimus.pdf"


def test_agent_message_content_text_omits_image_attachment_ascii() -> None:
    content = [
        AgentFileContent(
            type="file",
            url=_png_data_uri("blue"),
            name="screenshot.png",
        )
    ]

    assert ask_module._agent_message_content_text(content) == ""
    assert ask_module._agent_message_content_attachment_uris(content) == (
        content[0].url,
    )


def test_agent_file_content_delta_preserves_image_uri_for_tui_hydration() -> None:
    image_uri = _png_data_uri("blue")

    message = ask_module._ask_conversation_message_from_agent_message(
        ask_module.AgentFileContentDelta(
            type=ask_module.AGENT_EVENT_FILE_CONTENT_DELTA,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            url=image_uri,
        ),
        local_participant_name=None,
    )

    assert message is not None
    assert message.kind == "image"
    assert message.text == ""
    assert message.attachment_uris == (image_uri,)


def test_agent_file_content_delta_preserves_pdf_uri_for_tui_hydration() -> None:
    pdf_uri = _pdf_data_uri()

    message = ask_module._ask_conversation_message_from_agent_message(
        ask_module.AgentFileContentDelta(
            type=ask_module.AGENT_EVENT_FILE_CONTENT_DELTA,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="pdf-1",
            url=pdf_uri,
        ),
        local_participant_name=None,
    )

    assert message is not None
    assert message.kind == "image"
    assert message.text == ""
    assert message.attachment_uris == (pdf_uri,)


@pytest.mark.asyncio
async def test_ask_session_adds_cwd_storage_and_builtin_tools() -> None:
    session = ask_module._AskSession(
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
        current_working_directory="/tmp/ask-project",
    )

    toolkit_names = [toolkit.name for toolkit in session._process.toolkits]

    assert toolkit_names == ["storage", "apply_patch", "web_fetch", "web_search"]
    assert session.current_working_directory == "/tmp/ask-project"
    storage_toolkit = session._process.toolkits[0]
    assert isinstance(storage_toolkit, ask_module.StorageToolkit)
    assert storage_toolkit._mounts[0].virtual_path == "/tmp/ask-project"
    instructions_provider = session._process._turn_instructions_provider
    assert instructions_provider is not None
    additional_instructions = await instructions_provider(None)
    assert "You are the MeshAgent assistant." in additional_instructions
    assert "docs.meshagent.com and www.meshagent.com." in additional_instructions
    assert (
        "The current working directory is /tmp/ask-project." in additional_instructions
    )
    assert "You are not being run interactively." not in additional_instructions


@pytest.mark.asyncio
async def test_ask_session_adds_noninteractive_instruction_for_non_tty_mode() -> None:
    session = ask_module._AskSession(
        model="gpt-5.5",
        llm_adapter=_FakeAskAdapter(),
        current_working_directory="/tmp/ask-project",
        interactive=False,
    )

    instructions_provider = session._process._turn_instructions_provider
    assert instructions_provider is not None
    additional_instructions = await instructions_provider(None)

    assert "You are not being run interactively." in additional_instructions


def test_ask_session_uses_anthropic_web_tools_without_apply_patch() -> None:
    session = ask_module._AskSession(
        model="claude-opus-4-6",
        llm_adapter=_FakeAskAdapter(),
        current_working_directory="/tmp/ask-project",
    )

    toolkit_names = [toolkit.name for toolkit in session._process.toolkits]

    assert toolkit_names == ["storage", "web_fetch", "web_search"]


def test_is_cancelled_turn_error_checks_room_exception_code() -> None:
    assert ask_module._is_cancelled_turn_error(
        ask_module.RoomException("turn cancelled", code="cancelled")
    )
    assert not ask_module._is_cancelled_turn_error(
        ask_module.RoomException("boom", code="unexpected")
    )
    assert not ask_module._is_cancelled_turn_error(RuntimeError("boom"))


def test_suppress_ask_process_logs_restores_logger_state() -> None:
    loggers = [
        ask_module.logging.getLogger("agent-process"),
        ask_module.logging.getLogger("openai_agent"),
    ]
    previous_disabled = {logger: logger.disabled for logger in loggers}

    with ask_module._suppress_ask_process_logs():
        assert all(logger.disabled is True for logger in loggers)

    for logger in loggers:
        assert logger.disabled is previous_disabled[logger]


def test_build_ask_adapter_uses_websocket_mode_for_openai() -> None:
    adapter = ask_module._build_ask_adapter(
        model="gpt-5.5",
        project_id="project-123",
        access_token="oauth-token",
    )

    assert isinstance(adapter, OpenAIResponsesAdapter)
    assert adapter._mode == "websocket"


def test_should_launch_tui_only_when_prompt_missing_and_tty() -> None:
    assert (
        ask_module._should_launch_tui(
            message=None,
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is True
    )
    assert (
        ask_module._should_launch_tui(
            message="hello",
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is False
    )
    assert (
        ask_module._should_launch_tui(
            message=None,
            stdin_is_tty=False,
            stdout_is_tty=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_resolve_ask_access_token_prefers_meshagent_token(monkeypatch) -> None:
    async def _unexpected_get_access_token() -> str:
        raise AssertionError("oauth token should not be requested")

    monkeypatch.setenv("MESHAGENT_TOKEN", " room-token ")
    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _unexpected_get_access_token
    )

    result = await ask_module._resolve_ask_access_token()

    assert result == "room-token"


@pytest.mark.asyncio
async def test_ask_command_uses_oauth_token_and_renders_markdown_by_default(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    rendered: list[object] = []

    async def _fake_get_access_token() -> str:
        return "oauth-token"

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_build_ask_adapter(
        *,
        model: str,
        project_id: str,
        access_token: str,
    ) -> LLMAdapter:
        captured["model"] = model
        captured["project_id"] = project_id
        captured["access_token"] = access_token
        return _FakeAskAdapter()

    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module, "_build_ask_adapter", _fake_build_ask_adapter)

    class _FakeConsole:
        def print(self, value: object) -> None:
            rendered.append(value)

    monkeypatch.setattr(ask_module, "Console", _FakeConsole)
    monkeypatch.setattr(ask_module, "Markdown", lambda text: ("markdown", text))
    monkeypatch.setattr(
        ask_module.typer,
        "echo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("streaming output should not be used")
        ),
    )

    await ask_module.ask(
        project_id="project-123",
        message="hello",
        model="gpt-5.5",
    )

    assert captured == {
        "model": "gpt-5.5",
        "project_id": "project-123",
        "access_token": "oauth-token",
    }
    assert rendered == [("markdown", "hello world")]


def test_meshagent_ask_cli_invocation_prints_streamed_response(
    monkeypatch, capsys
) -> None:
    captured: dict[str, object] = {}

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_build_ask_adapter(
        *,
        model: str,
        project_id: str,
        access_token: str,
    ) -> LLMAdapter:
        captured["model"] = model
        captured["project_id"] = project_id
        captured["access_token"] = access_token
        return _FakeAskAdapter()

    monkeypatch.setenv("MESHAGENT_TOKEN", "cli-token")
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module, "_build_ask_adapter", _fake_build_ask_adapter)

    command = async_typer.get_command(cli_module.app, materialize_lazy=True)
    result = CliRunner().invoke(
        command,
        [
            "ask",
            "--project-id",
            "project-123",
            "--message",
            "hello",
            "--format",
            "text",
            "--model",
            "gpt-5.5",
        ],
    )

    assert result.exit_code == 0, result.output
    captured_stdout = result.output or capsys.readouterr().out
    assert captured_stdout == "hello world\n"
    assert captured == {
        "model": "gpt-5.5",
        "project_id": "project-123",
        "access_token": "cli-token",
    }


@pytest.mark.asyncio
async def test_send_chat_thread_prompt_returns_after_sending() -> None:
    session = _PromptSendSession()

    await asyncio.wait_for(
        ask_module._send_chat_thread_prompt(
            session=session,
            prompt="hello",
        ),
        timeout=0.1,
    )

    assert session.sent_text == "hello"


@pytest.mark.asyncio
async def test_send_chat_thread_prompt_starts_thread_without_waiting_for_turn_end() -> (
    None
):
    class _ThreadStartSession:
        has_thread_path = False

        def __init__(self) -> None:
            self.started_text: str | None = None

        async def start_thread(self, *, text: str, attachments=None) -> str:
            del attachments
            self.started_text = text
            return "message-1"

    session = _ThreadStartSession()

    await asyncio.wait_for(
        ask_module._send_chat_thread_prompt(
            session=session,
            prompt="hello",
        ),
        timeout=0.1,
    )

    assert session.started_text == "hello"


@pytest.mark.asyncio
async def test_send_chat_thread_prompt_does_not_wait_for_turn_completion() -> None:
    class _UnfinishedTurnSession:
        has_thread_path = True

        def __init__(self) -> None:
            self.sent_text: str | None = None
            self.last_completed_turn_id = None
            self.active_turn_id = "turn-1"
            self.thread_status_text = "Writing"

        async def send_text(self, *, text: str, attachments=None) -> str:
            del attachments
            self.sent_text = text
            return "message-1"

    session = _UnfinishedTurnSession()

    await asyncio.wait_for(
        ask_module._send_chat_thread_prompt(
            session=session,
            prompt="hello",
        ),
        timeout=0.1,
    )

    assert session.sent_text == "hello"


@pytest.mark.asyncio
async def test_ask_command_prints_markdown_without_streaming(monkeypatch) -> None:
    captured: dict[str, object] = {}
    rendered: list[object] = []

    async def _fake_get_access_token() -> str:
        return "oauth-token"

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_build_ask_adapter(
        *,
        model: str,
        project_id: str,
        access_token: str,
    ) -> LLMAdapter:
        captured["model"] = model
        captured["project_id"] = project_id
        captured["access_token"] = access_token
        return _FakeAskAdapter()

    class _FakeConsole:
        def print(self, value: object) -> None:
            rendered.append(value)

    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module, "_build_ask_adapter", _fake_build_ask_adapter)
    monkeypatch.setattr(ask_module, "Console", _FakeConsole)
    monkeypatch.setattr(ask_module, "Markdown", lambda text: ("markdown", text))
    monkeypatch.setattr(
        ask_module.typer,
        "echo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("streaming output should not be used")
        ),
    )

    await ask_module.ask(
        project_id="project-123",
        message="hello",
        format="markdown",
        model="gpt-5.5",
    )

    assert captured == {
        "model": "gpt-5.5",
        "project_id": "project-123",
        "access_token": "oauth-token",
    }
    assert rendered == [("markdown", "hello world")]


@pytest.mark.asyncio
async def test_ask_command_launches_tui_when_prompt_missing_in_tty(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_get_access_token() -> str:
        return "oauth-token"

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_build_ask_adapter(
        *,
        model: str,
        project_id: str,
        access_token: str,
    ) -> LLMAdapter:
        captured["model"] = model
        captured["project_id"] = project_id
        captured["access_token"] = access_token
        return _FakeAskAdapter()

    async def _fake_run_ask_tui(
        *, model: str, llm_adapter: LLMAdapter, preamble_rule: bool
    ) -> None:
        captured["tui_model"] = model
        captured["tui_adapter"] = llm_adapter
        captured["preamble_rule"] = preamble_rule

    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module, "_build_ask_adapter", _fake_build_ask_adapter)
    monkeypatch.setattr(ask_module, "_run_ask_tui", _fake_run_ask_tui)
    monkeypatch.setattr(ask_module.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(ask_module.sys, "stdout", _FakeTTY(is_tty=True))

    await ask_module.ask(
        project_id="project-123",
        message=None,
        model="gpt-5.5",
    )

    assert captured["model"] == "gpt-5.5"
    assert captured["project_id"] == "project-123"
    assert captured["access_token"] == "oauth-token"
    assert captured["tui_model"] == "gpt-5.5"
    assert captured["preamble_rule"] is True


@pytest.mark.asyncio
async def test_ask_command_requires_message_when_not_tty(monkeypatch) -> None:
    async def _fake_get_access_token() -> str:
        return "oauth-token"

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    printed: list[str] = []
    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module.typer, "echo", printed.append)
    monkeypatch.setattr(ask_module.sys, "stdin", _FakeTTY(is_tty=False))
    monkeypatch.setattr(ask_module.sys, "stdout", _FakeTTY(is_tty=False))

    with pytest.raises(typer.Exit) as exc:
        await ask_module.ask(
            project_id="project-123",
            message=None,
            model="gpt-5.5",
        )

    assert exc.value.exit_code == 1
    assert printed == [
        "Prompt required. Pass `-m/--message`, or run in a TTY for interactive mode."
    ]


@pytest.mark.asyncio
async def test_ask_command_requires_oauth_access_token(monkeypatch) -> None:
    async def _fake_get_access_token():
        return None

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    printed: list[str] = []
    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module.typer, "echo", printed.append)

    with pytest.raises(typer.Exit) as exc:
        await ask_module.ask(
            project_id="project-123",
            message="hello",
            model="gpt-5.5",
        )

    assert exc.value.exit_code == 1
    assert printed == [
        "No MeshAgent token or OAuth access token available. Set MESHAGENT_TOKEN or run `meshagent auth login` first."
    ]


@pytest.mark.asyncio
async def test_ask_command_prefers_meshagent_token_over_oauth(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _unexpected_get_access_token() -> str:
        raise AssertionError("oauth token should not be requested")

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_build_ask_adapter(
        *,
        model: str,
        project_id: str,
        access_token: str,
    ) -> LLMAdapter:
        captured["model"] = model
        captured["project_id"] = project_id
        captured["access_token"] = access_token
        return _FakeAskAdapter()

    monkeypatch.setenv("MESHAGENT_TOKEN", "room-token")
    monkeypatch.setattr(
        ask_module.auth_async, "get_access_token", _unexpected_get_access_token
    )
    monkeypatch.setattr(ask_module, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(ask_module, "_build_ask_adapter", _fake_build_ask_adapter)

    printed: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _fake_echo(*args: object, **kwargs: object) -> None:
        printed.append((args, dict(kwargs)))

    monkeypatch.setattr(ask_module.typer, "echo", _fake_echo)

    await ask_module.ask(
        project_id="project-123",
        message="hello",
        format="text",
        model="gpt-5.5",
    )

    assert captured == {
        "model": "gpt-5.5",
        "project_id": "project-123",
        "access_token": "room-token",
    }
    assert printed == [
        (("hello",), {"nl": False}),
        ((" world",), {"nl": False}),
        ((), {}),
    ]
