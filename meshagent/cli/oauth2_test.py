from types import SimpleNamespace

import pytest

from meshagent.cli import oauth2


class _FakeSecretsClient:
    def __init__(self, *, exists_result: bool) -> None:
        self.exists_result = exists_result
        self.exists_calls: list[dict[str, str | None]] = []

    async def exists(
        self,
        *,
        secret_id: str,
        delegated_to: str | None = None,
        for_identity: str | None = None,
    ) -> bool:
        self.exists_calls.append(
            {
                "secret_id": secret_id,
                "delegated_to": delegated_to,
                "for_identity": for_identity,
            }
        )
        return self.exists_result


class _FakeRoomClient:
    def __init__(self, *, secrets_client: _FakeSecretsClient) -> None:
        self.secrets = secrets_client

    async def __aenter__(self) -> "_FakeRoomClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        return None


class _FakeAccountClient:
    def __init__(self) -> None:
        self.closed = False
        self.connect_room_calls: list[tuple[str, str | None]] = []

    async def connect_room(
        self, *, project_id: str, room: str | None
    ) -> SimpleNamespace:
        self.connect_room_calls.append((project_id, room))
        return SimpleNamespace(jwt="token")

    async def close(self) -> None:
        self.closed = True


def _patch_secret_exists_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_client: _FakeAccountClient,
    secrets_client: _FakeSecretsClient,
) -> None:
    async def fake_get_client() -> _FakeAccountClient:
        return account_client

    async def fake_resolve_project_id(*, project_id: str | None) -> str:
        assert project_id == "project-1"
        return "resolved-project"

    monkeypatch.setattr(oauth2, "get_client", fake_get_client)
    monkeypatch.setattr(oauth2, "resolve_project_id", fake_resolve_project_id)
    monkeypatch.setattr(oauth2, "websocket_room_url", lambda room_name: room_name)
    monkeypatch.setattr(oauth2, "WebSocketClientProtocol", lambda url, token: None)
    monkeypatch.setattr(
        oauth2,
        "RoomClient",
        lambda protocol: _FakeRoomClient(secrets_client=secrets_client),
    )


@pytest.mark.asyncio
async def test_secret_exists_prints_true_and_passes_filters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_client = _FakeAccountClient()
    secrets_client = _FakeSecretsClient(exists_result=True)
    _patch_secret_exists_command(
        monkeypatch,
        account_client=account_client,
        secrets_client=secrets_client,
    )

    await oauth2.secret_exists(
        project_id="project-1",
        room="room-1",
        id="secret-1",
        delegated_to="participant-2",
        for_identity="agent-1",
    )

    assert account_client.connect_room_calls == [("resolved-project", "room-1")]
    assert secrets_client.exists_calls == [
        {
            "secret_id": "secret-1",
            "delegated_to": "participant-2",
            "for_identity": "agent-1",
        }
    ]
    assert capsys.readouterr().out == "true\n"
    assert account_client.closed is True


@pytest.mark.asyncio
async def test_secret_exists_prints_false_when_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_client = _FakeAccountClient()
    secrets_client = _FakeSecretsClient(exists_result=False)
    _patch_secret_exists_command(
        monkeypatch,
        account_client=account_client,
        secrets_client=secrets_client,
    )

    await oauth2.secret_exists(
        project_id="project-1",
        room="room-1",
        id="secret-1",
        delegated_to=None,
        for_identity=None,
    )

    assert account_client.connect_room_calls == [("resolved-project", "room-1")]
    assert secrets_client.exists_calls == [
        {
            "secret_id": "secret-1",
            "delegated_to": None,
            "for_identity": None,
        }
    ]
    assert capsys.readouterr().out == "false\n"
    assert account_client.closed is True
