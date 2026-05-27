from types import SimpleNamespace

import pytest
import typer
from click.testing import CliRunner

from meshagent.api import ApiScope, ParticipantToken
from meshagent.api.client import NotFoundError
from meshagent.cli import room_connect
from meshagent.cli import cli as root_cli
from meshagent.cli.async_typer import get_command

_LOCAL_SIGNING_SECRET = "local-room-signing-secret-1234567890"


class _FakeAccountClient:
    def __init__(
        self,
        *,
        room_token: str = "room-jwt",
        room_name: str = "connected-room",
        room_url: str = "wss://room-proxy.meshagent.test/rooms/connected-room",
        secret_values: dict[tuple[str | None, str], bytes] | None = None,
    ) -> None:
        self.base_url = "https://api.example.meshagent.test"
        self.closed = False
        self.room_token = room_token
        self.room_name = room_name
        self.room_url = room_url
        self.secret_values = secret_values or {}
        self.connect_calls: list[dict[str, str]] = []
        self.secret_calls: list[dict[str, str | None]] = []

    async def connect_room(self, *, project_id: str, room: str) -> SimpleNamespace:
        self.connect_calls.append({"project_id": project_id, "room": room})
        return SimpleNamespace(
            jwt=self.room_token,
            room_name=self.room_name,
            room_url=self.room_url,
        )

    async def get_room_secret(
        self,
        *,
        project_id: str,
        room_name: str,
        secret_id: str,
        delegated_to: str | None = None,
        for_identity: str | None = None,
    ) -> SimpleNamespace:
        del delegated_to
        self.secret_calls.append(
            {
                "project_id": project_id,
                "room_name": room_name,
                "secret_id": secret_id,
                "for_identity": for_identity,
            }
        )
        secret_key = (for_identity, secret_id)
        if secret_key not in self.secret_values:
            raise NotFoundError(f"missing secret {secret_key}")
        return SimpleNamespace(data=self.secret_values[secret_key])

    async def close(self) -> None:
        self.closed = True


