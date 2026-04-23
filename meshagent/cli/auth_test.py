from types import SimpleNamespace

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


class _FakeTTY:
    def __init__(self, *, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


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
async def test_token_prints_access_token(monkeypatch) -> None:
    output: list[str] = []

    async def _fake_get_access_token() -> str | None:
        return "oauth-token"

    monkeypatch.setattr(auth.auth_async, "get_access_token", _fake_get_access_token)
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.token()

    assert output == ["oauth-token"]


@pytest.mark.asyncio
async def test_token_exits_when_not_logged_in(monkeypatch) -> None:
    output: list[tuple[str, bool]] = []

    async def _fake_get_access_token() -> str | None:
        return None

    def _fake_echo(message: str, *, err: bool = False) -> None:
        output.append((message, err))

    monkeypatch.setattr(auth.auth_async, "get_access_token", _fake_get_access_token)
    monkeypatch.setattr(auth.typer, "echo", _fake_echo)

    with pytest.raises(auth.typer.Exit) as exc_info:
        await auth.token()

    assert exc_info.value.exit_code == 1
    assert output == [("Not logged in", True)]


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
    monkeypatch.setattr(auth.sys, "stdin", _FakeTTY(is_tty=False))
    monkeypatch.setattr(auth.sys, "stdout", _FakeTTY(is_tty=False))
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.switch()

    assert output == [
        "* Jesse Ezell [user-123] @ https://api.meshagent.test",
    ]


def test_should_launch_switch_tui_only_when_selector_missing_in_tty() -> None:
    assert (
        auth._should_launch_switch_tui(
            profile=None,
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is True
    )
    assert (
        auth._should_launch_switch_tui(
            profile="user-123",
            stdin_is_tty=True,
            stdout_is_tty=True,
        )
        is False
    )
    assert (
        auth._should_launch_switch_tui(
            profile=None,
            stdin_is_tty=False,
            stdout_is_tty=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_switch_launches_tui_when_no_selector_in_tty(monkeypatch) -> None:
    output: list[str] = []
    saved_profiles = [
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
        ),
        SavedProfileRecord(
            user_id="user-456",
            profile=StoredUserProfile(
                id="user-456",
                first_name="Taylor",
                last_name="Swift",
                email="taylor@example.com",
            ),
            api_url="https://api.meshagent.test",
            is_active=False,
        ),
    ]
    selected_selectors: list[str] = []

    async def _fake_run_auth_switch_tui(*, saved_profiles):
        return SimpleNamespace(
            status="completed",
            message=None,
            selected_profile=saved_profiles[1],
        )

    monkeypatch.setattr(auth, "list_saved_profiles", lambda: saved_profiles)
    monkeypatch.setattr(auth.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(auth.sys, "stdout", _FakeTTY(is_tty=True))
    monkeypatch.setattr(
        auth,
        "_run_auth_switch_tui",
        _fake_run_auth_switch_tui,
    )
    monkeypatch.setattr(
        auth,
        "switch_active_profile",
        lambda selector: (
            selected_selectors.append(selector)
            or SavedProfileRecord(
                user_id="user-456",
                profile=StoredUserProfile(
                    id="user-456",
                    first_name="Taylor",
                    last_name="Swift",
                    email="taylor@example.com",
                ),
                api_url="https://api.meshagent.test",
                is_active=True,
            )
        ),
    )
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.switch()

    assert selected_selectors == ["user-456"]
    assert output == [
        "Active profile: * Taylor Swift [user-456] @ https://api.meshagent.test",
    ]


@pytest.mark.asyncio
async def test_switch_prints_cancel_message_when_tui_is_canceled(monkeypatch) -> None:
    output: list[str] = []
    switch_attempted = False

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
    monkeypatch.setattr(auth.sys, "stdin", _FakeTTY(is_tty=True))
    monkeypatch.setattr(auth.sys, "stdout", _FakeTTY(is_tty=True))

    async def _fake_run_auth_switch_tui(*, saved_profiles):
        del saved_profiles
        return SimpleNamespace(
            status="canceled",
            message="Profile switch canceled.",
            selected_profile=None,
        )

    monkeypatch.setattr(
        auth,
        "_run_auth_switch_tui",
        _fake_run_auth_switch_tui,
    )

    def _unexpected_switch(selector: str):
        del selector
        nonlocal switch_attempted
        switch_attempted = True
        raise AssertionError("switch_active_profile should not be called")

    monkeypatch.setattr(auth, "switch_active_profile", _unexpected_switch)
    monkeypatch.setattr(auth.typer, "echo", output.append)

    await auth.switch()

    assert switch_attempted is False
    assert output == ["Profile switch canceled."]
