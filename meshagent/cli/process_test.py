import asyncio
import ast
import inspect
from pathlib import Path

import click
import pytest

from meshagent.agents.context import AgentSessionContext
from meshagent.agents.messages import (
    AGENT_EVENT_THREAD_STARTED,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_MESSAGE_THREAD_CLOSE,
    AGENT_MESSAGE_THREAD_OPEN,
    AGENT_MESSAGE_THREAD_START,
    AGENT_MESSAGE_TURN_START,
    AGENT_MESSAGE_TURN_STEER,
    AgentTextContent,
    AgentTextContentDelta,
    AgentThreadStatus,
    CloseThread,
    OpenThread,
    StartThread,
    TurnEnded,
    TurnStart,
    TurnStarted,
    TurnSteer,
)
from meshagent.agents.process import Message
from meshagent.agents.thread_status_publisher import (
    AgentMessageThreadStatusPublisher,
)
from meshagent.api import RoomClient
from meshagent.api.specs.service import ContainerSpec, ServiceMetadata, ServiceSpec
from meshagent.cli.async_typer import get_command
from meshagent.cli import chatbot
from meshagent.cli import codex
from meshagent.cli import cli as root_cli
from meshagent.cli import mailbot
from meshagent.cli import process
from meshagent.cli import task_runner
from meshagent.cli import worker
from meshagent.computers.agent import ComputerToolkit
from meshagent.tools import Toolkit
from meshagent.tools import ContainerShellTool, ContainerToolkit, ProcessShellTool


class _FakeService:
    def __init__(self) -> None:
        self.agents: list[object] = []
        self.add_path_calls: list[dict[str, object]] = []

    def has_path(self, path: str) -> bool:
        del path
        return False

    def add_path(self, *, identity: str, path: str, cls) -> None:
        self.add_path_calls.append(
            {
                "identity": identity,
                "path": path,
                "cls": cls,
            }
        )


def _service_spec() -> ServiceSpec:
    return ServiceSpec(
        version="v1",
        kind="Service",
        metadata=ServiceMetadata(name="placeholder"),
        container=ContainerSpec(image="meshagent/cli:default"),
        ports=[],
    )


def _assert_builder_kwargs_match_signature(
    *, allowed: set[str], kwargs: dict[str, object]
) -> None:
    unexpected = set(kwargs) - allowed
    assert unexpected == set()


def test_root_cli_registers_process_group() -> None:
    command = get_command(root_cli.app)
    assert "process" in command.commands


@pytest.mark.parametrize(
    ("app", "expected_present", "expected_absent"),
    [
        (
            process.app,
            {
                "--require-toolkit",
                "--schema",
                "--mcp",
                "--instructions",
                "--shell",
                "--web-search",
                "--read-only-storage",
                "--time",
                "--table-read",
                "--document-authoring",
                "--discovery",
                "--computer-use",
                "--thread-storage",
                "--context-management",
                "--compaction-threshold",
                "--max-output-tokens",
            },
            {
                "--toolkit",
                "--require-mcp",
                "--require-schema",
                "--require-shell",
                "--require-web-search",
                "--require-read-only-storage",
                "--require-time",
                "--require-table-read",
                "--require-document-authoring",
                "--require-discovery",
                "--require-computer-use",
            },
        ),
        (
            worker.app,
            {
                "--require-toolkit",
                "--schema",
                "--shell",
                "--web-search",
                "--read-only-storage",
                "--time",
                "--table-read",
                "--computer-use",
            },
            {
                "--toolkit",
                "--mcp",
                "--require-mcp",
                "--require-schema",
                "--require-shell",
                "--require-web-search",
                "--require-read-only-storage",
                "--require-time",
                "--require-table-read",
                "--require-computer-use",
            },
        ),
        (
            mailbot.app,
            {
                "--require-toolkit",
                "--schema",
                "--shell",
                "--web-search",
                "--read-only-storage",
                "--time",
                "--table-read",
                "--computer-use",
            },
            {
                "--toolkit",
                "--mcp",
                "--require-mcp",
                "--require-schema",
                "--require-shell",
                "--require-web-search",
                "--require-read-only-storage",
                "--require-time",
                "--require-table-read",
                "--require-computer-use",
            },
        ),
        (
            task_runner.app,
            {
                "--require-toolkit",
                "--schema",
                "--shell",
                "--web-search",
                "--read-only-storage",
                "--time",
                "--table-read",
                "--document-authoring",
                "--discovery",
                "--computer-use",
            },
            {
                "--toolkit",
                "--mcp",
                "--require-mcp",
                "--require-schema",
                "--require-shell",
                "--require-web-search",
                "--require-read-only-storage",
                "--require-time",
                "--require-table-read",
                "--require-document-authoring",
                "--require-discovery",
                "--require-computer-use",
            },
        ),
    ],
)
def test_join_help_uses_canonical_tool_flag_names(
    app, expected_present: set[str], expected_absent: set[str]
) -> None:
    join_command = get_command(app).commands["join"]
    visible_options = {
        option
        for param in join_command.params
        if isinstance(param, click.Option) and not param.hidden
        for option in param.opts
    }

    for option in expected_present:
        assert option in visible_options
    for option in expected_absent:
        assert option not in visible_options


def test_chatbot_use_help_hides_removed_tool_request_options() -> None:
    use_command = get_command(chatbot.app).commands["use"]

    assert not any(
        any(
            option in {"--use-web-search", "--use-image-gen", "--use-storage"}
            for option in param.opts
        )
        for param in use_command.params
        if isinstance(param, click.Option)
    )


def _chat_with_keyword_arguments(module) -> dict[int, set[str]]:
    tree = ast.parse(inspect.getsource(module))
    calls: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "chat_with":
            continue
        calls[node.lineno] = {
            keyword.arg for keyword in node.keywords if keyword.arg is not None
        }
    return calls


@pytest.mark.parametrize("module", [chatbot, codex])
def test_chat_with_call_sites_match_chat_with_signature(module) -> None:
    allowed_kwargs = set(inspect.signature(chatbot.chat_with).parameters)
    unexpected_by_line = {
        line: sorted(kwargs - allowed_kwargs)
        for line, kwargs in _chat_with_keyword_arguments(module).items()
        if len(kwargs - allowed_kwargs) > 0
    }

    assert unexpected_by_line == {}


def test_resolved_channels_accept_mail_channel() -> None:
    assert chatbot._resolved_channels(
        runtime="process",
        channel=["chat", "mail:mailbox@mail.meshagent.com"],
    ) == ["chat", "mail:mailbox@mail.meshagent.com"]


def test_resolved_channels_accept_queue_channel() -> None:
    assert chatbot._resolved_channels(
        runtime="process",
        channel=["queue:jobs"],
    ) == ["queue:jobs"]