def test_room_connect_help_mentions_llm_token_aliases() -> None:
    help_text = room_connect.connect_command.help

    assert isinstance(help_text, str)
    assert "MESHAGENT_PROJECT_ID" in help_text
    assert "MESHAGENT_TOKEN" in help_text
    assert "OPENAI_API_KEY" in help_text
    assert "ANTHROPIC_API_KEY" in help_text


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
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://env.meshagent.test",
    )
    monkeypatch.setattr(room_connect.subprocess, "run", _fake_run)
    monkeypatch.setenv("UNCHANGED_ENV", "keep-me")

    exit_code = room_connect.connect_command.main(
        args=[
            "--project-id",
            "project-input",
            "--room",
            "room-input",
            "--env",
            "EXTRA_ENV=extra-value",
            "--",
            "python",
            "-c",
            "print('hello')",
        ],
        standalone_mode=False,
    )

    assert exit_code == 23
    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "room-input"}
    ]
    assert account_client.closed is True
    assert captured_run["command"] == ["python", "-c", "print('hello')"]
    assert captured_run["check"] is False
    captured_env = captured_run["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["UNCHANGED_ENV"] == "keep-me"
    assert captured_env["EXTRA_ENV"] == "extra-value"
    assert captured_env["MESHAGENT_API_URL"] == "https://env.meshagent.test"
    assert captured_env["MESHAGENT_PROJECT_ID"] == "project-1"
    assert captured_env["MESHAGENT_TOKEN"] == "room-jwt"
    assert captured_env["MESHAGENT_ROOM"] == "connected-room"
    assert (
        captured_env["OPENAI_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/connected-room/openai/v1"
    )
    assert (
        captured_env["ANTHROPIC_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/connected-room/anthropic"
    )
    assert captured_env["OPENAI_API_KEY"] == "room-jwt"
    assert captured_env["ANTHROPIC_API_KEY"] == "room-jwt"


def test_room_connect_ignores_ambient_participant_token_for_other_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient_token = ParticipantToken(name="ambient-agent", project_id="project-1")
    ambient_token.add_room_grant("old-room")
    ambient_token.add_api_grant(ApiScope.agent_default())
    captured_run: dict[str, object] = {}

    async def _fake_get_access_token() -> str:
        return "oauth-access-token"

    async def _fake_connect_room(
        self: room_connect.CustomMeshagentClient,
        *,
        project_id: str,
        room: str,
    ) -> SimpleNamespace:
        assert self.token == "oauth-access-token"
        assert project_id == "project-1"
        assert room == "target-room"
        return SimpleNamespace(
            jwt="target-room-jwt",
            room_name=room,
            room_url="wss://room-proxy.meshagent.test/rooms/target-room",
        )

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id is None
        return "project-1"

    def _fake_run(command, *, check: bool, env: dict[str, str]):
        captured_run["command"] = command
        captured_run["check"] = check
        captured_run["env"] = env
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv(
        "MESHAGENT_TOKEN",
        ambient_token.to_jwt(token="ambient-signing-secret-with-enough-bytes"),
    )
    monkeypatch.setenv("MESHAGENT_ROOM", "old-room")
    monkeypatch.delenv("MESHAGENT_API_KEY", raising=False)
    monkeypatch.delenv("MESHAGENT_SESSION_ID", raising=False)
    monkeypatch.setattr(
        room_connect.auth_async, "get_access_token", _fake_get_access_token
    )
    monkeypatch.setattr(
        room_connect.CustomMeshagentClient,
        "connect_room",
        _fake_connect_room,
    )
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://env.meshagent.test",
    )
    monkeypatch.setattr(room_connect.subprocess, "run", _fake_run)

    exit_code = get_command(root_cli.app).main(
        args=[
            "room",
            "connect",
            "--room",
            "target-room",
            "--",
            "env",
        ],
        prog_name="meshagent",
        standalone_mode=False,
    )

    assert exit_code == 0
    assert captured_run["command"] == ["env"]
    captured_env = captured_run["env"]
    assert isinstance(captured_env, dict)
    assert captured_env["MESHAGENT_TOKEN"] == "target-room-jwt"
    assert captured_env["MESHAGENT_ROOM"] == "target-room"


@pytest.mark.asyncio
async def test_room_connect_env_satisfies_llm_proxy_docs_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client = _FakeAccountClient()

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _fake_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setenv("MESHAGENT_API_URL", "https://env.meshagent.test")

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=(),
        identity=None,
        meshagent_token=None,
    )

    sample_required_env = {
        "MESHAGENT_TOKEN",
        "MESHAGENT_PROJECT_ID",
        "MESHAGENT_ROOM",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
    }
    assert sample_required_env <= child_env.keys()
    assert child_env["MESHAGENT_TOKEN"] == "room-jwt"
    assert child_env["OPENAI_API_KEY"] == child_env["MESHAGENT_TOKEN"]
    assert child_env["ANTHROPIC_API_KEY"] == child_env["MESHAGENT_TOKEN"]
    assert (
        child_env["OPENAI_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/connected-room/openai/v1"
    )
    assert (
        child_env["ANTHROPIC_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/connected-room/anthropic"
    )


@pytest.mark.asyncio
async def test_room_connect_none_template_skips_added_room_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client = _FakeAccountClient()

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _fake_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    for name in (
        "MESHAGENT_TOKEN",
        "MESHAGENT_PROJECT_ID",
        "MESHAGENT_ROOM",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=(),
        identity=None,
        meshagent_token=None,
        template="none",
    )

    assert account_client.connect_calls == [
        {"project_id": "project-1", "room": "room-input"}
    ]
    for name in (
        "MESHAGENT_TOKEN",
        "MESHAGENT_PROJECT_ID",
        "MESHAGENT_ROOM",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
    ):
        assert name not in child_env


@pytest.mark.asyncio
async def test_room_connect_uses_default_api_url_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client = _FakeAccountClient()

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _fake_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://default.meshagent.test",
    )
    monkeypatch.delenv("MESHAGENT_API_URL", raising=False)

    room_env = await room_connect._connect_room_env(
        project_id="project-input",
        room="room-input",
    )

    assert room_env.api_url == "https://default.meshagent.test"
    assert room_env.room_name == "connected-room"
    assert room_env.room_url == "https://room-proxy.meshagent.test/rooms/connected-room"
    assert room_env.token == "room-jwt"
    assert account_client.closed is True


@pytest.mark.asyncio
async def test_room_connect_build_env_requires_identity_for_secret_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    with pytest.raises(
        typer.BadParameter, match="--identity is required when using --env-secret"
    ):
        await room_connect._build_connected_command_env(
            project_id="project-input",
            room="room-input",
            env=("PLAIN_ENV=plain-value",),
            env_secret=("DB_PASSWORD=db-password",),
            identity=None,
            meshagent_token=None,
        )


@pytest.mark.asyncio
async def test_room_connect_build_env_requires_identity_for_meshagent_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    with pytest.raises(
        typer.BadParameter,
        match="--identity is required when using --meshagent-token",
    ):
        await room_connect._build_connected_command_env(
            project_id="project-input",
            room="room-input",
            env=(),
            env_secret=(),
            identity=None,
            meshagent_token="full",
        )


@pytest.mark.asyncio
async def test_room_connect_build_env_requires_identity_for_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)

    with pytest.raises(
        typer.BadParameter, match="--identity is required when using --role"
    ):
        await room_connect._build_connected_command_env(
            project_id="project-input",
            room="room-input",
            env=(),
            env_secret=(),
            identity=None,
            role="user",
            meshagent_token=None,
        )


@pytest.mark.asyncio
async def test_room_connect_build_env_with_identity_mints_local_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_get_client() -> _FakeAccountClient:
        raise AssertionError("get_client should not be called without --env-secret")

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def _fake_resolve_key(*, project_id: str | None, key: str | None) -> None:
        assert project_id == "project-1"
        assert key is None
        return None

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _unexpected_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_key", _fake_resolve_key)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://default.meshagent.test",
    )
    monkeypatch.setattr(
        room_connect,
        "websocket_room_url",
        lambda *, room_name: f"wss://room-proxy.meshagent.test/rooms/{room_name}",
    )
    monkeypatch.setenv("MESHAGENT_SECRET", _LOCAL_SIGNING_SECRET)

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=(),
        identity="agent-name",
        meshagent_token=None,
    )

    minted_token = ParticipantToken.from_jwt(
        child_env["MESHAGENT_TOKEN"],
        token=_LOCAL_SIGNING_SECRET,
    )
    assert minted_token.name == "agent-name"
    assert minted_token.role == "agent"
    assert minted_token.grant_scope("room") == "room-input"
    assert minted_token.get_api_grant() == ApiScope.agent_default()
    assert child_env["MESHAGENT_PROJECT_ID"] == "project-1"
    assert child_env["MESHAGENT_ROOM"] == "room-input"
    assert (
        child_env["OPENAI_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/room-input/openai/v1"
    )
    assert (
        child_env["ANTHROPIC_BASE_URL"]
        == "https://room-proxy.meshagent.test/rooms/room-input/anthropic"
    )
    assert child_env["OPENAI_API_KEY"] == child_env["MESHAGENT_TOKEN"]
    assert child_env["ANTHROPIC_API_KEY"] == child_env["MESHAGENT_TOKEN"]


@pytest.mark.asyncio
async def test_room_connect_build_env_with_identity_mints_requested_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_get_client() -> _FakeAccountClient:
        raise AssertionError("get_client should not be called without --env-secret")

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def _fake_resolve_key(*, project_id: str | None, key: str | None) -> None:
        assert project_id == "project-1"
        assert key is None
        return None

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _unexpected_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_key", _fake_resolve_key)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://default.meshagent.test",
    )
    monkeypatch.setattr(
        room_connect,
        "websocket_room_url",
        lambda *, room_name: f"wss://room-proxy.meshagent.test/rooms/{room_name}",
    )
    monkeypatch.setenv("MESHAGENT_SECRET", _LOCAL_SIGNING_SECRET)

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=(),
        identity="user-name",
        role="user",
        meshagent_token=None,
    )

    minted_token = ParticipantToken.from_jwt(
        child_env["MESHAGENT_TOKEN"],
        token=_LOCAL_SIGNING_SECRET,
    )
    assert minted_token.name == "user-name"
    assert minted_token.role == "user"
    assert minted_token.grant_scope("room") == "room-input"


