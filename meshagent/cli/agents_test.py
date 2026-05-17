import json

import pytest

from meshagent.agents.chat_client import BaseChatClient
from meshagent.agents.messages import (
    AGENT_EVENT_THREAD_STARTED,
    AGENT_MESSAGE_THREAD_START,
    AgentMessage,
    AgentTextContent,
    StartThread,
)
from meshagent.api.client import ManagedAgent
from meshagent.api.managed_agents import (
    AllowedOpenAIModel,
    ManagedAgentMetadata,
    ManagedAgentSpec,
)
from meshagent.cli import agents


class _FakeAgentClient:
    def __init__(self) -> None:
        self.closed = False
        self.create_agent_calls: list[dict[str, object]] = []
        self.update_agent_calls: list[dict[str, object]] = []
        self.list_agents_calls: list[dict[str, object]] = []
        self.get_agent_calls: list[dict[str, object]] = []
        self.list_agents_result: list[ManagedAgent] = []
        self.get_agent_result: ManagedAgent | None = None

    async def create_agent(
        self,
        *,
        project_id: str,
        configuration: ManagedAgentSpec,
        if_not_exists: bool = False,
    ) -> ManagedAgent:
        self.create_agent_calls.append(
            {
                "project_id": project_id,
                "configuration": configuration,
                "if_not_exists": if_not_exists,
            }
        )
        return _sample_agent(configuration=configuration)

    async def update_agent(
        self,
        *,
        project_id: str,
        agent_id: str,
        configuration: ManagedAgentSpec,
    ) -> None:
        self.update_agent_calls.append(
            {
                "project_id": project_id,
                "agent_id": agent_id,
                "configuration": configuration,
            }
        )

    async def list_agents(
        self,
        *,
        project_id: str,
        limit: int,
        offset: int,
        order_by: str,
        filter: str | None = None,
    ) -> list[ManagedAgent]:
        self.list_agents_calls.append(
            {
                "project_id": project_id,
                "limit": limit,
                "offset": offset,
                "order_by": order_by,
                "filter": filter,
            }
        )
        return self.list_agents_result

    async def get_agent(self, *, project_id: str, name: str) -> ManagedAgent:
        self.get_agent_calls.append({"project_id": project_id, "name": name})
        if self.get_agent_result is None:
            raise AssertionError("get_agent_result was not configured")
        return self.get_agent_result

    async def close(self) -> None:
        self.closed = True


class _FakeWebSocketChatClient(BaseChatClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = True
        self.sent: list[AgentMessage] = []

    async def _start_transport(self) -> None:
        self.started = True

    async def _stop_transport(self) -> None:
        self.started = False

    async def _send_agent_message(self, payload: AgentMessage) -> None:
        if not self.started:
            raise AssertionError("chat client was stopped before send")
        self.sent.append(payload)
        if isinstance(payload, StartThread):
            self._handle_agent_payload(
                {
                    "type": AGENT_EVENT_THREAD_STARTED,
                    "thread_id": "thread-1",
                    "source_message_id": payload.message_id,
                }
            )


def _sample_configuration(*, agent_id: str | None = "agent-1") -> ManagedAgentSpec:
    return ManagedAgentSpec(
        id=agent_id,
        metadata=ManagedAgentMetadata(name="planner"),
        allowed_models=[AllowedOpenAIModel(model="gpt-4.1")],
    )


def _sample_agent(
    *,
    configuration: ManagedAgentSpec | None = None,
) -> ManagedAgent:
    configuration = configuration or _sample_configuration()
    return ManagedAgent(
        id=configuration.id or "agent-1",
        name=configuration.name,
        configuration=configuration,
    )


def _configuration_json(configuration: ManagedAgentSpec | None = None) -> str:
    return json.dumps(
        (configuration or _sample_configuration()).model_dump(mode="json")
    )


def _patch_agent_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: _FakeAgentClient,
) -> None:
    async def fake_get_client() -> _FakeAgentClient:
        return client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(agents, "get_client", fake_get_client)
    monkeypatch.setattr(agents, "resolve_project_id", fake_resolve_project_id)