@pytest.mark.asyncio
async def test_process_agent_passes_threading_mode_to_queue_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents
    from meshagent.agents.process import Channel

    captured_calls: list[dict[str, object]] = []

    class _RecordingQueueChannel(Channel):
        def __init__(
            self,
            *,
            room,
            queue_name: str,
            threading_mode: str | None = None,
            thread_dir: str | None = None,
            llm_adapter=None,
        ) -> None:
            super().__init__()
            captured_calls.append(
                {
                    "room": room,
                    "queue_name": queue_name,
                    "threading_mode": threading_mode,
                    "thread_dir": thread_dir,
                    "llm_adapter": llm_adapter,
                }
            )

        def handles(self, message: Message) -> bool:
            del message
            return False

    monkeypatch.setattr(meshagent.agents, "QueueChannel", _RecordingQueueChannel)

    agent_cls = process.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        threading_mode="default-new",
        thread_dir="/threads/queue",
        channels=["queue:jobs"],
    )
    agent = agent_cls()
    room = _FakeProcessRoomClient()

    async def _skip_install_requirements() -> None:
        return None

    monkeypatch.setattr(agent, "install_requirements", _skip_install_requirements)

    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        assert len(captured_calls) == 1
        assert captured_calls[0]["room"] is room
        assert captured_calls[0]["queue_name"] == "jobs"
        assert captured_calls[0]["threading_mode"] == "default-new"
        assert captured_calls[0]["thread_dir"] == "/threads/queue"
        assert captured_calls[0]["llm_adapter"] is not None
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_process_agent_passes_threading_mode_to_mail_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents
    from meshagent.agents.process import Channel

    captured_calls: list[dict[str, object]] = []

    class _RecordingMailChannel(Channel):
        def __init__(
            self,
            *,
            room,
            queue_name: str,
            email_address: str,
            threading_mode: str | None = None,
            thread_dir: str | None = None,
            llm_adapter=None,
        ) -> None:
            super().__init__()
            captured_calls.append(
                {
                    "room": room,
                    "queue_name": queue_name,
                    "email_address": email_address,
                    "threading_mode": threading_mode,
                    "thread_dir": thread_dir,
                    "llm_adapter": llm_adapter,
                }
            )

        def handles(self, message: Message) -> bool:
            del message
            return False

    monkeypatch.setattr(meshagent.agents, "MailChannel", _RecordingMailChannel)

    agent_cls = process.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        threading_mode="default-new",
        thread_dir="/threads/mail",
        channels=["mail:mailbox@mail.meshagent.com"],
    )
    agent = agent_cls()
    room = _FakeProcessRoomClient()

    async def _skip_install_requirements() -> None:
        return None

    monkeypatch.setattr(agent, "install_requirements", _skip_install_requirements)

    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        assert len(captured_calls) == 1
        assert captured_calls[0]["room"] is room
        assert captured_calls[0]["queue_name"] == "mailbox@mail.meshagent.com"
        assert captured_calls[0]["email_address"] == "mailbox@mail.meshagent.com"
        assert captured_calls[0]["threading_mode"] == "default-new"
        assert captured_calls[0]["thread_dir"] == "/threads/mail"
        assert captured_calls[0]["llm_adapter"] is not None
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_process_agent_uses_shared_decision_adapter_for_threaded_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents
    from meshagent.agents.process import Channel

    created_adapters: list[object] = []
    captured_calls: list[dict[str, object]] = []

    class _FakeDecisionAdapter:
        def __init__(
            self,
            *,
            model: str | None = None,
            api_key: str | None = None,
            response_options=None,
            log_requests=None,
            context_management=None,
            compaction_threshold=None,
            max_output_tokens=None,
        ) -> None:
            self._model = model if model is not None else "default-model"
            self.api_key = api_key
            self.response_options = response_options
            self.log_requests = log_requests
            self.context_management = context_management
            self.compaction_threshold = compaction_threshold
            self.max_output_tokens = max_output_tokens
            created_adapters.append(self)

        def default_model(self) -> str:
            return self._model

        def create_session(self) -> AgentSessionContext:
            return AgentSessionContext()

        async def next(self, **kwargs):
            del kwargs
            raise AssertionError("decision adapter should not be used in this test")

    class _RecordingChatChannel(Channel):
        def __init__(
            self,
            *,
            room,
            threading_mode: str | None = None,
            thread_dir: str | None = None,
            llm_adapter=None,
        ) -> None:
            super().__init__()
            captured_calls.append(
                {
                    "kind": "chat",
                    "room": room,
                    "threading_mode": threading_mode,
                    "thread_dir": thread_dir,
                    "llm_adapter": llm_adapter,
                }
            )

        def handles(self, message: Message) -> bool:
            del message
            return False

    class _RecordingQueueChannel(Channel):
        def __init__(
            self,
            *,
            room,
            queue_name: str,
            threading_mode: str | None = None,
            thread_dir: str | None = None,
            llm_adapter=None,
        ) -> None:
            super().__init__()
            captured_calls.append(
                {
                    "kind": "queue",
                    "room": room,
                    "queue_name": queue_name,
                    "threading_mode": threading_mode,
                    "thread_dir": thread_dir,
                    "llm_adapter": llm_adapter,
                }
            )

        def handles(self, message: Message) -> bool:
            del message
            return False

    class _RecordingMailChannel(Channel):
        def __init__(
            self,
            *,
            room,
            queue_name: str,
            email_address: str,
            threading_mode: str | None = None,
            thread_dir: str | None = None,
            llm_adapter=None,
        ) -> None:
            super().__init__()
            captured_calls.append(
                {
                    "kind": "mail",
                    "room": room,
                    "queue_name": queue_name,
                    "email_address": email_address,
                    "threading_mode": threading_mode,
                    "thread_dir": thread_dir,
                    "llm_adapter": llm_adapter,
                }
            )

        def handles(self, message: Message) -> bool:
            del message
            return False

    monkeypatch.setattr(chatbot, "OpenAIResponsesAdapter", _FakeDecisionAdapter)
    monkeypatch.setattr(meshagent.agents, "ChatChannel", _RecordingChatChannel)
    monkeypatch.setattr(meshagent.agents, "QueueChannel", _RecordingQueueChannel)
    monkeypatch.setattr(meshagent.agents, "MailChannel", _RecordingMailChannel)

    agent_cls = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        channels=["chat", "queue:jobs", "mail:mailbox@mail.meshagent.com"],
        context_management="standalone",
        compaction_threshold=120000,
        max_output_tokens=4096,
    )
    agent = agent_cls()
    room = _FakeProcessRoomClient()

    async def _skip_install_requirements() -> None:
        return None

    monkeypatch.setattr(agent, "install_requirements", _skip_install_requirements)

    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        assert len(created_adapters) == 2
        channel_adapter = created_adapters[0]
        main_adapter = created_adapters[1]
        assert channel_adapter.default_model() == "gpt-5.4-mini"
        assert main_adapter.default_model() == "gpt-5.5"
        assert main_adapter.context_management == "standalone"
        assert main_adapter.compaction_threshold == 120000
        assert main_adapter.max_output_tokens == 4096
        assert len(captured_calls) == 3
        assert {call["kind"] for call in captured_calls} == {"chat", "queue", "mail"}
        assert all(call["llm_adapter"] is channel_adapter for call in captured_calls)
        assert all(call["llm_adapter"] is not main_adapter for call in captured_calls)
    finally:
        await agent.stop()


def test_resolved_channels_accept_toolkit_channel() -> None:
    assert chatbot._resolved_channels(
        runtime="process",
        channel=["toolkit:assistant"],
    ) == ["toolkit:assistant"]


def test_chatbot_agent_annotations_include_thread_dir() -> None:
    assert chatbot._chatbot_agent_annotations(
        threading_mode="default-new",
        thread_dir="/threads/helper",
    ) == {
        "meshagent.agent.type": "ChatBot",
        "meshagent.chatbot.threading": "default-new",
        "meshagent.chatbot.thread-dir": "/threads/helper",
        "meshagent.chatbot.thread-list": "/threads/helper/index.threadl",
    }


def test_codex_chatbot_agent_annotations_include_thread_dir() -> None:
    assert codex._chatbot_agent_annotations(
        threading_mode="default-new",
        thread_dir="/threads/helper",
    ) == {
        "meshagent.agent.type": "ChatBot",
        "meshagent.chatbot.threading": "default-new",
        "meshagent.chatbot.thread-dir": "/threads/helper",
        "meshagent.chatbot.thread-list": "/threads/helper/index.threadl",
    }


def test_process_agent_annotations_use_canonical_dataset_thread_dir() -> None:
    assert process._process_agent_annotations(
        threading_mode="default-new",
        thread_dir="/agents/helper/threads",
        thread_storage="dataset",
        channel=["chat"],
    ) == {
        "meshagent.agent.type": "ChatBot",
        "meshagent.chatbot.threading": "default-new",
        "meshagent.chatbot.thread-dir": "dataset://agents/helper/threads",
        "meshagent.chatbot.thread-list": "/agents/helper/threads/index.threadl",
    }


def test_process_agent_annotations_use_dataset_thread_path_without_threading() -> None:
    assert process._process_agent_annotations(
        threading_mode="none",
        thread_dir="/agents/helper/threads",
        thread_storage="dataset",
        channel=["chat"],
    ) == {
        "meshagent.agent.type": "ChatBot",
        "meshagent.chatbot.thread-path": "dataset://agents/helper/threads/main",
    }


def test_process_threading_options_default_dataset_thread_dir_without_threading() -> (
    None
):
    assert process._resolve_process_threading_options(
        agent_name="helper",
        threading_mode="none",
        thread_dir=None,
        thread_storage="dataset",
    ) == ("none", "/agents/helper/threads")


def test_process_agent_annotations_use_tmp_thread_dir_without_storage() -> None:
    assert process._process_agent_annotations(
        threading_mode="default-new",
        thread_dir="/agents/helper/threads",
        thread_storage="none",
        channel=["chat"],
    ) == {
        "meshagent.agent.type": "ChatBot",
        "meshagent.chatbot.threading": "default-new",
        "meshagent.chatbot.thread-dir": "tmp://agents/helper/threads",
        "meshagent.chatbot.thread-list": "/agents/helper/threads/index.threadl",
    }


def test_process_spec_uses_process_runtime_and_chat_channel(monkeypatch) -> None:
    fake_service = _FakeService()
    build_calls: list[dict[str, object]] = []
    printed: list[str] = []
    allowed_build_kwargs = set(
        inspect.signature(chatbot.build_process_agent).parameters
    )

    def fake_get_service(*, host, port):
        del host
        del port
        return fake_service

    def fake_build_process_agent(**kwargs):
        build_calls.append(kwargs)
        return type("DummyProcessAgent", (), {})

    def fail_build_chatbot(**kwargs):
        del kwargs
        raise AssertionError("process spec should not use chatbot builder")

    def capture_print(*args, **kwargs):
        del kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr(chatbot, "get_service", fake_get_service)
    monkeypatch.setattr(
        chatbot, "service_specs", lambda token_identity=None: [_service_spec()]
    )
    monkeypatch.setattr(chatbot, "build_process_agent", fake_build_process_agent)
    monkeypatch.setattr(chatbot, "build_chatbot", fail_build_chatbot)
    monkeypatch.setattr(chatbot, "print", capture_print)
    monkeypatch.setattr(
        chatbot.sys,
        "argv",
        [
            "meshagent",
            "process",
            "spec",
            "--agent-name",
            "helper",
            "--decision-model",
            "gpt-5.4-nano",
            "--channel",
            "chat",
            "--thread-storage",
            "dataset",
            "--context-management",
            "standalone",
            "--compaction-threshold",
            "120000",
            "--max-output-tokens",
            "4096",
        ],
    )

    async def invoke_spec() -> None:
        await chatbot.spec(
            agent_name="helper",
            decision_model="gpt-5.4-nano",
            channel=["chat"],
            context_management="standalone",
            compaction_threshold=120000,
            max_output_tokens=4096,
        )

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    spec_command = click.Command("spec")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                spec_command,
                info_name="spec",
                parent=process_context,
            ):
                asyncio.run(invoke_spec())

    assert len(build_calls) == 1
    _assert_builder_kwargs_match_signature(
        allowed=allowed_build_kwargs,
        kwargs=build_calls[0],
    )
    assert build_calls[0]["channels"] == ["chat"]
    assert build_calls[0]["decision_model"] == "gpt-5.4-nano"
    assert build_calls[0]["context_management"] == "standalone"
    assert build_calls[0]["compaction_threshold"] == 120000
    assert build_calls[0]["max_output_tokens"] == 4096
    assert build_calls[0]["dataset_namespace"] is None
    assert "mcp" not in build_calls[0]
    assert build_calls[0]["require_mcp"] is False
    assert len(fake_service.agents) == 1
    assert fake_service.agents[0].annotations == {
        "meshagent.agent.type": "ChatBot",
    }
    assert len(printed) == 1
    assert "meshagent process join --agent-name helper" in printed[0]