@pytest.mark.asyncio
async def test_room_connect_build_env_with_identity_and_meshagent_token_mints_local_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_get_client() -> _FakeAccountClient:
        raise AssertionError("get_client should not be called without --env-secret")

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def _fake_resolve_key(*, project_id: str | None, key: str | None) -> None:
        assert project_id == "project-1"
        assert key is None
        return None

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _unexpected_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_key", _fake_resolve_key)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://default.meshagent.test",
    )
    monkeypatch.setattr(
        room_connect,
        "websocket_room_url",
        lambda *, room_name: f"wss://room-proxy.meshagent.test/rooms/{room_name}",
    )
    monkeypatch.setenv("MESHAGENT_SECRET", _LOCAL_SIGNING_SECRET)

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=(),
        identity="agent-name",
        meshagent_token="full",
    )

    minted_token = ParticipantToken.from_jwt(
        child_env["MESHAGENT_TOKEN"],
        token=_LOCAL_SIGNING_SECRET,
    )
    assert minted_token.name == "agent-name"
    assert minted_token.role == "agent"
    assert minted_token.grant_scope("room") == "room-input"
    assert minted_token.get_api_grant() == ApiScope.full()
    assert child_env["MESHAGENT_PROJECT_ID"] == "project-1"
    assert child_env["OPENAI_API_KEY"] == child_env["MESHAGENT_TOKEN"]
    assert child_env["ANTHROPIC_API_KEY"] == child_env["MESHAGENT_TOKEN"]


