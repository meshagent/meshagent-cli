import asyncio
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from openai import AsyncOpenAI

from meshagent.agents.adapter import LLMProvider
from meshagent.agents.messages import (
    AGENT_EVENT_AUDIO_GENERATION_DELTA,
    AGENT_EVENT_MODEL_CHANGED,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA,
    AGENT_MESSAGE_MODEL_CHANGE,
    AGENT_MESSAGE_TURN_START,
    AgentAudioFormat,
    AgentRealtimeAudioCommit,
    AgentRealtimeAudioChunk,
    AgentAudioGenerationDelta,
    AgentAudioTranscriptionDelta,
    AgentMessage,
    AgentError,
    AgentTextContentDelta,
    ChangeModel,
    TurnEnded,
    TurnStart,
    TurnStartAccepted,
    TurnStarted,
)
from meshagent.agents.process import (
    AgentSupervisor,
    LLMAgentProcess,
    Message,
)
from meshagent.cli import ask as ask_module
from meshagent.cli import process
from meshagent.openai.tools.realtime_adapter import (
    OpenAIRealtimeAdapter,
    OpenAIRealtimeSessionContext,
)
from meshagent.openai.tools.responses_adapter import OpenAIResponsesAdapter


def _should_run_live_openai_tests() -> bool:
    return (
        os.getenv("RUN_OPENAI_LIVE_TESTS") == "1"
        and isinstance(os.getenv("OPENAI_API_KEY"), str)
        and os.getenv("OPENAI_API_KEY", "").strip() != ""
    )


pytestmark = pytest.mark.skipif(
    not _should_run_live_openai_tests(),
    reason="set RUN_OPENAI_LIVE_TESTS=1 and OPENAI_API_KEY to run live OpenAI tests",
)


class _FakeParticipant:
    id = "local"

    def get_attribute(self, key: str) -> str | None:
        if key == "name":
            return "agent"
        return None


