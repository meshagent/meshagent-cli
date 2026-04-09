import asyncio

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


def test_worker_spec_defaults_database_namespace(monkeypatch) -> None:
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
    assert build_calls[0]["database_namespace"] == [".database"]