@pytest.mark.asyncio
async def test_room_connect_build_env_with_identity_fetches_secret_without_connect_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_client = _FakeAccountClient(
        secret_values={
            ("agent-name", "db-password"): b"topsecret",
        }
    )

    async def _fake_get_client() -> _FakeAccountClient:
        return account_client

    async def _fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-input"
        return "project-1"

    async def _fake_resolve_key(*, project_id: str | None, key: str | None) -> None:
        assert project_id == "project-1"
        assert key is None
        return None

    def _fake_resolve_room(room: str | None) -> str | None:
        assert room == "room-input"
        return room

    monkeypatch.setattr(room_connect, "get_client", _fake_get_client)
    monkeypatch.setattr(room_connect, "resolve_project_id", _fake_resolve_project_id)
    monkeypatch.setattr(room_connect, "resolve_key", _fake_resolve_key)
    monkeypatch.setattr(room_connect, "resolve_room", _fake_resolve_room)
    monkeypatch.setattr(
        room_connect,
        "resolve_api_url",
        lambda: "https://default.meshagent.test",
    )
    monkeypatch.setattr(
        room_connect,
        "websocket_room_url",
        lambda *, room_name: f"wss://room-proxy.meshagent.test/rooms/{room_name}",
    )
    monkeypatch.setenv("MESHAGENT_SECRET", _LOCAL_SIGNING_SECRET)

    child_env = await room_connect._build_connected_command_env(
        project_id="project-input",
        room="room-input",
        env=(),
        env_secret=("DB_PASSWORD=db-password",),
        identity="agent-name",
        meshagent_token=None,
    )

    assert child_env["DB_PASSWORD"] == "topsecret"
    assert child_env["MESHAGENT_PROJECT_ID"] == "project-1"
    assert account_client.connect_calls == []
    assert account_client.secret_calls == [
        {
            "project_id": "project-1",
            "room_name": "room-input",
            "secret_id": "db-password",
            "for_identity": "agent-name",
        }
    ]
    assert account_client.closed is True


def test_room_connect_requires_command_after_separator() -> None:
    result = CliRunner().invoke(room_connect.connect_command, ["--"])

    assert result.exit_code == 2
    assert "Pass the local command after --" in result.output