def test_chatbot_spec_defaults_dataset_namespace(monkeypatch) -> None:
    fake_service = _FakeService()
    build_calls: list[dict[str, object]] = []
    allowed_build_kwargs = set(inspect.signature(chatbot.build_chatbot).parameters)

    def fake_get_service(*, host, port):
        del host
        del port
        return fake_service

    def fake_build_chatbot(**kwargs):
        build_calls.append(kwargs)
        return type("DummyChatbot", (), {})

    def fail_build_process_agent(**kwargs):
        del kwargs
        raise AssertionError("chatbot spec should not use process builder")

    monkeypatch.setattr(chatbot, "get_service", fake_get_service)
    monkeypatch.setattr(
        chatbot, "service_specs", lambda token_identity=None: [_service_spec()]
    )
    monkeypatch.setattr(chatbot, "build_chatbot", fake_build_chatbot)
    monkeypatch.setattr(chatbot, "build_process_agent", fail_build_process_agent)
    monkeypatch.setattr(chatbot, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chatbot.sys,
        "argv",
        [
            "meshagent",
            "chatbot",
            "spec",
            "--agent-name",
            "helper",
        ],
    )

    async def invoke_spec() -> None:
        await chatbot.spec(agent_name="helper")

    root_command = click.Command("meshagent")
    chatbot_command = click.Command("chatbot")
    spec_command = click.Command("spec")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            chatbot_command,
            info_name="chatbot",
            parent=root_context,
        ) as chatbot_context:
            with click.Context(
                spec_command,
                info_name="spec",
                parent=chatbot_context,
            ):
                asyncio.run(invoke_spec())

    assert len(build_calls) == 1
    _assert_builder_kwargs_match_signature(
        allowed=allowed_build_kwargs,
        kwargs=build_calls[0],
    )
    assert build_calls[0]["dataset_namespace"] == [".datasets"]
    assert "mcp" not in build_calls[0]
    assert build_calls[0]["require_mcp"] is False


def test_process_join_passes_supported_builder_kwargs(monkeypatch) -> None:
    build_calls: list[dict[str, object]] = []
    allowed_build_kwargs = set(
        inspect.signature(process.build_process_agent).parameters
    )

    class _DummyAccountClient:
        async def close(self) -> None:
            return None

    async def fake_get_client():
        return _DummyAccountClient()

    async def fake_resolve_project_id(*, project_id=None):
        del project_id
        return "project-123"

    async def fake_resolve_key(*, project_id=None, key=None):
        del project_id
        del key
        return None

    def fake_build_process_agent(**kwargs):
        build_calls.append(kwargs)
        return type("DummyProcessAgent", (), {})

    def fail_build_chatbot(**kwargs):
        del kwargs
        raise AssertionError("process join should not use chatbot builder")

    monkeypatch.setenv("MESHAGENT_TOKEN", "test-token")
    monkeypatch.setattr(process, "get_client", fake_get_client)
    monkeypatch.setattr(process, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(process, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(process, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(process, "build_process_agent", fake_build_process_agent)
    monkeypatch.setattr(process, "build_chatbot", fail_build_chatbot)
    monkeypatch.setattr(process, "get_deferred", lambda: True)
    monkeypatch.setattr(
        process.sys,
        "argv",
        [
            "meshagent",
            "process",
            "join",
            "--agent-name",
            "helper",
            "--room",
            "quickstart",
            "--channel",
            "chat",
            "--context-management",
            "none",
            "--compaction-threshold",
            "90000",
            "--max-output-tokens",
            "2048",
        ],
    )

    async def invoke_join() -> None:
        await process.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
            channel=["chat"],
            thread_storage="dataset",
            context_management="none",
            compaction_threshold=90000,
            max_output_tokens=2048,
        )

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    join_command = click.Command("join")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                join_command,
                info_name="join",
                parent=process_context,
            ):
                asyncio.run(invoke_join())

    assert len(build_calls) == 1
    _assert_builder_kwargs_match_signature(
        allowed=allowed_build_kwargs,
        kwargs=build_calls[0],
    )
    assert build_calls[0]["channels"] == ["chat"]
    assert "local_shell" not in build_calls[0]
    assert "shell" not in build_calls[0]
    assert "script_tool" not in build_calls[0]
    assert "mcp" not in build_calls[0]
    assert build_calls[0]["require_mcp"] is False
    assert build_calls[0]["api_key"] == "test-token"
    assert build_calls[0]["thread_storage"] == "dataset"
    assert build_calls[0]["context_management"] == "none"
    assert build_calls[0]["compaction_threshold"] == 90000
    assert build_calls[0]["max_output_tokens"] == 2048


def test_process_join_requires_at_least_one_channel(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        chatbot,
        "print",
        lambda *args, **kwargs: printed.append(" ".join(str(arg) for arg in args)),
    )

    async def invoke_join() -> None:
        await chatbot.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
            channel=[],
        )

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    join_command = click.Command("join")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                join_command,
                info_name="join",
                parent=process_context,
            ):
                with pytest.raises(click.exceptions.Exit) as exc_info:
                    asyncio.run(invoke_join())

    assert exc_info.value.exit_code == 1
    assert printed == [
        "[bold red]at least one channel is required for process agents[/bold red]"
    ]


def test_process_service_requires_at_least_one_channel(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(
        chatbot,
        "print",
        lambda *args, **kwargs: printed.append(" ".join(str(arg) for arg in args)),
    )

    async def invoke_service() -> None:
        await chatbot.service(agent_name="helper", channel=[])

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    service_command = click.Command("service")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                service_command,
                info_name="service",
                parent=process_context,
            ):
                with pytest.raises(click.exceptions.Exit) as exc_info:
                    asyncio.run(invoke_service())

    assert exc_info.value.exit_code == 1
    assert printed == [
        "[bold red]at least one channel is required for process agents[/bold red]"
    ]


@pytest.mark.asyncio
async def test_process_run_starts_room_agent_and_uses_ask_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _DummyAccountClient:
        async def close(self) -> None:
            captured["account_closed"] = True

    class _DummyParticipant:
        def get_attribute(self, name: str) -> str:
            return f"agent-{name}"

    class _DummyProtocol:
        async def wait_for_close(self) -> None:
            await asyncio.Future()

    class _DummyRoomClient:
        def __init__(self) -> None:
            self.local_participant = _DummyParticipant()
            self.protocol = _DummyProtocol()
            self.enter_calls = 0
            self.exit_calls = 0

        async def __aenter__(self):
            self.enter_calls += 1
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type
            del exc
            del tb
            self.exit_calls += 1

    class _DummyProcessAgent:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0
            self.started_room = None

        async def start(self, *, room) -> None:
            self.start_calls += 1
            self.started_room = room

        async def stop(self) -> None:
            self.stop_calls += 1

    class _DummyWebSocketClientProtocol:
        def __init__(self, *, url: str, token: str) -> None:
            captured["websocket_url"] = url
            captured["websocket_token"] = token

        def create_factory(self):
            return object()

    room_client = _DummyRoomClient()
    process_agent = _DummyProcessAgent()

    async def fake_get_client():
        return _DummyAccountClient()

    async def fake_resolve_project_id(*, project_id=None):
        del project_id
        return "project-123"

    async def fake_resolve_key(*, project_id=None, key=None):
        del project_id
        del key
        return "signing-key"

    def fake_room_client(*, protocol_factory):
        captured["protocol_factory"] = protocol_factory
        return room_client

    def fake_build_runtime_agent(**kwargs):
        captured["runtime"] = kwargs["runtime"]
        captured["channels"] = kwargs["channels"]
        return lambda: process_agent

    async def fake_run_process_run_tui(**kwargs):
        captured["process_tui_kwargs"] = kwargs

    async def fail_chat_with(**kwargs):
        del kwargs
        raise AssertionError("process run should use ask TUI, not chat_with")

    monkeypatch.setenv("MESHAGENT_TOKEN", "test-token")
    monkeypatch.setattr(process, "get_client", fake_get_client)
    monkeypatch.setattr(process, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(process, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(process, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(process, "RoomClient", fake_room_client)
    monkeypatch.setattr(
        process, "WebSocketClientProtocol", _DummyWebSocketClientProtocol
    )
    monkeypatch.setattr(process, "_build_runtime_agent", fake_build_runtime_agent)
    monkeypatch.setattr(process, "_run_process_run_tui", fake_run_process_run_tui)
    monkeypatch.setattr(process, "chat_with", fail_chat_with)

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    run_command = click.Command("run")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                run_command,
                info_name="run",
                parent=process_context,
            ):
                await process.run(
                    project_id=None,
                    room="quickstart",
                    agent_name="helper",
                    channel=["chat"],
                )

    assert captured["runtime"] == "process"
    assert captured["channels"] == ["chat"]
    assert room_client.enter_calls == 1
    assert room_client.exit_calls == 1
    assert process_agent.start_calls == 1
    assert process_agent.stop_calls == 1
    assert process_agent.started_room is room_client
    assert captured["process_tui_kwargs"] == {
        "bot": process_agent,
        "model": "gpt-5.5",
        "thread_path": None,
        "thread_storage": "meshdocument",
        "agent_name": "helper",
        "thread_dir": "/agents/helper/threads",
        "message": None,
        "working_dir": None,
    }
    assert captured["account_closed"] is True


def test_process_run_thread_id_uses_dataset_scheme_for_dataset_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process.uuid, "uuid4", lambda: "fixed-id")

    assert (
        process._process_run_thread_id(
            thread_path=None,
            thread_storage="dataset",
            agent_name=None,
            thread_dir=None,
        )
        == "dataset://process-run/fixed-id"
    )
    assert (
        process._process_run_thread_id(
            thread_path="/threads/custom",
            thread_storage="dataset",
            agent_name=None,
            thread_dir=None,
        )
        == "dataset://threads/custom"
    )
    assert (
        process._process_run_thread_id(
            thread_path="dataset://threads/custom",
            thread_storage="dataset",
            agent_name=None,
            thread_dir=None,
        )
        == "dataset://threads/custom"
    )
    assert (
        process._process_run_thread_id(
            thread_path=None,
            thread_storage="meshdocument",
            agent_name="helper",
            thread_dir=None,
        )
        == ".threads/helper/main.thread"
    )
    assert (
        process._process_run_thread_id(
            thread_path=None,
            thread_storage="meshdocument",
            agent_name="helper",
            thread_dir="/agents/helper/threads",
        )
        == "/agents/helper/threads/main.thread"
    )
    assert (
        process._process_run_thread_id(
            thread_path=None,
            thread_storage="dataset",
            agent_name="helper",
            thread_dir="dataset://agents/helper/threads",
        )
        == "dataset://agents/helper/threads/main"
    )


@pytest.mark.asyncio
async def test_process_run_tui_reuses_ask_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshagent.cli import ask as ask_module

    captured: dict[str, object] = {}

    class _DummySupervisor:
        def __init__(self) -> None:
            self.subscribed_queue = None
            self.unsubscribed_queue = None
            self.sent_messages: list[Message] = []
            self.processes: list[object] = []

        def subscribe_local_events(self):
            self.subscribed_queue = asyncio.Queue()
            return self.subscribed_queue

        def unsubscribe_local_events(self, queue) -> None:
            self.unsubscribed_queue = queue

        def send(self, message: Message) -> None:
            self.sent_messages.append(message)

        async def route(self, message: Message) -> None:
            self.sent_messages.append(message)

    class _DummyBot:
        def __init__(self) -> None:
            self._supervisor = _DummySupervisor()

    async def fake_run_ask_tui(**kwargs):
        captured.update(kwargs)

    bot = _DummyBot()
    monkeypatch.setattr(ask_module, "_run_ask_tui", fake_run_ask_tui)

    await process._run_process_run_tui(
        bot=bot,
        model="gpt-5.5",
        thread_path="/threads/process-run.thread",
        thread_storage="meshdocument",
        agent_name="helper",
        thread_dir=None,
        message=None,
        working_dir="/tmp",
    )

    assert captured["model"] == "gpt-5.5"
    assert captured["title"] == "meshagent process run"
    assert captured["session"].current_working_directory == "/tmp"
    assert bot._supervisor.unsubscribed_queue is bot._supervisor.subscribed_queue


@pytest.mark.asyncio
async def test_process_run_tui_loads_existing_thread_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshagent.cli import ask as ask_module

    captured: dict[str, object] = {}

    class _FakeStorage:
        pass

    class _FakeProcess:
        thread_id = "/threads/process-run.thread"
        thread_storage = _FakeStorage()

    class _DummySupervisor:
        def __init__(self) -> None:
            self.events: asyncio.Queue[Message] = asyncio.Queue()
            self.processes: list[object] = []

        def subscribe_local_events(self):
            return self.events

        def unsubscribe_local_events(self, queue) -> None:
            assert queue is self.events

        def send(self, message: Message) -> None:
            del message

        async def route(self, message: Message) -> None:
            assert isinstance(message.data, OpenThread)
            self.processes.append(_FakeProcess())

    class _DummyBot:
        def __init__(self) -> None:
            self._supervisor = _DummySupervisor()

    async def fake_run_ask_tui(**kwargs):
        captured["messages"] = kwargs["session"].messages

    monkeypatch.setattr(ask_module, "_run_ask_tui", fake_run_ask_tui)
    monkeypatch.setattr(
        process,
        "_thread_agent_messages_from_storage",
        lambda thread_storage: [
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                thread_id="/threads/process-run.thread",
                message_id="existing-1",
                content=[AgentTextContent(type="text", text="existing prompt")],
                sender_name="alex",
            ),
            AgentTextContentDelta(
                type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                thread_id="/threads/process-run.thread",
                message_id="existing-2",
                turn_id="turn-existing",
                item_id="existing-2",
                text="existing answer",
                sender_name="helper",
            ),
        ],
    )

    await process._run_process_run_tui(
        bot=_DummyBot(),
        model="gpt-5.5",
        thread_path="/threads/process-run.thread",
        thread_storage="meshdocument",
        agent_name="helper",
        thread_dir=None,
        message=None,
        working_dir="/tmp",
    )

    assert [(message.role, message.text) for message in captured["messages"]] == [
        ("alex", "existing prompt"),
        ("helper", "existing answer"),
    ]


