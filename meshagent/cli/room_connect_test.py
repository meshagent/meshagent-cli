from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from meshagent.cli import room_connect
from meshagent.cli import cli as root_cli
from meshagent.cli.async_typer import get_command


class _FakeAccountClient:
    def __init__(self) -> None:
        self.base_url = "https://api.example.meshagent.test"
        self.closed = False
        self.connect_calls: list[dict[str, str]] = []

    async def connect_room(self, *, project_id: str, room: str) -> SimpleNamespace:
        self.connect_calls.append({"project_id": project_id, "room": room})
        return SimpleNamespace(
            jwt="room-jwt",
            room_name="connected-room",
        )

    async def close(self) -> None:
        self.closed = True


def test_room_connect_runs_command_with_connected_room_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client = _FakeAccountClient()
    captured_run: dict[str, object] = {}

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    def _fake_run(command, *, check: bool, env: dict[str, str]):
        captured_run["command"] = command
        captured_run["check"] = check
        captured_run["env"] = env
        return SimpleNamespace(returncode=23)

    monkeypatch.setattr(room_connect, "get_client", _fake_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(room_connect.subprocess, "run", _fake_run)
    monkeypatch.setenv("UNCHANGED_ENV", "keep-me")

    result = CliRunner().invoke(
        get_command(root_cli.app),
        [
            "room",
            "connect",
            "--project-id",
            "project-input",
            "--room",
            "room-input",
            "--",
            "python",
            "-c",
            "print('hello')",
        ],
    )

    assert result.exit_code == 23
    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "room-input"}
    ]
    assert account_client.closed is True
    assert captured_run["command"] == ["python", "-c", "print('hello')"]
    assert captured_run["check"] is False
    captured_env = captured_run["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["UNCHANGED_ENV"] == "keep-me"
    assert captured_env["MESHAGENT_API_URL"] == "https://api.example.meshagent.test"
    assert captured_env["MESHAGENT_TOKEN"] == "room-jwt"
    assert captured_env["MESHAGENT_ROOM"] == "connected-room"


def test_room_connect_requires_command_after_separator() -> None:
    result = CliRunner().invoke(
        get_command(root_cli.app),
        ["room", "connect", "--"],
    )

    assert result.exit_code == 2
    assert "Pass the local command after --" in result.output
