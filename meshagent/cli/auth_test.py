import pytest

from meshagent.cli import auth
from meshagent.cli.local_settings import SavedProfileRecord, StoredUserProfile


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


@pytest.mark.asyncio
async def test_login_passes_api_url_to_auth_async(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    async def _fake_login(*, api_url: str | None = None) -> None:
        captured["api_url"] = api_url

    async def _fake_get_active_project() -> str | None:
        return "project-123"

    monkeypatch.setattr(auth.auth_async, "login", _fake_login)
    monkeypatch.setattr(auth, "get_active_project", _fake_get_active_project)

    await auth.login(api_url="https://override.meshagent.test")

    assert captured == {"api_url": "https://override.meshagent.test"}


@pytest.mark.asyncio
async def test_switch_lists_saved_profiles(monkeypatch) -> None:
    output: list[str] = []

    monkeypatch.setattr(
        auth,
        "list_saved_profiles",
        lambda: [
            SavedProfileRecord(
                user_id="user-123",
                profile=StoredUserProfile(
                    id="user-123",
                    first_name="Jesse",
                    last_name="Ezell",
                    email="jesse@example.com",
                ),
                api_url="https://api.meshagent.test",
                is_active=True,
            )
        ],
    )
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.switch()

    assert output == [
        "* Jesse Ezell [user-123] @ https://api.meshagent.test",
    ]