@pytest.mark.asyncio
async def test_process_run_session_uses_thread_status_messages() -> None:
    class _DummySupervisor:
        def __init__(self) -> None:
            self.events: asyncio.Queue[Message] = asyncio.Queue()
            self.sent_messages: list[Message] = []

        def subscribe_local_events(self):
            return self.events

        def unsubscribe_local_events(self, queue) -> None:
            assert queue is self.events

        def send(self, message: Message) -> None:
            self.sent_messages.append(message)
            if isinstance(message.data, TurnStart):
                self.events.put_nowait(
                    Message(
                        data=AgentThreadStatus(
                            type=AGENT_EVENT_THREAD_STATUS,
                            thread_id=message.data.thread_id,
                            status="Planning",
                        )
                    )
                )
                self.events.put_nowait(
                    Message(
                        data=TurnStarted(
                            type=AGENT_EVENT_TURN_STARTED,
                            thread_id=message.data.thread_id,
                            turn_id="turn-1",
                            source_message_id=message.data.message_id,
                        )
                    )
                )
                self.events.put_nowait(
                    Message(
                        data=AgentThreadStatus(
                            type=AGENT_EVENT_THREAD_STATUS,
                            thread_id=message.data.thread_id,
                            turn_id="turn-1",
                            status="Running tools",
                        )
                    )
                )
                self.events.put_nowait(
                    Message(
                        data=AgentTextContentDelta(
                            type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                            thread_id=message.data.thread_id,
                            turn_id="turn-1",
                            item_id="text-1",
                            text="local response",
                        )
                    )
                )
                self.events.put_nowait(
                    Message(
                        data=TurnEnded(
                            type=AGENT_EVENT_TURN_ENDED,
                            thread_id=message.data.thread_id,
                            turn_id="turn-1",
                            error=None,
                        )
                    )
                )

    class _DummyBot:
        def __init__(self) -> None:
            self._supervisor = _DummySupervisor()

    session = process._ProcessRunSession(
        bot=_DummyBot(),
        model="gpt-5.5",
        thread_path="/threads/process-run.thread",
        thread_storage="meshdocument",
        agent_name="helper",
        thread_dir=None,
        current_working_directory="/tmp",
    )
    statuses: list[str | None] = []

    def _on_message(message) -> None:
        if isinstance(message, AgentThreadStatus):
            statuses.append(message.status)

    result = await session.ask(prompt="hello local", on_message=_on_message)

    assert result == "local response"
    assert statuses == ["Working", "Planning", "Running tools", None]


