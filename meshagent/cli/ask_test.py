import asyncio
import base64
import io

import pytest
import typer
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
    AgentError,
    AgentGeneratedImage,
    AgentThreadEvent,
    AgentThreadStatus,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentTextContentStarted,
    AgentToolCallEnded,
    AgentToolCallStarted,
    TurnEnded,
    TurnStart,
    TurnStarted,
)
from meshagent.api import Participant, RoomException
from meshagent.cli import ask as ask_module
from meshagent.cli.preamble_rules import DEFAULT_PREAMBLE_RULE
from meshagent.openai import OpenAIResponsesAdapter


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


def _png_bytes(color: str) -> bytes:
    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(image_buffer, format="PNG")
    return image_buffer.getvalue()


def _png_data_uri(color: str) -> str:
    image_data = _png_bytes(color)
    return f"data:image/png;base64,{base64.b64encode(image_data).decode('ascii')}"


def test_agent_message_session_renders_image_generation_data_uri_as_ascii() -> None:
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
    assert session.messages[0].text.strip() != ""
    assert "\x1b[" in session.messages[0].text


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
    first_preview = session.messages[0].text
    session.add_agent_message(
        ask_module.AgentImageGenerationPartial(
            type=ask_module.AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
            thread_id="/threads/test.thread",
            turn_id="turn-1",
            item_id="image-1",
            image=AgentGeneratedImage(uri=latest_uri, mime_type="image/png"),
        )
    )
    latest_preview = session.messages[0].text
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
    assert session.messages[0].text not in {first_preview, latest_preview}


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
    image_dataset_client = ask_module.ImageDatasetClient(_FakeImageRoom())

    image_ascii = await ask_module._ascii_image_from_uri_async(
        "dataset://agents/demo/images?id=image-1",
        image_dataset_client=image_dataset_client,
    )

    assert image_ascii is not None
    assert image_ascii.strip() != ""
    assert "\x1b[" in image_ascii


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
    logger = ask_module.logging.getLogger("agent-process")
    previous_disabled = logger.disabled

    with ask_module._suppress_ask_process_logs():
        assert logger.disabled is True

    assert logger.disabled is previous_disabled


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
async def test_ask_command_uses_oauth_token_and_prints_result(monkeypatch) -> None:
    captured: dict[str, object] = {}
    printed: list[tuple[tuple[object, ...], dict[str, object]]] = []

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

    def _fake_echo(*args: object, **kwargs: object) -> None:
        printed.append((args, dict(kwargs)))

    monkeypatch.setattr(ask_module.click, "echo", _fake_echo)

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
    assert printed == [
        (("hello",), {"nl": False}),
        ((" world",), {"nl": False}),
        ((), {}),
    ]


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
        ask_module.click,
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
    monkeypatch.setattr(ask_module.click, "echo", printed.append)
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
    monkeypatch.setattr(ask_module.click, "echo", printed.append)

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

    monkeypatch.setattr(ask_module.click, "echo", _fake_echo)

    await ask_module.ask(
        project_id="project-123",
        message="hello",
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