class _FakeRawOutputStream:
    def __init__(self, writes: list[bytes], kwargs: dict[str, Any]) -> None:
        self._writes = writes
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def write(self, data: bytes) -> None:
        self._writes.append(data)

    def stop(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _CapturingOpenAIRealtimeAdapter(OpenAIRealtimeAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sent_events: list[dict[str, Any]] = []
        self.received_events: list[dict[str, Any]] = []

    def create_session(self):
        context = super().create_session()
        original_send_json = context.send_json

        async def send_json(payload: dict[str, Any]) -> None:
            self.sent_events.append(payload)
            await original_send_json(payload)

        context.send_json = send_json
        return context

    async def connect(
        self,
        *,
        context: OpenAIRealtimeSessionContext,
        event_handler=None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        def capture_event(event: dict[str, Any]) -> None:
            self.received_events.append(event)
            if event_handler is not None:
                event_handler(event)

        await super().connect(
            context=context,
            event_handler=capture_event,
            model=model,
            options=options,
        )

    async def create_response(
        self,
        *,
        context,
        caller,
        toolkits,
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of=None,
        tool_choice=None,
        options: dict[str, Any] | None = None,
    ):
        def capture_event(event: dict[str, Any]) -> None:
            self.received_events.append(event)
            if event_handler is not None:
                event_handler(event)

        return await super().create_response(
            context=context,
            caller=caller,
            toolkits=toolkits,
            output_schema=output_schema,
            event_handler=capture_event,
            steering_callback=steering_callback,
            model=model,
            on_behalf_of=on_behalf_of,
            tool_choice=tool_choice,
            options=options,
        )


class _LiveRealtimeSupervisor:
    def __init__(self) -> None:
        self.queues: list[asyncio.Queue[Message]] = []
        self.sent_turns: list[TurnStart] = []
        self.tasks: set[asyncio.Task[None]] = set()

    def subscribe_local_events(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue()
        self.queues.append(queue)
        return queue

    def unsubscribe_local_events(self, queue: asyncio.Queue[Message]) -> None:
        if queue in self.queues:
            self.queues.remove(queue)

    def send(self, message: Message) -> None:
        if not isinstance(message.data, TurnStart):
            return
        turn_start = message.data
        self.sent_turns.append(turn_start)
        task = asyncio.create_task(self._run_turn(turn_start))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _broadcast(self, message: object) -> None:
        if not hasattr(message, "model_dump"):
            raise TypeError(f"expected pydantic message, got {type(message)!r}")
        for queue in [*self.queues]:
            queue.put_nowait(Message(data=message))

    async def _run_turn(self, turn_start: TurnStart) -> None:
        turn_id = "live-cli-audio-turn"
        self._broadcast(
            TurnStartAccepted(
                type=AGENT_EVENT_TURN_START_ACCEPTED,
                thread_id=turn_start.thread_id,
                turn_id=turn_id,
                source_message_id=turn_start.message_id,
                content=turn_start.content,
            )
        )
        self._broadcast(
            TurnStarted(
                type=AGENT_EVENT_TURN_STARTED,
                thread_id=turn_start.thread_id,
                turn_id=turn_id,
                source_message_id=turn_start.message_id,
            )
        )

        adapter = OpenAIRealtimeAdapter(
            model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime"),
            base_url=os.getenv(
                "OPENAI_REALTIME_BASE_URL",
                "https://api.openai.com/v1",
            ),
            api_key=os.getenv("OPENAI_API_KEY"),
            session_options={
                "instructions": "You are a test assistant. Keep responses short.",
                "output_modalities": ["text"],
            },
            response_options={
                "instructions": "Reply with exactly the lowercase word: pong",
                "output_modalities": ["text"],
            },
        )
        context = adapter.create_session()
        publisher = adapter.make_agent_event_publisher(
            thread_id=turn_start.thread_id,
            turn_id=turn_id,
            callback=self._broadcast,
        )
        turn_error: AgentError | None = None
        try:
            await adapter.connect(context=context, event_handler=publisher)
            await adapter.create_response(
                context=context,
                caller=_FakeParticipant(),
                toolkits=[],
                event_handler=publisher,
                options={"output_modalities": turn_start.output_modalities or ["text"]},
            )
        except Exception as exc:
            turn_error = AgentError(message=str(exc), code="live_test_error")
        finally:
            await adapter.disconnect(context=context)
            await context.close()
            self._broadcast(
                TurnEnded(
                    type=AGENT_EVENT_TURN_ENDED,
                    thread_id=turn_start.thread_id,
                    turn_id=turn_id,
                    error=turn_error,
                )
            )

    async def close(self) -> None:
        for task in [*self.tasks]:
            task.cancel()
        if len(self.tasks) > 0:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class _RecordingAgentSupervisor(AgentSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        self.sent.append(message)

    def payloads(self, *, message_type: str) -> list[dict[str, Any]]:
        return [
            message.data.model_dump(mode="json")
            for message in self.sent
            if message.data.type == message_type
        ]

    def messages(self, *, message_type: str) -> list[Message]:
        return [message for message in self.sent if message.data.type == message_type]


class _RestoringThreadStorage:
    path = "thread-1"

    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    def push_message(
        self,
        *,
        message: AgentMessage,
        sender: object | None = None,
    ) -> None:
        del sender
        self.messages.append(message)

    def agent_messages(self) -> list[AgentMessage]:
        return [*self.messages]

    async def restore_session_context_async(self, *, context, llm_adapter) -> None:
        restored_messages: list[dict[str, Any]] = []
        reader = llm_adapter.make_agent_event_reader(
            emit_message=restored_messages.append
        )
        for message in self.messages:
            reader.consume(message)
        reader.finalize()
        llm_adapter.restore_context_messages(
            context=context,
            messages=restored_messages,
        )


async def _live_tts_wav(*, text: str) -> bytes:
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = await client.audio.speech.create(
        model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
        input=text,
        response_format="wav",
    )
    return response.content


def _contains_words(*, text: str, words: list[str]) -> bool:
    normalized = "".join(
        character.lower() if character.isalnum() else " " for character in text
    )
    padded = f" {normalized} "
    return all(f" {word.lower()} " in padded for word in words)


@pytest.mark.asyncio
async def test_live_llm_agent_process_sends_audio_input_to_realtime() -> None:
    phrase = "mesh agent audio test"
    audio = await asyncio.wait_for(_live_tts_wav(text=phrase), timeout=60)
    assert audio.startswith(b"RIFF")
    session_options = process._openai_realtime_text_session_options()
    session_options["instructions"] = "You are a live audio input test assistant."

    adapter = _CapturingOpenAIRealtimeAdapter(
        model=os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime"),
        base_url=os.getenv("OPENAI_REALTIME_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY"),
        session_options=session_options,
        response_options={
            "instructions": "Listen to the user's audio input and reply only with the words you heard.",
            "output_modalities": ["text"],
        },
    )
    supervisor = _RecordingAgentSupervisor()
    process_instance = LLMAgentProcess(
        thread_id="thread-1",
        participant=_FakeParticipant(),
        llm_providers=[
            LLMProvider(name="openai-realtime", adapter=adapter),
        ],
    )

    await process_instance.start(supervisor)
    try:
        process_instance.send(
            Message(
                data=AgentRealtimeAudioChunk(
                    type="meshagent.agent.realtime_audio.chunk",
                    thread_id="thread-1",
                    data=audio,
                    format=AgentAudioFormat(type="audio/wav"),
                )
            )
        )
        process_instance.send(
            Message(
                data=AgentRealtimeAudioCommit(
                    type="meshagent.agent.realtime_audio.commit",
                    thread_id="thread-1",
                    turn_id="audio-turn-1",
                )
            )
        )
        process_instance.send(
            Message(
                data=TurnStart(
                    type="meshagent.agent.turn.start",
                    thread_id="thread-1",
                    turn_id="audio-turn-1",
                    content=[],
                    model=adapter.default_model(),
                    provider="openai-realtime",
                    output_modalities=["text"],
                )
            )
        )

        await asyncio.wait_for(
            _wait_for_turn_end(supervisor=supervisor),
            timeout=90,
        )
    finally:
        await process_instance.stop(supervisor)

    sent_event_types = [
        event_type
        for event in adapter.sent_events
        if isinstance((event_type := event.get("type")), str)
    ]
    assistant_text = " ".join(
        payload.get("text", "")
        for payload in supervisor.payloads(message_type=AGENT_EVENT_TEXT_CONTENT_DELTA)
        if isinstance(payload.get("text"), str)
    )
    transcript_text = " ".join(
        payload.get("text", "")
        for payload in supervisor.payloads(
            message_type=AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA
        )
        if isinstance(payload.get("text"), str)
    )
    user_transcript_text = " ".join(
        payload.get("text", "")
        for payload in supervisor.payloads(
            message_type=AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA
        )
        if payload.get("role") == "user" and isinstance(payload.get("text"), str)
    )
    assistant_transcript_text = " ".join(
        payload.get("text", "")
        for payload in supervisor.payloads(
            message_type=AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA
        )
        if payload.get("role") == "assistant" and isinstance(payload.get("text"), str)
    )
    received_event_types = [
        event_type
        for event in adapter.received_events
        if isinstance((event_type := event.get("type")), str)
    ]
    response_created_events = [
        event
        for event in adapter.received_events
        if event.get("type") == "response.created"
    ]
    turn_ended_payloads = supervisor.payloads(message_type=AGENT_EVENT_TURN_ENDED)

    assert "input_audio_buffer.append" in sent_event_types
    assert "input_audio_buffer.commit" in sent_event_types
    assert "response.create" in sent_event_types
    assert _contains_words(
        text=user_transcript_text,
        words=["mesh", "agent", "audio", "test"],
    ), {
        "assistant_text": assistant_text,
        "transcript_text": transcript_text,
        "user_transcript_text": user_transcript_text,
        "assistant_transcript_text": assistant_transcript_text,
        "received_event_types": received_event_types,
        "sent_event_types": sent_event_types,
        "message_types": [message.data.type for message in supervisor.sent],
        "turn_errors": turn_ended_payloads,
        "supervisor_payloads": [
            message.data.model_dump(mode="json") for message in supervisor.sent
        ],
    }
    assert assistant_transcript_text.strip() == "", {
        "assistant_text": assistant_text,
        "user_transcript_text": user_transcript_text,
        "assistant_transcript_text": assistant_transcript_text,
        "received_event_types": received_event_types,
        "sent_event_types": sent_event_types,
        "response_created_events": response_created_events,
        "turn_errors": turn_ended_payloads,
        "supervisor_payloads": [
            message.data.model_dump(mode="json") for message in supervisor.sent
        ],
    }
    assert len(response_created_events) == 1, {
        "received_event_types": received_event_types,
        "sent_event_types": sent_event_types,
        "response_created_events": response_created_events,
        "turn_errors": turn_ended_payloads,
    }


async def _wait_for_turn_end(*, supervisor: _RecordingAgentSupervisor) -> None:
    while len(supervisor.payloads(message_type=AGENT_EVENT_TURN_ENDED)) == 0:
        await asyncio.sleep(0.05)


async def _wait_for_message_count(
    *,
    supervisor: _RecordingAgentSupervisor,
    message_type: str,
    count: int,
) -> None:
    while len(supervisor.payloads(message_type=message_type)) < count:
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_live_llm_agent_process_switches_from_realtime_audio_to_responses_text() -> (
    None
):
    realtime_model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
    text_model = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4")
    session_options = process._openai_realtime_text_session_options()
    session_options["instructions"] = "You are a live model switch test assistant."
    realtime_adapter = OpenAIRealtimeAdapter(
        model=realtime_model,
        base_url=os.getenv("OPENAI_REALTIME_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY"),
        session_options=session_options,
        response_options={
            "instructions": "Reply with exactly the lowercase word: pong",
        },
    )
    responses_adapter = OpenAIResponsesAdapter(
        model=text_model,
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY"),
        response_options={
            "instructions": "Reply with exactly the lowercase word: pong",
        },
        max_output_tokens=32,
        mode="request",
    )
    storage = _RestoringThreadStorage()
    supervisor = _RecordingAgentSupervisor()
    process_instance = LLMAgentProcess(
        thread_id="thread-1",
        participant=_FakeParticipant(),
        llm_providers=[
            LLMProvider(name="openai-realtime", adapter=realtime_adapter),
            LLMProvider(name="openai", adapter=responses_adapter),
        ],
        thread_storage=storage,
    )

    await process_instance.start(supervisor)
    try:
        process_instance.send(
            Message(
                data=TurnStart(
                    type=AGENT_MESSAGE_TURN_START,
                    thread_id="thread-1",
                    content=[{"type": "text", "text": "Say pong once."}],
                    output_modalities=["audio"],
                )
            )
        )
        await asyncio.wait_for(
            _wait_for_message_count(
                supervisor=supervisor,
                message_type=AGENT_EVENT_TURN_ENDED,
                count=1,
            ),
            timeout=90,
        )
        assert (
            len(supervisor.messages(message_type=AGENT_EVENT_AUDIO_GENERATION_DELTA))
            > 0
        )

        process_instance.send(
            Message(
                data=ChangeModel(
                    type=AGENT_MESSAGE_MODEL_CHANGE,
                    thread_id="thread-1",
                    provider="openai",
                    model=text_model,
                )
            )
        )
        await asyncio.wait_for(
            _wait_for_message_count(
                supervisor=supervisor,
                message_type=AGENT_EVENT_MODEL_CHANGED,
                count=2,
            ),
            timeout=30,
        )
        changed = supervisor.payloads(message_type=AGENT_EVENT_MODEL_CHANGED)[-1]
        assert changed["provider"] == "openai"
        assert changed["model"] == text_model
        assert changed["voice"] is None
        assert changed["output_modalities"] == ["text"]

        process_instance.send(
            Message(
                data=TurnStart(
                    type=AGENT_MESSAGE_TURN_START,
                    thread_id="thread-1",
                    content=[{"type": "text", "text": "Say pong once."}],
                    output_modalities=["text"],
                )
            )
        )
        await asyncio.wait_for(
            _wait_for_message_count(
                supervisor=supervisor,
                message_type=AGENT_EVENT_TURN_ENDED,
                count=2,
            ),
            timeout=90,
        )
    finally:
        await process_instance.stop(supervisor)

    turn_errors = [
        payload.get("error")
        for payload in supervisor.payloads(message_type=AGENT_EVENT_TURN_ENDED)
    ]
    assert turn_errors == [None, None]


@pytest.mark.asyncio
async def test_live_process_run_voice_modality_plays_realtime_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[bytes] = []

    def raw_output_stream(**kwargs: Any) -> _FakeRawOutputStream:
        return _FakeRawOutputStream(writes, kwargs)

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawOutputStream=raw_output_stream),
    )

    thread_id = "/live/process-run/audio.thread"
    model = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
    supervisor = _LiveRealtimeSupervisor()
    session = process._ProcessRunSession(
        bot=SimpleNamespace(_supervisor=supervisor),
        model=None,
        thread_path=thread_id,
        thread_storage="none",
        agent_name=None,
        thread_dir=None,
        threading_mode="none",
        current_working_directory=os.getcwd(),
        initial_model=process._agent_model_changed_for_model(
            model=model,
            thread_id=thread_id,
        ),
    )
    session._apply_models_response(
        process._configured_models_response(
            models=[model],
            current_model=session.current_model,
        )
    )
    command_response = await process._handle_process_model_command(
        "/output audio",
        session=session,
    )

    player = ask_module._StreamingAudioPlayer()
    audio_delta_count = 0
    transcript_deltas: list[str] = []
    text_deltas: list[str] = []

    async def on_message(message: object) -> None:
        nonlocal audio_delta_count
        if isinstance(message, AgentAudioGenerationDelta):
            audio_delta_count += 1
            error = await player.play_delta(message.data)
            assert error is None
        if isinstance(message, AgentAudioTranscriptionDelta):
            transcript_deltas.append(message.text)
        if isinstance(message, AgentTextContentDelta):
            text_deltas.append(message.text)

    try:
        output = await asyncio.wait_for(
            session.ask(
                prompt="Say the word pong once.",
                on_message=on_message,
            ),
            timeout=90,
        )
    finally:
        await player.close()
        await session.close()
        await supervisor.close()

    assert command_response == "Using audio responses"
    assert len(supervisor.sent_turns) == 1
    assert supervisor.sent_turns[0].output_modalities == ["audio"]
    assert audio_delta_count > 0
    assert b"".join(writes) != b""
    assert "".join(transcript_deltas).strip() != ""
    assert output.strip() != ""
    assert text_deltas == []