@pytest.mark.asyncio
async def test_process_use_routes_to_chat_channel_ask_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _DummyAccountClient:
        async def close(self) -> None:
            captured["account_closed"] = True

    async def fake_get_client():
        return _DummyAccountClient()

    async def fake_resolve_project_id(*, project_id=None):
        del project_id
        return "project-123"

    async def fake_run_process_use_tui(**kwargs):
        captured["process_use_kwargs"] = kwargs

    async def fail_chat_with(**kwargs):
        del kwargs
        raise AssertionError("process use should not use chatbot chat_with")

    monkeypatch.setattr(process, "get_client", fake_get_client)
    monkeypatch.setattr(process, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(process, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(process, "_run_process_use_tui", fake_run_process_use_tui)
    monkeypatch.setattr(process, "chat_with", fail_chat_with)

    root_command = click.Command("meshagent")
    process_command = click.Command("process")
    use_command = click.Command("use")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            process_command,
            info_name="process",
            parent=root_context,
        ) as process_context:
            with click.Context(
                use_command,
                info_name="use",
                parent=process_context,
            ):
                await process.use(
                    project_id=None,
                    room="quickstart",
                    agent_name="remote-helper",
                    thread_path="/threads/remote.thread",
                    message=None,
                )

    assert captured["process_use_kwargs"] == {
        "account_client": captured["process_use_kwargs"]["account_client"],
        "project_id": "project-123",
        "room": "quickstart",
        "agent_name": "remote-helper",
        "thread_path": "/threads/remote.thread",
        "message": None,
    }
    assert captured["account_closed"] is True


@pytest.mark.asyncio
async def test_chatbot_use_keeps_legacy_chat_with_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _DummyAccountClient:
        async def close(self) -> None:
            captured["account_closed"] = True

    async def fake_get_client():
        return _DummyAccountClient()

    async def fake_resolve_project_id(*, project_id=None):
        del project_id
        return "project-123"

    async def fake_chat_with(**kwargs):
        captured["chat_with_kwargs"] = kwargs

    async def fail_process_use_tui(**kwargs):
        del kwargs
        raise AssertionError("chatbot use should not use process use TUI")

    monkeypatch.setattr(chatbot, "get_client", fake_get_client)
    monkeypatch.setattr(chatbot, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(chatbot, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(chatbot, "chat_with", fake_chat_with)
    monkeypatch.setattr(chatbot, "_run_process_use_tui", fail_process_use_tui)

    root_command = click.Command("meshagent")
    chatbot_command = click.Command("chatbot")
    use_command = click.Command("use")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            chatbot_command,
            info_name="chatbot",
            parent=root_context,
        ) as chatbot_context:
            with click.Context(
                use_command,
                info_name="use",
                parent=chatbot_context,
            ):
                await chatbot.use(
                    project_id=None,
                    room="quickstart",
                    agent_name="legacy-helper",
                    thread_path="/threads/legacy.thread",
                    message="hello legacy",
                )

    assert captured["chat_with_kwargs"] == {
        "participant_name": "legacy-helper",
        "room": "quickstart",
        "project_id": "project-123",
        "thread_path": "/threads/legacy.thread",
        "message": "hello legacy",
    }
    assert captured["account_closed"] is True


@pytest.mark.asyncio
async def test_process_use_tui_uses_chat_channel_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshagent.cli import ask as ask_module

    captured: dict[str, object] = {}

    class _DummyRoomClient:
        def __init__(self) -> None:
            self.exit_calls = 0

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type
            del exc
            del tb
            self.exit_calls += 1

    class _DummyChatClient:
        def __init__(self) -> None:
            self.room = object()
            self.has_thread_path = True
            self.thread_path = "/threads/remote.thread"
            self.local_participant_name = "local-user"
            self.sent_payloads: list[object] = []
            self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.exit_calls = 0
            self.accepted_input_callback = None

        def set_accepted_input_callback(self, callback) -> None:
            self.accepted_input_callback = callback

        async def send(self, payload) -> None:
            self.sent_payloads.append(payload)
            if isinstance(payload, TurnStart):
                self.events.put_nowait(
                    TurnStarted(
                        type=AGENT_EVENT_TURN_STARTED,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        source_message_id=payload.message_id,
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    AgentTextContentDelta(
                        type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        item_id="text-1",
                        text="remote response",
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    TurnEnded(
                        type=AGENT_EVENT_TURN_ENDED,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        error=None,
                    ).model_dump(mode="json")
                )

        async def start_thread(self, payload) -> None:
            raise AssertionError("start_thread should not be called")

        async def receive(self) -> dict[str, object]:
            return await self.events.get()

        def clear_applied_queued_agent_inputs(self) -> None:
            pass

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type
            del exc
            del tb
            self.exit_calls += 1

    room_client = _DummyRoomClient()
    chat_client = _DummyChatClient()

    async def fake_open_process_use_chat_session(**kwargs):
        captured["open_kwargs"] = kwargs
        return room_client, chat_client

    async def fake_run_ask_tui(**kwargs):
        captured["tui_model"] = kwargs["model"]
        captured["tui_title"] = kwargs["title"]
        captured["assistant_name"] = kwargs["assistant_name"]
        session = kwargs["session"]
        captured["session_cwd"] = session.current_working_directory
        captured["initial_messages"] = session.messages
        deltas: list[str] = []

        def _on_message(message) -> None:
            if isinstance(message, AgentTextContentDelta):
                deltas.append(message.text)

        captured["ask_result"] = await session.ask(
            prompt="hello remote",
            on_message=_on_message,
        )
        captured["deltas"] = deltas

    async def fake_load_thread_agent_messages(**kwargs):
        captured["load_messages_kwargs"] = kwargs
        return [
            TurnStart(
                type=AGENT_MESSAGE_TURN_START,
                thread_id="/threads/remote.thread",
                message_id="existing-remote-1",
                content=[AgentTextContent(type="text", text="stored remote prompt")],
                sender_name="local-user",
            )
        ]

    monkeypatch.setattr(
        process,
        "_open_process_use_chat_session",
        fake_open_process_use_chat_session,
    )
    monkeypatch.setattr(
        process,
        "_load_thread_agent_messages",
        fake_load_thread_agent_messages,
    )
    monkeypatch.setattr(ask_module, "_run_ask_tui", fake_run_ask_tui)

    await process._run_process_use_tui(
        account_client=object(),
        project_id="project-123",
        room="quickstart",
        agent_name="remote-helper",
        thread_path="/threads/remote.thread",
        message=None,
    )

    assert captured["open_kwargs"]["project_id"] == "project-123"
    assert captured["open_kwargs"]["room"] == "quickstart"
    assert captured["open_kwargs"]["participant_name"] == "remote-helper"
    assert captured["open_kwargs"]["thread_path"] == "/threads/remote.thread"
    assert captured["tui_model"] == "remote"
    assert captured["tui_title"] == "meshagent process use: remote-helper"
    assert captured["assistant_name"] == "remote-helper"
    assert [
        (message.role, message.text) for message in captured["initial_messages"]
    ] == [("you", "stored remote prompt")]
    assert captured["ask_result"] == "remote response"
    assert captured["deltas"] == ["remote response"]
    assert len(chat_client.sent_payloads) == 1
    sent_payload = chat_client.sent_payloads[0]
    assert isinstance(sent_payload, TurnStart)
    assert sent_payload.type == AGENT_MESSAGE_TURN_START
    assert sent_payload.thread_id == "/threads/remote.thread"
    assert sent_payload.content == [AgentTextContent(type="text", text="hello remote")]
    assert chat_client.exit_calls == 1
    assert room_client.exit_calls == 1


@pytest.mark.asyncio
async def test_process_chat_channel_session_uses_thread_status_messages() -> None:
    class _DummyChatClient:
        def __init__(self) -> None:
            self.has_thread_path = True
            self.thread_path = "/threads/remote.thread"
            self.local_participant_name = None
            self.sent_payloads: list[object] = []
            self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.accepted_input_callback = None

        def set_accepted_input_callback(self, callback) -> None:
            self.accepted_input_callback = callback

        async def send(self, payload) -> None:
            self.sent_payloads.append(payload)
            if isinstance(payload, TurnStart):
                self.events.put_nowait(
                    AgentThreadStatus(
                        type=AGENT_EVENT_THREAD_STATUS,
                        thread_id=payload.thread_id,
                        status="Searching files",
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    TurnStarted(
                        type=AGENT_EVENT_TURN_STARTED,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        source_message_id=payload.message_id,
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    AgentThreadStatus(
                        type=AGENT_EVENT_THREAD_STATUS,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        status="Editing file",
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    AgentTextContentDelta(
                        type=AGENT_EVENT_TEXT_CONTENT_DELTA,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        item_id="text-1",
                        text="remote response",
                    ).model_dump(mode="json")
                )
                self.events.put_nowait(
                    TurnEnded(
                        type=AGENT_EVENT_TURN_ENDED,
                        thread_id=payload.thread_id,
                        turn_id="turn-1",
                        error=None,
                    ).model_dump(mode="json")
                )

        async def start_thread(self, payload) -> None:
            raise AssertionError("start_thread should not be called")

        async def receive(self) -> dict[str, object]:
            return await self.events.get()

        def clear_applied_queued_agent_inputs(self) -> None:
            pass

    chat_client = _DummyChatClient()

    session = process._ChatChannelUseSession(chat_client=chat_client)
    statuses: list[str | None] = []

    def _on_message(message) -> None:
        if isinstance(message, AgentThreadStatus):
            statuses.append(message.status)

    result = await session.ask(
        prompt="hello remote",
        on_message=_on_message,
    )

    assert result == "remote response"
    assert statuses == ["Working", "Searching files", "Editing file", None]


@pytest.mark.asyncio
async def test_process_use_chat_channel_client_opens_thread_and_tracks_status() -> None:
    class _Participant:
        id = "agent-1"

        def get_attribute(self, name: str):
            if name == "name":
                return "remote-helper"
            return None

    class _Messaging:
        def __init__(self) -> None:
            self.is_enabled = True
            self.handlers: list[object] = []
            self.sent_payloads: list[object] = []
            self.participant = _Participant()

        def on(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.append(handler)

        def off(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.remove(handler)

        def get_participants(self) -> list[_Participant]:
            return [self.participant]

        async def enable(self) -> None:
            self.is_enabled = True

        async def send_message(
            self,
            *,
            to,
            type: str,
            message: dict[str, object],
            attachment,
        ) -> None:
            assert to is self.participant
            assert type == "agent-message"
            assert attachment is None
            self.sent_payloads.append(message["payload"])

    class _Room:
        def __init__(self) -> None:
            self.messaging = _Messaging()

    class _RoomMessage:
        from_participant_id = "agent-1"
        type = "agent-message"

        def __init__(self, payload: dict[str, object]) -> None:
            self.message = {"payload": payload}

    room = _Room()
    client = process._ProcessUseChatChannelClient(
        room=room,
        participant_name="remote-helper",
        thread_path="/threads/remote.thread",
        timeout=0.1,
    )

    await client.start()
    assert isinstance(
        OpenThread.model_validate(room.messaging.sent_payloads[0]), OpenThread
    )
    assert room.messaging.sent_payloads[0]["type"] == AGENT_MESSAGE_THREAD_OPEN

    status_payload = AgentThreadStatus(
        type=AGENT_EVENT_THREAD_STATUS,
        thread_id="/threads/remote.thread",
        status="Reviewing changes",
    ).model_dump(mode="json")
    room.messaging.handlers[0](_RoomMessage(status_payload))

    assert client.thread_status_text == "Reviewing changes"
    assert await client.receive() == status_payload

    local_turn_start = TurnStart(
        type=AGENT_MESSAGE_TURN_START,
        thread_id="/threads/remote.thread",
        message_id="message-1",
        content=[AgentTextContent(type="text", text="queued prompt")],
    )
    await client.send(local_turn_start)

    accepted_payload = {
        "type": AGENT_EVENT_TURN_START_ACCEPTED,
        "thread_id": "/threads/remote.thread",
        "source_message_id": "message-1",
        "sender_name": "self",
        "content": [{"type": "text", "text": "queued prompt"}],
    }
    room.messaging.handlers[0](_RoomMessage(accepted_payload))

    assert client.queued_message_labels == ("self: queued prompt",)
    assert await client.receive() == accepted_payload

    applied_payload = {
        "type": AGENT_EVENT_TURN_STARTED,
        "thread_id": "/threads/remote.thread",
        "turn_id": "turn-1",
        "source_message_id": "message-1",
    }
    room.messaging.handlers[0](_RoomMessage(applied_payload))

    assert client.queued_message_labels == ()
    assert await client.receive() == applied_payload

    ended_payload = {
        "type": AGENT_EVENT_TURN_ENDED,
        "thread_id": "/threads/remote.thread",
        "turn_id": "turn-1",
    }
    room.messaging.handlers[0](_RoomMessage(ended_payload))

    assert client.queued_message_labels == ()
    assert await client.receive() == ended_payload

    await client.stop()
    assert isinstance(
        CloseThread.model_validate(room.messaging.sent_payloads[-1]),
        CloseThread,
    )
    assert room.messaging.sent_payloads[-1]["type"] == AGENT_MESSAGE_THREAD_CLOSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attributes", "expected_thread_path"),
    [
        (
            {"meshagent.chatbot.thread-path": "dataset://agents/helper/threads/main"},
            "dataset://agents/helper/threads/main",
        ),
        (
            {"meshagent.chatbot.thread-dir": "dataset://agents/helper/threads"},
            "dataset://agents/helper/threads/main",
        ),
        ({}, ".threads/remote-helper/main.thread"),
    ],
)
async def test_process_use_chat_channel_client_resolves_studio_thread_path(
    attributes: dict[str, str],
    expected_thread_path: str,
) -> None:
    class _Participant:
        id = "agent-1"

        def get_attribute(self, name: str):
            if name == "name":
                return "remote-helper"
            return attributes.get(name)

    class _Messaging:
        def __init__(self) -> None:
            self.is_enabled = True
            self.handlers: list[object] = []
            self.sent_payloads: list[object] = []
            self.participant = _Participant()

        def on(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.append(handler)

        def off(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.remove(handler)

        def get_participants(self) -> list[_Participant]:
            return [self.participant]

        async def enable(self) -> None:
            self.is_enabled = True

        async def send_message(
            self,
            *,
            to,
            type: str,
            message: dict[str, object],
            attachment,
        ) -> None:
            assert to is self.participant
            assert type == "agent-message"
            assert attachment is None
            self.sent_payloads.append(message["payload"])

    class _Room:
        def __init__(self) -> None:
            self.messaging = _Messaging()

    room = _Room()
    client = process._ProcessUseChatChannelClient(
        room=room,
        participant_name="remote-helper",
        thread_path=None,
        timeout=0.1,
    )

    await client.start()

    assert client.thread_path == expected_thread_path
    assert OpenThread.model_validate(room.messaging.sent_payloads[0]).thread_id == (
        expected_thread_path
    )

    await client.stop()


@pytest.mark.asyncio
async def test_process_use_chat_channel_client_starts_default_new_thread_with_message() -> (
    None
):
    class _Participant:
        id = "agent-1"

        def get_attribute(self, name: str):
            if name == "name":
                return "remote-helper"
            if name == "meshagent.chatbot.threading":
                return "default-new"
            if name == "meshagent.chatbot.thread-dir":
                return ".threads/remote-helper"
            return None

    class _Messaging:
        def __init__(self) -> None:
            self.is_enabled = True
            self.handlers: list[object] = []
            self.sent_payloads: list[object] = []
            self.participant = _Participant()

        def on(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.append(handler)

        def off(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.remove(handler)

        def get_participants(self) -> list[_Participant]:
            return [self.participant]

        async def enable(self) -> None:
            self.is_enabled = True

        async def send_message(
            self,
            *,
            to,
            type: str,
            message: dict[str, object],
            attachment,
        ) -> None:
            assert to is self.participant
            assert type == "agent-message"
            assert attachment is None
            self.sent_payloads.append(message["payload"])

    class _Room:
        def __init__(self) -> None:
            self.messaging = _Messaging()

    class _RoomMessage:
        from_participant_id = "agent-1"
        type = "agent-message"

        def __init__(self, payload: dict[str, object]) -> None:
            self.message = {"payload": payload}

    room = _Room()
    client = process._ProcessUseChatChannelClient(
        room=room,
        participant_name="remote-helper",
        thread_path=None,
        timeout=0.1,
    )

    await client.start()
    assert client.has_thread_path is False
    assert room.messaging.sent_payloads == []

    start_thread = StartThread(
        type=AGENT_MESSAGE_THREAD_START,
        message_id="start-thread-1",
        content=[AgentTextContent(type="text", text="hello")],
    )
    start_task = asyncio.create_task(client.start_thread(start_thread))
    await asyncio.sleep(0)

    assert room.messaging.sent_payloads[0]["type"] == AGENT_MESSAGE_THREAD_START
    assert room.messaging.sent_payloads[0]["message_id"] == "start-thread-1"

    room.messaging.handlers[0](
        _RoomMessage(
            {
                "type": AGENT_EVENT_THREAD_STARTED,
                "message_id": "thread-started-1",
                "source_message_id": "start-thread-1",
                "thread_id": ".threads/remote-helper/new.thread",
            }
        )
    )
    await start_task

    assert client.thread_path == ".threads/remote-helper/new.thread"
    assert room.messaging.sent_payloads[1]["type"] == AGENT_MESSAGE_THREAD_OPEN
    assert room.messaging.sent_payloads[1]["thread_id"] == (
        ".threads/remote-helper/new.thread"
    )

    await client.stop()


@pytest.mark.asyncio
async def test_process_use_chat_channel_client_reports_accepted_remote_inputs() -> None:
    accepted_inputs: list[object] = []

    class _Participant:
        id = "agent-1"

        def get_attribute(self, name: str):
            if name == "name":
                return "remote-helper"
            return None

    class _Messaging:
        def __init__(self) -> None:
            self.is_enabled = True
            self.handlers: list[object] = []
            self.sent_payloads: list[object] = []
            self.participant = _Participant()

        def on(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.append(handler)

        def off(self, event: str, handler) -> None:
            assert event == "message"
            self.handlers.remove(handler)

        def get_participants(self) -> list[_Participant]:
            return [self.participant]

        async def enable(self) -> None:
            self.is_enabled = True

        async def send_message(
            self,
            *,
            to,
            type: str,
            message: dict[str, object],
            attachment,
        ) -> None:
            assert to is self.participant
            assert type == "agent-message"
            assert attachment is None
            self.sent_payloads.append(message["payload"])

    class _Room:
        def __init__(self) -> None:
            self.messaging = _Messaging()

    class _RoomMessage:
        from_participant_id = "agent-1"
        type = "agent-message"

        def __init__(self, payload: dict[str, object]) -> None:
            self.message = {"payload": payload}

    room = _Room()
    client = process._ProcessUseChatChannelClient(
        room=room,
        participant_name="remote-helper",
        thread_path="/threads/remote.thread",
        local_participant_name="local-user",
        timeout=0.1,
    )
    client.set_accepted_input_callback(accepted_inputs.append)

    await client.start()
    accepted_payload = {
        "type": AGENT_EVENT_TURN_START_ACCEPTED,
        "thread_id": "/threads/remote.thread",
        "source_message_id": "remote-message-1",
        "sender_name": "other-user",
        "content": [{"type": "text", "text": "hello from elsewhere"}],
    }
    room.messaging.handlers[0](_RoomMessage(accepted_payload))

    assert len(accepted_inputs) == 1
    assert accepted_inputs[0].message_id == "remote-message-1"
    assert accepted_inputs[0].role == "other-user"
    assert accepted_inputs[0].text == "hello from elsewhere"

    local_payload = {
        "type": AGENT_EVENT_TURN_START_ACCEPTED,
        "thread_id": "/threads/remote.thread",
        "source_message_id": "local-message-1",
        "sender_name": "local-user",
        "content": [{"type": "text", "text": "hello from here"}],
    }
    room.messaging.handlers[0](_RoomMessage(local_payload))

    assert len(accepted_inputs) == 2
    assert accepted_inputs[1].message_id == "local-message-1"
    assert accepted_inputs[1].role == "you"
    assert accepted_inputs[1].text == "hello from here"

    started_payload = {
        "type": AGENT_EVENT_TURN_STARTED,
        "thread_id": "/threads/remote.thread",
        "turn_id": "remote-turn-1",
        "source_message_id": "remote-message-1",
    }
    room.messaging.handlers[0](_RoomMessage(started_payload))
    first_delta_payload = {
        "type": AGENT_EVENT_TEXT_CONTENT_DELTA,
        "thread_id": "/threads/remote.thread",
        "turn_id": "remote-turn-1",
        "item_id": "text-1",
        "text": "remote ",
    }
    room.messaging.handlers[0](_RoomMessage(first_delta_payload))
    second_delta_payload = {
        "type": AGENT_EVENT_TEXT_CONTENT_DELTA,
        "thread_id": "/threads/remote.thread",
        "turn_id": "remote-turn-1",
        "item_id": "text-1",
        "text": "response",
    }
    room.messaging.handlers[0](_RoomMessage(second_delta_payload))
    ended_payload = {
        "type": AGENT_EVENT_TURN_ENDED,
        "thread_id": "/threads/remote.thread",
        "turn_id": "remote-turn-1",
        "error": None,
    }
    room.messaging.handlers[0](_RoomMessage(ended_payload))

    assert len(accepted_inputs) == 3
    assert accepted_inputs[2].message_id == "remote-turn-1"
    assert accepted_inputs[2].role == "remote-helper"
    assert accepted_inputs[2].text == "remote response"

    await client.stop()


class _FakeRoomClient:
    def __init__(self) -> None:
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self) -> "_FakeRoomClient":
        self.enter_calls += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type
        del exc
        del tb
        self.exit_calls += 1
        await asyncio.sleep(0)


class _FakeBot:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.started_room: _FakeRoomClient | None = None

    async def start(self, *, room) -> None:
        self.start_calls += 1
        self.started_room = room

    async def stop(self) -> None:
        self.stop_calls += 1
        await asyncio.sleep(0)


class _FakeShellProtocol:
    def __init__(self) -> None:
        self.token = "test-token"


class _FakeShellRoom:
    def __init__(self) -> None:
        self.protocol = _FakeShellProtocol()


@pytest.mark.asyncio
async def test_run_agent_room_session_cleans_up_on_cancellation() -> None:
    client = _FakeRoomClient()
    bot = _FakeBot()

    async def runner(client_arg: _FakeRoomClient) -> None:
        assert client_arg is client
        await asyncio.Future()

    task = asyncio.create_task(
        chatbot._run_agent_room_session(
            client=client,  # type: ignore[arg-type]
            bot=bot,
            runner=runner,
        )
    )

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot.start_calls == 1
    assert bot.stop_calls == 1
    assert bot.started_room is client
    assert client.enter_calls == 1
    assert client.exit_calls == 1


class _FakeParticipant:
    def __init__(self) -> None:
        self.id = "participant-1"
        self.attributes: dict[str, object] = {}

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    async def set_attribute(self, name: str, value) -> None:
        if value is None:
            self.attributes.pop(name, None)
            return
        self.attributes[name] = value


class _FakeProcessRoom(RoomClient):
    def __init__(self) -> None:
        self._local_participant = _FakeParticipant()
        self._protocol = _FakeProcessProtocol()

    @property
    def local_participant(self):
        return self._local_participant

    @property
    def protocol(self):
        return self._protocol


class _FakeProcessState:
    def __init__(self) -> None:
        self.thread_id = "threads/example"
        self.thread_storage = None
        self.supervisor = None
        self.session_context = None


class _FakeProcessProtocol:
    token = "token"


class _FakeProcessRoomClient(RoomClient):
    def __init__(self) -> None:
        self._local_participant = _FakeParticipant()
        self._protocol = _FakeProcessProtocol()

    @property
    def local_participant(self):
        return self._local_participant

    @property
    def protocol(self):
        return self._protocol


class _FakeProcessThreadAdapter:
    def __init__(self, *, room, path: str) -> None:
        del room
        self.path = path

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def set_pending_messages(self, *, pending_messages: list[dict]) -> None:
        del pending_messages

    async def set_thread_turn_id(self, *, turn_id: str | None) -> None:
        del turn_id

    def push_message(self, *, message, sender=None) -> None:
        del message
        del sender

    def restore_session_context(self, *, context, llm_adapter=None) -> None:
        del context
        del llm_adapter

    def make_toolkit(self):
        return Toolkit(name="thread", tools=[])


class _FakeDatasetThreadStorage(_FakeProcessThreadAdapter):
    pass


class _SteeringRecordingAdapter:
    def __init__(self) -> None:
        self.session = AgentSessionContext()
        self.call_started = asyncio.Event()
        self.release_tool_boundary = asyncio.Event()
        self.calls: list[dict[str, object]] = []
        self.tool_call_approval_handler = None

    def default_model(self) -> str:
        return "gpt-5.5"

    def create_session(self) -> AgentSessionContext:
        return self.session

    def set_tool_call_approval_handler(self, handler) -> None:
        self.tool_call_approval_handler = handler

    def needs_compaction(self, *, context: AgentSessionContext) -> bool:
        del context
        return False

    def make_agent_event_publisher(
        self,
        *,
        turn_id,
        thread_id,
        callback,
        custom_event_callback=None,
    ):
        del turn_id
        del thread_id
        del callback
        del custom_event_callback
        return lambda message: None

    async def next(
        self,
        *,
        context: AgentSessionContext,
        caller,
        toolkits: list[Toolkit],
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of=None,
        options=None,
        tool_choice=None,
    ) -> dict[str, object]:
        del caller
        del toolkits
        del output_schema
        del event_handler
        del model
        del on_behalf_of
        del options
        del tool_choice

        call: dict[str, object] = {
            "messages_before_boundary": [*context.messages],
        }
        self.calls.append(call)
        self.call_started.set()
        await self.release_tool_boundary.wait()
        if steering_callback is not None:
            call["steered"] = await steering_callback()
        else:
            call["steered"] = False
        call["messages_after_boundary"] = [*context.messages]
        return {"ok": True}


async def _wait_for(predicate, *, timeout: float = 1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_process_turn_toolkits_keep_computer_toolkit_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_cls = process.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_computer_use=True,
        require_table_read=[],
        require_table_write=[],
    )
    agent = agent_cls()
    agent._room = _FakeProcessRoom()

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    combined_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="gpt-5.5",
        turns=[
            TurnStart(
                type="meshagent.agent.turn.start",
                thread_id="threads/example",
                content=[AgentTextContent(type="text", text="hello")],
            )
        ],
    )

    assert any(isinstance(toolkit, ComputerToolkit) for toolkit in combined_toolkits)
    tool_wrapper = next(
        (
            toolkit
            for toolkit in combined_toolkits
            if isinstance(toolkit, Toolkit) and toolkit.name == "tools"
        ),
        None,
    )
    if tool_wrapper is not None:
        assert all(not isinstance(tool, ComputerToolkit) for tool in tool_wrapper.tools)


@pytest.mark.asyncio
async def test_process_turn_toolkits_preserve_required_toolkit_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_cls = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_storage=True,
        default_room_storage_mount=True,
        require_time=True,
        require_uuid=True,
        require_table_read=[],
        require_table_write=[],
    )
    agent = agent_cls()
    agent._room = _FakeProcessRoom()

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    combined_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="gpt-5.5",
        turns=[
            TurnStart(
                type="meshagent.agent.turn.start",
                thread_id="threads/example",
                content=[AgentTextContent(type="text", text="hello")],
            )
        ],
    )

    toolkit_names = [
        toolkit.name for toolkit in combined_toolkits if isinstance(toolkit, Toolkit)
    ]
    assert "storage" in toolkit_names
    assert "datetime" in toolkit_names
    assert "uuid" in toolkit_names
    assert "tools" not in toolkit_names


@pytest.mark.asyncio
async def test_process_turn_toolkits_include_thread_id_in_caller_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_cls = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_table_read=[],
        require_table_write=[],
    )
    agent = agent_cls()
    agent._room = _FakeProcessRoom()

    caller_contexts: list[dict[str, object] | None] = []

    async def _fake_get_required_toolkits(*, context):
        caller_contexts.append(context.caller_context)
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="gpt-5.5",
        turns=[
            TurnStart(
                type="meshagent.agent.turn.start",
                thread_id="threads/example",
                content=[AgentTextContent(type="text", text="hello")],
            )
        ],
    )

    assert caller_contexts == [{"thread_id": "threads/example"}]


@pytest.mark.asyncio
async def test_build_process_agent_uses_selected_dataset_thread_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents as agents_module

    monkeypatch.setattr(
        agents_module,
        "MeshDocumentThreadStorage",
        _FakeProcessThreadAdapter,
    )
    monkeypatch.setattr(
        agents_module,
        "DatasetThreadStorage",
        _FakeDatasetThreadStorage,
    )
    agent_cls = process.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_table_read=[],
        require_table_write=[],
        channels=[],
        thread_storage="dataset",
    )
    agent = agent_cls()
    monkeypatch.setattr(agent, "install_requirements", lambda: asyncio.sleep(0))
    monkeypatch.setattr(agent, "get_exposed_toolkits", lambda: asyncio.sleep(0, []))

    room = _FakeProcessRoomClient()
    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        supervisor = agent._supervisor
        assert supervisor is not None

        process_state = supervisor.create_thread_process("dataset://threads/example")
        assert isinstance(process_state.thread_storage, _FakeDatasetThreadStorage)
        assert process_state.thread_storage.path == "dataset://threads/example"
        assert process_state.thread_id == "dataset://threads/example"
        status_publisher = process_state.thread_status_publisher
        assert isinstance(
            status_publisher,
            AgentMessageThreadStatusPublisher,
        )
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_build_process_agent_can_disable_thread_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents as agents_module

    monkeypatch.setattr(
        agents_module,
        "MeshDocumentThreadStorage",
        _FakeProcessThreadAdapter,
    )
    monkeypatch.setattr(
        agents_module,
        "DatasetThreadStorage",
        _FakeDatasetThreadStorage,
    )
    agent_cls = process.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_table_read=[],
        require_table_write=[],
        channels=[],
        thread_storage="none",
    )
    agent = agent_cls()
    monkeypatch.setattr(agent, "install_requirements", lambda: asyncio.sleep(0))
    monkeypatch.setattr(agent, "get_exposed_toolkits", lambda: asyncio.sleep(0, []))

    room = _FakeProcessRoomClient()
    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        supervisor = agent._supervisor
        assert supervisor is not None

        process_state = supervisor.create_thread_process("tmp://threads/example")
        assert process_state.thread_storage is None
        assert process_state.thread_id == "tmp://threads/example"
        status_publisher = process_state.thread_status_publisher
        assert isinstance(
            status_publisher,
            AgentMessageThreadStatusPublisher,
        )
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_build_process_agent_forwards_tool_boundary_steering_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents as agents_module

    fake_adapter = _SteeringRecordingAdapter()
    monkeypatch.setattr(
        chatbot,
        "OpenAIResponsesAdapter",
        lambda **kwargs: fake_adapter,
    )
    monkeypatch.setattr(
        agents_module,
        "MeshDocumentThreadStorage",
        _FakeProcessThreadAdapter,
    )
    agent_cls = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = agent_cls()
    monkeypatch.setattr(agent, "install_requirements", lambda: asyncio.sleep(0))
    monkeypatch.setattr(agent, "get_exposed_toolkits", lambda: asyncio.sleep(0, []))
    monkeypatch.setattr(
        agent,
        "init_session",
        lambda: asyncio.sleep(0, fake_adapter.create_session()),
    )

    async def _fake_get_process_turn_toolkits(**kwargs):
        del kwargs
        return []

    monkeypatch.setattr(
        agent, "get_process_turn_toolkits", _fake_get_process_turn_toolkits
    )

    room = _FakeProcessRoomClient()
    await agent.start(room=room)  # type: ignore[arg-type]
    try:
        supervisor = agent._supervisor
        assert supervisor is not None

        supervisor.send(
            Message(
                data=TurnStart(
                    type=AGENT_MESSAGE_TURN_START,
                    thread_id="threads/example",
                    content=[AgentTextContent(type="text", text="first")],
                )
            )
        )

        await asyncio.wait_for(fake_adapter.call_started.wait(), timeout=1)
        await _wait_for(lambda: len(supervisor.processes) == 1)
        process = supervisor.processes[0]
        turn_id = process.turn_id
        assert turn_id is not None

        supervisor.send(
            Message(
                data=TurnSteer(
                    type=AGENT_MESSAGE_TURN_STEER,
                    thread_id="threads/example",
                    turn_id=turn_id,
                    content=[AgentTextContent(type="text", text="second")],
                )
            )
        )

        await _wait_for(
            lambda: (
                process._active_turn_queue is not None
                and process._active_turn_queue.qsize() >= 1
            )
        )
        fake_adapter.release_tool_boundary.set()
        await _wait_for(
            lambda: fake_adapter.calls[0].get("messages_after_boundary") is not None
        )

        assert fake_adapter.calls[0]["steered"] is True
        assert fake_adapter.calls[0]["messages_before_boundary"] == [
            {"role": "user", "content": "first"}
        ]
        assert fake_adapter.calls[0]["messages_after_boundary"] == [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_process_agent_require_shell_uses_process_shell_for_selected_claude_model_without_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5",
        rule=[],
        toolkit=[],
        schema=[],
        require_shell=True,
        shell_image="none",
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()
    agent._room = _FakeProcessRoom()

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    combined_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="claude-3-7-sonnet",
        turns=[
            TurnStart(
                type="meshagent.agent.turn.start",
                thread_id="threads/example",
                content=[AgentTextContent(type="text", text="hello")],
            )
        ],
    )

    shell_toolkit = next(
        toolkit
        for toolkit in combined_toolkits
        if isinstance(toolkit, Toolkit) and toolkit.name == "shell"
    )

    assert len(shell_toolkit.tools) == 1
    assert isinstance(shell_toolkit.tools[0], ProcessShellTool)


@pytest.mark.asyncio
async def test_process_agent_require_shell_reuses_container_shell_tool_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_process_agent = chatbot.build_process_agent(
        model="claude-3-7-sonnet",
        rule=[],
        toolkit=[],
        schema=[],
        require_shell=True,
        working_dir="/workspace",
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()
    agent._room = _FakeProcessRoom()

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    turns = [
        TurnStart(
            type="meshagent.agent.turn.start",
            thread_id="threads/example",
            content=[AgentTextContent(type="text", text="hello")],
        )
    ]

    first_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="claude-3-7-sonnet",
        turns=turns,
    )
    second_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="claude-3-7-sonnet",
        turns=turns,
    )

    first_shell_toolkit = next(
        toolkit
        for toolkit in first_toolkits
        if isinstance(toolkit, Toolkit) and toolkit.name == "shell"
    )
    second_shell_toolkit = next(
        toolkit
        for toolkit in second_toolkits
        if isinstance(toolkit, Toolkit) and toolkit.name == "shell"
    )

    assert isinstance(first_shell_toolkit.tools[0], ContainerShellTool)
    assert first_shell_toolkit.tools[0] is second_shell_toolkit.tools[0]


@pytest.mark.asyncio
async def test_process_agent_optional_shell_reuses_container_shell_toolkit_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_process_agent = chatbot.build_process_agent(
        model="claude-3-7-sonnet",
        rule=[],
        toolkit=[],
        schema=[],
        require_shell=True,
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()
    agent._room = _FakeProcessRoom()

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)

    turns = [
        TurnStart(
            type="meshagent.agent.turn.start",
            thread_id="threads/example",
            content=[AgentTextContent(type="text", text="hello")],
            toolkits={"shell": {}},
        )
    ]

    first_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="claude-3-7-sonnet",
        turns=turns,
    )
    second_toolkits = await agent.get_process_turn_toolkits(
        process=_FakeProcessState(),
        sender=None,
        model="claude-3-7-sonnet",
        turns=turns,
    )

    first_shell_toolkit = next(
        toolkit
        for toolkit in first_toolkits
        if isinstance(toolkit, Toolkit) and toolkit.name == "shell"
    )
    second_shell_toolkit = next(
        toolkit
        for toolkit in second_toolkits
        if isinstance(toolkit, Toolkit) and toolkit.name == "shell"
    )

    assert isinstance(first_shell_toolkit.tools[0], ContainerShellTool)
    assert first_shell_toolkit.tools[0] is second_shell_toolkit.tools[0]


@pytest.mark.asyncio
async def test_chatbot_require_shell_uses_none_sentinel(monkeypatch) -> None:
    custom_chatbot = chatbot.build_chatbot(
        model="gpt-5",
        rule=[],
        toolkit=[],
        schema=[],
        require_shell=True,
        shell_image="none",
    )

    async def fake_start(self, *, room) -> None:
        self._room = room

    monkeypatch.setattr(custom_chatbot.__mro__[1], "start", fake_start)

    agent = custom_chatbot()

    await agent.start(room=_FakeShellRoom())

    assert agent.shell_tool is not None
    assert agent.shell_tool.image is None


@pytest.mark.asyncio
async def test_chatbot_require_shell_uses_container_shell_for_non_gpt_model(
    monkeypatch,
) -> None:
    custom_chatbot = chatbot.build_chatbot(
        model="claude-3-7-sonnet",
        rule=[],
        toolkit=[],
        schema=[],
        require_shell=True,
        working_dir="/workspace",
    )

    async def fake_start(self, *, room) -> None:
        self._room = room

    monkeypatch.setattr(custom_chatbot.__mro__[1], "start", fake_start)

    agent = custom_chatbot()

    await agent.start(room=_FakeShellRoom())

    assert isinstance(agent.shell_tool, ContainerShellTool)
    assert agent.shell_tool.working_dir == "/workspace"


def test_chatbot_rejects_computer_use() -> None:
    with pytest.raises(click.exceptions.Exit) as exc_info:
        chatbot.build_chatbot(
            model="gpt-5",
            rule=[],
            toolkit=[],
            schema=[],
            require_computer_use=True,
        )

    assert exc_info.value.exit_code == 1


@pytest.mark.asyncio
async def test_chatbot_get_rules_loads_instructions_from_configured_storage(
    tmp_path: Path,
) -> None:
    instructions_file = tmp_path / "instructions.txt"
    instructions_file.write_text(
        "global instruction\n[web]\nweb instruction\n",
        encoding="utf-8",
    )

    class _RulesParticipant:
        def __init__(self, *, client: str | None) -> None:
            self._client = client

        def get_attribute(self, name: str) -> str | None:
            if name == "client":
                return self._client
            return None

    custom_chatbot = chatbot.build_chatbot(
        client=_FakeProcessRoomClient(),
        model="gpt-5",
        rule=["base rule"],
        toolkit=[],
        schema=[],
        instructions=["instructions.txt"],
        require_storage=True,
        storage_tool_local_paths=[f"{tmp_path}:/:ro"],
    )
    agent = custom_chatbot()

    rules = await agent.get_rules(
        thread_context=None,
        participant=_RulesParticipant(client="web"),
    )

    assert "base rule" in rules
    assert "global instruction" in rules
    assert "web instruction" in rules


@pytest.mark.asyncio
async def test_process_agent_get_rules_loads_instructions_from_current_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    instructions_file = tmp_path / "instructions.txt"
    instructions_file.write_text(
        "cwd instruction\n",
        encoding="utf-8",
    )

    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5",
        rule=["base rule"],
        toolkit=[],
        schema=[],
        instructions=["instructions.txt"],
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()

    rules = await agent.get_rules(participant=None)

    assert "base rule" in rules
    assert "cwd instruction" in rules


@pytest.mark.asyncio
async def test_process_agent_get_rules_loads_instructions_from_parent_relative_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.chdir(app_dir)

    instructions_file = tmp_path / "shared" / "instructions.txt"
    instructions_file.parent.mkdir()
    instructions_file.write_text(
        "shared instruction\n",
        encoding="utf-8",
    )

    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5",
        rule=["base rule"],
        toolkit=[],
        schema=[],
        instructions=["../shared/instructions.txt"],
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()

    rules = await agent.get_rules(participant=None)

    assert "base rule" in rules
    assert "shared instruction" in rules


@pytest.mark.asyncio
async def test_process_agent_get_rules_warns_when_instructions_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    caplog.set_level("WARNING")

    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5",
        rule=["base rule"],
        toolkit=[],
        schema=[],
        instructions=["missing.txt"],
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()

    rules = await agent.get_rules(participant=None)

    assert "base rule" in rules
    assert "unable to load instructions from missing.txt" in caplog.text


@pytest.mark.asyncio
async def test_chatbot_require_advanced_shell_uses_container_toolkit_with_shell_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPIED_ENV", "copied")
    custom_chatbot = chatbot.build_chatbot(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_advanced_shell=True,
        working_dir="/workspace",
        shell_image="python:3.13",
        shell_copy_env=["COPIED_ENV"],
        shell_set_env=["SET_ENV=set"],
        delegate_shell_token=True,
    )

    async def fake_start(self, *, room) -> None:
        self._room = room

    monkeypatch.setattr(custom_chatbot.__mro__[1], "start", fake_start)

    agent = custom_chatbot()

    await agent.start(room=_FakeShellRoom())

    assert isinstance(agent.advanced_shell_toolkit, ContainerToolkit)
    assert agent.advanced_shell_toolkit.default_working_dir == "/workspace"
    assert agent.advanced_shell_toolkit.default_image == "python:3.13"
    assert agent.advanced_shell_toolkit.default_env == {
        "COPIED_ENV": "copied",
        "SET_ENV": "set",
        "MESHAGENT_TOKEN": "test-token",
        "OPENAI_API_KEY": "test-token",
        "ANTHROPIC_API_KEY": "test-token",
    }


@pytest.mark.asyncio
async def test_process_agent_require_advanced_shell_uses_container_toolkit_with_shell_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents.process as process_module

    monkeypatch.setenv("COPIED_ENV", "copied")
    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_advanced_shell=True,
        working_dir="/workspace",
        shell_image="python:3.13",
        shell_copy_env=["COPIED_ENV"],
        shell_set_env=["SET_ENV=set"],
        delegate_shell_token=True,
        channels=[],
    )
    agent = custom_process_agent()

    async def fake_install_requirements() -> None:
        return None

    async def fake_supervisor_start(self) -> None:
        return None

    async def fake_supervisor_stop(self) -> None:
        return None

    monkeypatch.setattr(agent, "install_requirements", fake_install_requirements)
    monkeypatch.setattr(process_module.AgentSupervisor, "start", fake_supervisor_start)
    monkeypatch.setattr(process_module.AgentSupervisor, "stop", fake_supervisor_stop)

    await agent.start(room=_FakeProcessRoomClient())
    try:
        assert isinstance(agent._advanced_shell_toolkit, ContainerToolkit)
        assert agent._advanced_shell_toolkit.default_working_dir == "/workspace"
        assert agent._advanced_shell_toolkit.default_image == "python:3.13"
        assert agent._advanced_shell_toolkit.default_env == {
            "COPIED_ENV": "copied",
            "SET_ENV": "set",
            "MESHAGENT_TOKEN": "token",
            "OPENAI_API_KEY": "token",
            "ANTHROPIC_API_KEY": "token",
        }
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_process_agent_require_advanced_shell_reuses_container_toolkit_across_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents.process as process_module

    custom_process_agent = chatbot.build_process_agent(
        model="gpt-5.5",
        rule=[],
        toolkit=[],
        schema=[],
        require_advanced_shell=True,
        working_dir="/workspace",
        shell_image="python:3.13",
        require_table_read=[],
        require_table_write=[],
        channels=[],
    )
    agent = custom_process_agent()

    async def fake_install_requirements() -> None:
        return None

    async def fake_supervisor_start(self) -> None:
        return None

    async def fake_supervisor_stop(self) -> None:
        return None

    async def _fake_get_required_toolkits(*, context):
        del context
        return []

    monkeypatch.setattr(agent, "install_requirements", fake_install_requirements)
    monkeypatch.setattr(agent, "get_required_toolkits", _fake_get_required_toolkits)
    monkeypatch.setattr(process_module.AgentSupervisor, "start", fake_supervisor_start)
    monkeypatch.setattr(process_module.AgentSupervisor, "stop", fake_supervisor_stop)

    await agent.start(room=_FakeProcessRoomClient())
    try:
        turns = [
            TurnStart(
                type="meshagent.agent.turn.start",
                thread_id="threads/example",
                content=[AgentTextContent(type="text", text="hello")],
            )
        ]

        first_toolkits = await agent.get_process_turn_toolkits(
            process=_FakeProcessState(),
            sender=None,
            model="gpt-5.5",
            turns=turns,
        )
        second_toolkits = await agent.get_process_turn_toolkits(
            process=_FakeProcessState(),
            sender=None,
            model="gpt-5.5",
            turns=turns,
        )

        first_advanced_toolkit = next(
            toolkit
            for toolkit in first_toolkits
            if isinstance(toolkit, ContainerToolkit)
        )
        second_advanced_toolkit = next(
            toolkit
            for toolkit in second_toolkits
            if isinstance(toolkit, ContainerToolkit)
        )

        assert first_advanced_toolkit is agent._advanced_shell_toolkit
        assert second_advanced_toolkit is agent._advanced_shell_toolkit
    finally:
        await agent.stop()
