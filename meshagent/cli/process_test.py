import asyncio
import inspect
from pathlib import Path

import click
import pytest

from meshagent.agents.context import AgentSessionContext
from meshagent.agents.messages import (
    AGENT_MESSAGE_TURN_START,
    AGENT_MESSAGE_TURN_STEER,
    AgentTextContent,
    TurnStart,
    TurnSteer,
)
from meshagent.agents.process import Message
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

    agent_cls = chatbot.build_process_agent(
        model="gpt-5.4",
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

    agent_cls = chatbot.build_process_agent(
        model="gpt-5.4",
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
        ) -> None:
            self._model = model if model is not None else "default-model"
            self.api_key = api_key
            self.response_options = response_options
            self.log_requests = log_requests
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
        model="gpt-5.4",
        rule=[],
        toolkit=[],
        schema=[],
        channels=["chat", "queue:jobs", "mail:mailbox@mail.meshagent.com"],
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
        assert main_adapter.default_model() == "gpt-5.4"
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
        ],
    )

    async def invoke_spec() -> None:
        await chatbot.spec(
            agent_name="helper",
            decision_model="gpt-5.4-nano",
            channel=["chat"],
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
    assert build_calls[0]["database_namespace"] is None
    assert "mcp" not in build_calls[0]
    assert build_calls[0]["require_mcp"] is False
    assert len(fake_service.agents) == 1
    assert fake_service.agents[0].annotations == {
        "meshagent.agent.type": "ChatBot",
    }
    assert len(printed) == 1
    assert "meshagent process join --agent-name helper" in printed[0]


def test_chatbot_spec_defaults_database_namespace(monkeypatch) -> None:
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
    assert build_calls[0]["database_namespace"] == [".database"]
    assert "mcp" not in build_calls[0]
    assert build_calls[0]["require_mcp"] is False


def test_process_join_passes_supported_builder_kwargs(monkeypatch) -> None:
    build_calls: list[dict[str, object]] = []
    allowed_build_kwargs = set(
        inspect.signature(chatbot.build_process_agent).parameters
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
    monkeypatch.setattr(chatbot, "get_client", fake_get_client)
    monkeypatch.setattr(chatbot, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(chatbot, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(chatbot, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(chatbot, "build_process_agent", fake_build_process_agent)
    monkeypatch.setattr(chatbot, "build_chatbot", fail_build_chatbot)
    monkeypatch.setattr(chatbot, "get_deferred", lambda: True)
    monkeypatch.setattr(
        chatbot.sys,
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
        ],
    )

    async def invoke_join() -> None:
        await chatbot.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
            channel=["chat"],
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
        self.thread_adapter = None
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

    def restore_session_context(self, *, context) -> None:
        del context

    def make_toolkit(self):
        return Toolkit(name="thread", tools=[])


class _SteeringRecordingAdapter:
    def __init__(self) -> None:
        self.session = AgentSessionContext()
        self.call_started = asyncio.Event()
        self.release_tool_boundary = asyncio.Event()
        self.calls: list[dict[str, object]] = []
        self.tool_call_approval_handler = None

    def default_model(self) -> str:
        return "gpt-5.4"

    def create_session(self) -> AgentSessionContext:
        return self.session

    def set_tool_call_approval_handler(self, handler) -> None:
        self.tool_call_approval_handler = handler

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
    agent_cls = chatbot.build_process_agent(
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
async def test_build_process_agent_forwards_tool_boundary_steering_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshagent.agents as agents_module
    import meshagent.agents.process as process_module

    fake_adapter = _SteeringRecordingAdapter()
    monkeypatch.setattr(
        chatbot,
        "OpenAIResponsesAdapter",
        lambda **kwargs: fake_adapter,
    )
    monkeypatch.setattr(
        agents_module,
        "AgentProcessThreadAdapter",
        _FakeProcessThreadAdapter,
    )
    monkeypatch.setattr(
        process_module,
        "AgentProcessThreadAdapter",
        _FakeProcessThreadAdapter,
    )

    agent_cls = chatbot.build_process_agent(
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
        model="gpt-5.4",
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
            model="gpt-5.4",
            turns=turns,
        )
        second_toolkits = await agent.get_process_turn_toolkits(
            process=_FakeProcessState(),
            sender=None,
            model="gpt-5.4",
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
