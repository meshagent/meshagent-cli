from pathlib import Path

import pytest
import typer

from meshagent.agents import Package
from meshagent.agents.package import Package as AgentPackage
from meshagent.agents.package import MeshagentPackage as PackagedMeshagentPackage
from meshagent.cli import agent_package_cli
from meshagent.cli.agent_package_cli import _load_package


def _write_agent_module(tmp_path: Path, body: str) -> Path:
    module_path = tmp_path / "agent_package.py"
    module_path.write_text(body, encoding="utf-8")
    return module_path


def test_load_package_supports_exported_instance(tmp_path: Path) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\nmain = Package(name='assistant')\n",
    )

    loaded = _load_package(module_path=str(module_path), export_name="main")

    assert isinstance(loaded, Package)
    assert loaded.name == "assistant"
    assert loaded._module_path == module_path.resolve()
    assert loaded._module_export_name == "main"
    assert loaded._module_export_is_factory is False


def test_load_package_supports_zero_arg_factory(tmp_path: Path) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\n"
        "def build():\n"
        "    return Package(name='assistant')\n",
    )

    loaded = _load_package(module_path=str(module_path), export_name="build")

    assert isinstance(loaded, Package)
    assert loaded.name == "assistant"
    assert loaded._module_path == module_path.resolve()
    assert loaded._module_export_name == "build"
    assert loaded._module_export_is_factory is True


def test_load_package_stores_exported_instance_paths_relative_to_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_dir = tmp_path / "agent"
    module_dir.mkdir()
    (module_dir / "rules.txt").write_text("rules", encoding="utf-8")
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    module_path = _write_agent_module(
        module_dir,
        "from meshagent.agents import Package\n"
        "main = Package(name='assistant').instructions('rules.txt')\n",
    )

    loaded = _load_package(module_path=str(module_path), export_name="main")

    assert loaded._instructions[0].source == Path("rules.txt")
    assert loaded._instructions[0].base_path == module_dir.resolve()
    assert (
        loaded._resolve_deploy_assets()[0].asset.source
        == (module_dir / "rules.txt").resolve()
    )


def test_load_package_stores_factory_paths_relative_to_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_dir = tmp_path / "agent"
    module_dir.mkdir()
    (module_dir / "rules.txt").write_text("rules", encoding="utf-8")
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)

    module_path = _write_agent_module(
        module_dir,
        "from meshagent.agents import Package\n"
        "def build():\n"
        "    return Package(name='assistant').instructions('rules.txt')\n",
    )

    loaded = _load_package(module_path=str(module_path), export_name="build")

    assert loaded._instructions[0].source == Path("rules.txt")
    assert loaded._instructions[0].base_path == module_dir.resolve()
    assert (
        loaded._resolve_deploy_assets()[0].asset.source
        == (module_dir / "rules.txt").resolve()
    )


@pytest.mark.asyncio
async def test_package_deploy_command_invokes_package_deploy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\nmain = Package(name='assistant')\n",
    )
    captured: dict[str, object] = {}

    async def _fake_deploy(
        *,
        package: Package,
        room: str,
        project_id: str | None = None,
        builder_name: str | None = None,
        status_callback=None,
    ) -> str:
        captured["package_name"] = package.name
        captured["room"] = room
        captured["project_id"] = project_id
        captured["builder_name"] = builder_name
        captured["status_callback"] = status_callback
        return "service-123"

    monkeypatch.setattr(agent_package_cli, "deploy_package", _fake_deploy)

    await agent_package_cli.deploy(
        module=str(module_path),
        room="demo-room",
        project_id="project-123",
        name="main",
        builder_name="builder.custom",
        verbose=False,
    )

    assert captured == {
        "package_name": "assistant",
        "room": "demo-room",
        "project_id": "project-123",
        "builder_name": "builder.custom",
        "status_callback": None,
    }


