import pytest

from meshagent.cli import auth


class _FakeClient:
    def __init__(self, *, profile: dict[str, object]) -> None:
        self._profile = profile
        self.closed = False

    async def get_user_profile(self, user_id: str) -> dict[str, object]:
        assert user_id == "me"
        return self._profile

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_whoami_uses_user_profile(monkeypatch) -> None:
    output: list[str] = []
    client = _FakeClient(
        profile={
            "id": "user-123",
            "first_name": "Jesse",
            "last_name": "Ezell",
            "email": "jesse@example.com",
        }
    )

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    monkeypatch.setattr(auth.auth_async, "get_access_token", _fake_get_access_token)
    monkeypatch.setattr(
        auth,
        "CustomMeshagentClient",
        lambda *, base_url, token: client,
    )
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.whoami()

    assert output == ["Jesse Ezell <jesse@example.com>"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_whoami_prints_not_logged_in_without_identity(monkeypatch) -> None:
    output: list[str] = []

    async def _fake_get_access_token() -> str | None:
        return None

    monkeypatch.setattr(auth.auth_async, "get_access_token", _fake_get_access_token)
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.whoami()

    assert output == ["Not logged in"]