@pytest.mark.asyncio
async def test_agent_create_command_uses_configuration_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    _patch_agent_command(monkeypatch, client=client)
    monkeypatch.setattr(agents, "print", lambda *args, **kwargs: None)

    await agents.agent_create_command(
        project_id="project-1",
        configuration=_configuration_json(),
        if_not_exists=True,
    )

    assert client.create_agent_calls == [
        {
            "project_id": "resolved-project",
            "configuration": _sample_configuration(),
            "if_not_exists": True,
        }
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_agent_create_command_can_override_thread_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    _patch_agent_command(monkeypatch, client=client)
    monkeypatch.setattr(agents, "print", lambda *args, **kwargs: None)

    await agents.agent_create_command(
        project_id="project-1",
        configuration=_configuration_json(),
        thread_isolation="participant",
        if_not_exists=False,
    )

    configuration = client.create_agent_calls[0]["configuration"]
    assert isinstance(configuration, ManagedAgentSpec)
    assert configuration.thread_isolation == "participant"
    assert client.closed is True


@pytest.mark.asyncio
async def test_agent_update_command_uses_configuration_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    client.get_agent_result = _sample_agent()
    _patch_agent_command(monkeypatch, client=client)
    monkeypatch.setattr(agents, "print", lambda *args, **kwargs: None)

    await agents.agent_update_command(
        project_id="project-1",
        name="planner",
        configuration=_configuration_json(),
    )

    assert client.get_agent_calls == [
        {"project_id": "resolved-project", "name": "planner"}
    ]
    assert client.update_agent_calls == [
        {
            "project_id": "resolved-project",
            "agent_id": "agent-1",
            "configuration": _sample_configuration(),
        }
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_agent_update_command_can_override_thread_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    client.get_agent_result = _sample_agent()
    _patch_agent_command(monkeypatch, client=client)
    monkeypatch.setattr(agents, "print", lambda *args, **kwargs: None)

    await agents.agent_update_command(
        project_id="project-1",
        name="planner",
        configuration=_configuration_json(),
        thread_isolation="participant",
    )

    configuration = client.update_agent_calls[0]["configuration"]
    assert isinstance(configuration, ManagedAgentSpec)
    assert configuration.thread_isolation == "participant"
    assert client.closed is True


@pytest.mark.asyncio
async def test_agent_list_command_outputs_current_agent_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    client.list_agents_result = [_sample_agent()]
    printed: list[tuple[list[dict[str, object]], tuple[str, ...]]] = []
    _patch_agent_command(monkeypatch, client=client)

    def fake_print_json_table(records: list[dict[str, object]], *cols: str) -> None:
        printed.append((records, cols))

    monkeypatch.setattr(agents, "print_json_table", fake_print_json_table)

    await agents.agent_list_command(project_id="project-1")

    assert printed == [
        (
            [
                {
                    "id": "agent-1",
                    "name": "planner",
                    "configuration": _sample_configuration().model_dump(mode="json"),
                }
            ],
            ("id", "name"),
        )
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_agent_use_session_starts_thread_without_stopping_websocket_client() -> (
    None
):
    client = _FakeWebSocketChatClient()
    pending_session = client.create_thread_session(
        local_participant_name="you",
        close_client_on_close=True,
    )
    session = agents._AgentUseSession(chat_client=pending_session)

    started_session = await session._start_thread(
        StartThread(
            type=AGENT_MESSAGE_THREAD_START,
            content=[AgentTextContent(type="text", text="hello")],
        )
    )

    assert client.started is True
    assert started_session.thread_path == "thread-1"
    assert [message.type for message in client.sent] == [AGENT_MESSAGE_THREAD_START]


@pytest.mark.asyncio
async def test_agent_use_command_runs_tui_with_resolved_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeAgentClient()
    calls: list[dict[str, object]] = []

    async def fake_run_agent_use_tui(**kwargs: object) -> None:
        calls.append(kwargs)

    _patch_agent_command(monkeypatch, client=client)
    monkeypatch.setattr(agents, "_run_agent_use_tui", fake_run_agent_use_tui)

    await agents.agent_use_command(
        "planner",
        project_id="project-1",
        thread_path=".threads/planner/main.thread",
        message="hello",
        load=True,
        since_turn="turn-1",
        o="json",
    )

    assert calls == [
        {
            "account_client": client,
            "project_id": "resolved-project",
            "agent_name": "planner",
            "thread_path": ".threads/planner/main.thread",
            "message": "hello",
            "load": True,
            "since_turn": "turn-1",
            "output": "json",
        }
    ]
    assert client.closed is True
