import asyncio

import click

from meshagent.api.specs.service import ContainerSpec, ServiceMetadata, ServiceSpec
from meshagent.cli import task_runner
from meshagent.cli.async_typer import get_command


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


def test_task_runner_spec_defaults_database_namespace(monkeypatch) -> None:
    fake_service = _FakeService()
    build_calls: list[dict[str, object]] = []

    def fake_get_service(*, host, port):
        del host
        del port
        return fake_service

    def fake_build_task_runner(**kwargs):
        build_calls.append(kwargs)
        return type("DummyTaskRunner", (), {})

    monkeypatch.setattr(task_runner, "get_service", fake_get_service)
    monkeypatch.setattr(
        task_runner,
        "service_specs",
        lambda token_identity=None: [_service_spec()],
    )
    monkeypatch.setattr(task_runner, "build_task_runner", fake_build_task_runner)
    monkeypatch.setattr(task_runner, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        task_runner.sys,
        "argv",
        [
            "meshagent",
            "task-runner",
            "spec",
            "--agent-name",
            "helper",
        ],
    )

    async def invoke_spec() -> None:
        await task_runner.spec(agent_name="helper")

    asyncio.run(invoke_spec())

    assert len(build_calls) == 1
    assert build_calls[0]["database_namespace"] == [".database"]
    assert "mcp" not in build_calls[0]
    assert "require_mcp" not in build_calls[0]


def test_task_runner_join_help_hides_mcp_flags() -> None:
    join_command = get_command(task_runner.app).commands["join"]
    visible_options = {
        option
        for param in join_command.params
        if isinstance(param, click.Option) and not param.hidden
        for option in param.opts
    }

    assert "--mcp" not in visible_options
    assert "--require-mcp" not in visible_options


def test_task_runner_join_passes_room_jwt_as_api_key(monkeypatch) -> None:
    build_calls: list[dict[str, object]] = []

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

    def fake_build_task_runner(**kwargs):
        build_calls.append(kwargs)
        return type("DummyTaskRunner", (), {})

    monkeypatch.setenv("MESHAGENT_TOKEN", "test-token")
    monkeypatch.setattr(task_runner, "get_client", fake_get_client)
    monkeypatch.setattr(task_runner, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(task_runner, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(task_runner, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(task_runner, "build_task_runner", fake_build_task_runner)
    monkeypatch.setattr(task_runner, "get_deferred", lambda: True)
    monkeypatch.setattr(task_runner, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        task_runner.sys,
        "argv",
        [
            "meshagent",
            "task-runner",
            "join",
            "--agent-name",
            "helper",
            "--room",
            "quickstart",
        ],
    )

    async def invoke_join() -> None:
        await task_runner.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
        )

    root_command = click.Command("meshagent")
    task_runner_command = click.Command("task-runner")
    join_command = click.Command("join")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            task_runner_command,
            info_name="task-runner",
            parent=root_context,
        ) as task_runner_context:
            with click.Context(
                join_command,
                info_name="join",
                parent=task_runner_context,
            ):
                asyncio.run(invoke_join())

    assert len(build_calls) == 1
    assert build_calls[0]["api_key"] == "test-token"
