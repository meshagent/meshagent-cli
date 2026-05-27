import asyncio

import click

from meshagent.api.specs.service import ContainerSpec, ServiceMetadata, ServiceSpec
from meshagent.cli import worker


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


def test_worker_spec_defaults_dataset_namespace(monkeypatch) -> None:
    fake_service = _FakeService()
    build_calls: list[dict[str, object]] = []

    def fake_get_service(*, host, port):
        del host
        del port
        return fake_service

    def fake_build_worker(**kwargs):
        build_calls.append(kwargs)
        return type("DummyWorker", (), {})

    monkeypatch.setattr(worker, "get_service", fake_get_service)
    monkeypatch.setattr(
        worker, "service_specs", lambda token_identity=None: [_service_spec()]
    )
    monkeypatch.setattr(worker, "build_worker", fake_build_worker)
    monkeypatch.setattr(worker, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker.sys,
        "argv",
        [
            "meshagent",
            "worker",
            "spec",
            "--agent-name",
            "helper",
            "--queue",
            "jobs",
        ],
    )

    async def invoke_spec() -> None:
        await worker.spec(agent_name="helper", queue="jobs")

    asyncio.run(invoke_spec())

    assert len(build_calls) == 1
    assert build_calls[0]["dataset_namespace"] == [".datasets"]


def test_worker_join_passes_room_jwt_as_api_key(monkeypatch) -> None:
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

    def fake_build_worker(**kwargs):
        build_calls.append(kwargs)
        return type("DummyWorker", (), {})

    monkeypatch.setenv("MESHAGENT_TOKEN", "test-token")
    monkeypatch.setattr(worker, "get_client", fake_get_client)
    monkeypatch.setattr(worker, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(worker, "resolve_key", fake_resolve_key)
    monkeypatch.setattr(worker, "resolve_room", lambda room_name=None: room_name)
    monkeypatch.setattr(worker, "build_worker", fake_build_worker)
    monkeypatch.setattr(worker, "get_deferred", lambda: True)
    monkeypatch.setattr(worker, "print", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker.sys,
        "argv",
        [
            "meshagent",
            "worker",
            "join",
            "--agent-name",
            "helper",
            "--room",
            "quickstart",
            "--queue",
            "jobs",
        ],
    )

    async def invoke_join() -> None:
        await worker.join(
            project_id=None,
            room="quickstart",
            agent_name="helper",
            queue="jobs",
        )

    root_command = click.Command("meshagent")
    worker_command = click.Command("worker")
    join_command = click.Command("join")
    with click.Context(root_command, info_name="meshagent") as root_context:
        with click.Context(
            worker_command,
            info_name="worker",
            parent=root_context,
        ) as worker_context:
            with click.Context(
                join_command,
                info_name="join",
                parent=worker_context,
            ):
                asyncio.run(invoke_join())

    assert len(build_calls) == 1
    assert build_calls[0]["api_key"] == "test-token"
