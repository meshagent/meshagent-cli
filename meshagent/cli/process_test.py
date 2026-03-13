import asyncio

import click
import pytest
from typer.main import get_command

from meshagent.agents.messages import AgentTextContent, TurnStart
from meshagent.api.specs.service import ContainerSpec, ServiceMetadata, ServiceSpec
from meshagent.cli import chatbot
from meshagent.cli import cli as root_cli
from meshagent.computers.agent import ComputerToolkit
from meshagent.tools import Toolkit


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


def test_root_cli_registers_process_group() -> None:
    command = get_command(root_cli.app)
    assert "process" in command.commands


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


def test_resolved_channels_accept_toolkit_channel() -> None:
    assert chatbot._resolved_channels(
        runtime="process",
        channel=["toolkit:assistant"],
    ) == ["toolkit:assistant"]


def test_process_spec_uses_process_runtime_and_chat_channel(monkeypatch) -> None:
    fake_service = _FakeService()
    build_calls: list[dict[str, object]] = []
    printed: list[str] = []

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
            "--channel",
            "chat",
        ],
    )

    async def invoke_spec() -> None:
        await chatbot.spec(
            agent_name="helper",
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
    assert build_calls[0]["channels"] == ["chat"]
    assert len(fake_service.agents) == 1
    assert fake_service.agents[0].annotations == {
        "meshagent.agent.type": "ChatBot",
    }
    assert len(printed) == 1
    assert "meshagent process join --agent-name helper --channel chat" in printed[0]


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


class _FakeProcessRoom:
    def __init__(self) -> None:
        self.local_participant = _FakeParticipant()


class _FakeProcessState:
    def __init__(self) -> None:
        self.thread_id = "threads/example"
        self.thread_adapter = None
        self.supervisor = None
        self.session_context = None


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
