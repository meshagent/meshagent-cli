import pytest
import typer

from meshagent.agents import AgentSessionContext
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.messages import (
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_TEXT_CONTENT_STARTED,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentTextContentStarted,
)
from meshagent.api import Participant
from meshagent.cli import ask as ask_module
from meshagent.openai import OpenAIResponsesAdapter


class _FakeAskAdapter(LLMAdapter[object]):
    def default_model(self) -> str:
        return "gpt-5.4"

    def create_session(self) -> AgentSessionContext:
        return AgentSessionContext()

    async def next(
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
        assert model == "gpt-5.4"
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


@pytest.mark.asyncio
async def test_run_ask_process_returns_text_output() -> None:
    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.4",
        llm_adapter=_FakeAskAdapter(),
    )

    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_ask_process_streams_text_deltas() -> None:
    deltas: list[str] = []

    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.4",
        llm_adapter=_FakeAskAdapter(),
        on_delta=deltas.append,
    )

    assert result == "hello world"
    assert deltas == ["hello", " world"]


@pytest.mark.asyncio
async def test_run_ask_process_awaits_async_text_delta_callback() -> None:
    deltas: list[str] = []

    async def _on_delta(text: str) -> None:
        deltas.append(text)

    result = await ask_module._run_ask_process(
        prompt="hi",
        model="gpt-5.4",
        llm_adapter=_FakeAskAdapter(),
        on_delta=_on_delta,
    )

    assert result == "hello world"
    assert deltas == ["hello", " world"]


@pytest.mark.asyncio
async def test_ask_session_reuses_process_for_multiple_prompts() -> None:
    async with ask_module._AskSession(
        model="gpt-5.4",
        llm_adapter=_FakeAskAdapter(),
    ) as session:
        first = await session.ask(prompt="first")
        second = await session.ask(prompt="second")

    assert first == "hello world"
    assert second == "hello world"


@pytest.mark.asyncio
async def test_ask_session_adds_cwd_storage_and_builtin_tools() -> None:
    session = ask_module._AskSession(
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
    )

    assert captured == {
        "model": "gpt-5.4",
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
        model="gpt-5.4",
    )

    assert captured == {
        "model": "gpt-5.4",
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

    async def _fake_run_ask_tui(*, model: str, llm_adapter: LLMAdapter) -> None:
        captured["tui_model"] = model
        captured["tui_adapter"] = llm_adapter

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
        model="gpt-5.4",
    )

    assert captured["model"] == "gpt-5.4"
    assert captured["project_id"] == "project-123"
    assert captured["access_token"] == "oauth-token"
    assert captured["tui_model"] == "gpt-5.4"


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
            model="gpt-5.4",
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
            model="gpt-5.4",
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
        model="gpt-5.4",
    )

    assert captured == {
        "model": "gpt-5.4",
        "project_id": "project-123",
        "access_token": "room-token",
    }
    assert printed == [
        (("hello",), {"nl": False}),
        ((" world",), {"nl": False}),
        ((), {}),
    ]