@pytest.mark.asyncio
async def test_package_run_command_invokes_package_run_and_tails_logs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\nmain = Package(name='assistant')\n",
    )
    captured: dict[str, object] = {}
    fake_client: object | None = None

    async def _fake_run(
        *,
        package: Package,
        room: str,
        project_id: str | None = None,
        builder_name: str | None = None,
        status_callback=None,
    ) -> str:
        captured["package_name"] = package.name
        captured["room"] = room
        captured["project_id"] = project_id
        captured["builder_name"] = builder_name
        captured["status_callback"] = status_callback
        return "container-123"

    class _FakeContainers:
        async def stop(self, *, container_id: str, force: bool = False) -> None:
            captured["stop"] = (container_id, force)

        async def delete(self, *, container_id: str) -> None:
            captured["delete"] = container_id

    class _FakeRoomClient:
        def __init__(self, *, protocol) -> None:
            del protocol
            self.containers = _FakeContainers()

        async def __aenter__(self):
            nonlocal fake_client
            fake_client = self
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            return None

    class _FakeAccountClient:
        async def connect_room(self, *, project_id: str, room: str):
            captured["connect_room"] = (project_id, room)
            return type(
                "_Connection",
                (),
                {"room_url": "ws://example.test/rooms/demo-room", "jwt": "caller-jwt"},
            )()

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_get_client():
        return _FakeAccountClient()

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-123"
        return "project-123"

    def _fake_resolve_room(room: str | None) -> str | None:
        return room

    async def _fake_stream(*, client, container_id: str) -> int:
        captured["stream_client"] = client
        captured["stream_container_id"] = container_id
        return 0

    monkeypatch.setattr(agent_package_cli, "run_package", _fake_run)
    monkeypatch.setattr(agent_package_cli, "get_client", _fake_get_client)
    monkeypatch.setattr(
        agent_package_cli, "resolve_project_id", _fake_resolve_project_id
    )
    monkeypatch.setattr(agent_package_cli, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(agent_package_cli, "RoomClient", _FakeRoomClient)
    monkeypatch.setattr(
        agent_package_cli,
        "_stream_container_job_logs_and_wait_for_exit",
        _fake_stream,
    )

    await agent_package_cli.run(
        module=str(module_path),
        room="demo-room",
        project_id="project-123",
        name="main",
        builder_name="builder.custom",
        verbose=False,
    )

    assert captured == {
        "package_name": "assistant",
        "room": "demo-room",
        "project_id": "project-123",
        "builder_name": "builder.custom",
        "status_callback": None,
        "connect_room": ("project-123", "demo-room"),
        "stream_client": fake_client,
        "stream_container_id": "container-123",
        "account_client_closed": True,
    }


@pytest.mark.asyncio
async def test_package_run_command_deletes_container_on_interrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\nmain = Package(name='assistant')\n",
    )
    captured: dict[str, object] = {}

    async def _fake_run(
        *,
        package: Package,
        room: str,
        project_id: str | None = None,
        builder_name: str | None = None,
        status_callback=None,
    ) -> str:
        captured["package_name"] = package.name
        captured["room"] = room
        captured["project_id"] = project_id
        captured["builder_name"] = builder_name
        captured["status_callback"] = status_callback
        return "container-123"

    class _FakeContainers:
        async def stop(self, *, container_id: str, force: bool = False) -> None:
            captured["stop"] = (container_id, force)

        async def delete(self, *, container_id: str) -> None:
            captured["delete"] = container_id

    class _FakeRoomClient:
        def __init__(self, *, protocol) -> None:
            del protocol
            self.containers = _FakeContainers()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            return None

    class _FakeAccountClient:
        async def connect_room(self, *, project_id: str, room: str):
            return type(
                "_Connection",
                (),
                {"room_url": "ws://example.test/rooms/demo-room", "jwt": "caller-jwt"},
            )()

        async def close(self) -> None:
            captured["account_client_closed"] = True

    async def _fake_get_client():
        return _FakeAccountClient()

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        return "project-123"

    def _fake_resolve_room(room: str | None) -> str | None:
        return room

    async def _fake_stream(*, client, container_id: str) -> int:
        del client, container_id
        raise KeyboardInterrupt()

    monkeypatch.setattr(agent_package_cli, "run_package", _fake_run)
    monkeypatch.setattr(agent_package_cli, "get_client", _fake_get_client)
    monkeypatch.setattr(
        agent_package_cli, "resolve_project_id", _fake_resolve_project_id
    )
    monkeypatch.setattr(agent_package_cli, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(agent_package_cli, "RoomClient", _FakeRoomClient)
    monkeypatch.setattr(
        agent_package_cli,
        "_stream_container_job_logs_and_wait_for_exit",
        _fake_stream,
    )

    with pytest.raises(typer.Exit) as excinfo:
        await agent_package_cli.run(
            module=str(module_path),
            room="demo-room",
            project_id="project-123",
            name="main",
            builder_name="builder.custom",
            verbose=False,
        )

    assert excinfo.value.exit_code == 130
    assert captured["stop"] == ("container-123", True)
    assert captured["delete"] == "container-123"
    assert captured["account_client_closed"] is True
    assert captured["builder_name"] == "builder.custom"
    assert captured["status_callback"] is None


def test_root_agents_module_exports_package_and_agent() -> None:
    import meshagent.agents as agents_module

    assert agents_module.Package is AgentPackage
    assert agents_module.MeshagentPackage is PackagedMeshagentPackage
    assert agents_module.deploy_package is agent_package_cli.deploy_package
    assert agents_module.run_package is agent_package_cli.run_package


@pytest.mark.asyncio
async def test_package_deploy_command_prints_verbose_file_list(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_path = _write_agent_module(
        tmp_path,
        "from meshagent.agents import Package\nmain = Package(name='assistant')\n",
    )
    captured: dict[str, object] = {}

    async def _fake_deploy(
        *,
        package: Package,
        room: str,
        project_id: str | None = None,
        builder_name: str | None = None,
        status_callback=None,
    ) -> str:
        captured["package_name"] = package.name
        captured["room"] = room
        captured["project_id"] = project_id
        captured["builder_name"] = builder_name
        captured["status_callback"] = status_callback
        return "service-123"

    def _fake_print_packaged_files(*, package: Package) -> None:
        captured["printed_package_name"] = package.name

    monkeypatch.setattr(agent_package_cli, "deploy_package", _fake_deploy)
    monkeypatch.setattr(
        agent_package_cli, "_print_packaged_files", _fake_print_packaged_files
    )

    await agent_package_cli.deploy(
        module=str(module_path),
        room="demo-room",
        project_id="project-123",
        name="main",
        builder_name="builder.custom",
        verbose=True,
    )

    assert captured == {
        "package_name": "assistant",
        "printed_package_name": "assistant",
        "room": "demo-room",
        "project_id": "project-123",
        "builder_name": "builder.custom",
        "status_callback": agent_package_cli._verbose_status_printer,
    }
