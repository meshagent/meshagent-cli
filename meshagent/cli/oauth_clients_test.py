import pytest
import typer
from meshagent.cli.oauth_clients import Theme, app, branding_metadata
from meshagent.cli.testing import CliRunner


def test_branding_update_preserves_other_metadata():
    assert branding_metadata(
        {"name": "App", "custom": "keep", "logo_url": "https://old.example/logo"},
        name=None,
        logo_url=None,
        theme=Theme.dark,
    ) == {
        "name": "App",
        "custom": "keep",
        "logo_url": "https://old.example/logo",
        "theme": "dark",
    }


def test_clear_logo_restores_default_without_removing_name():
    assert branding_metadata(
        {"name": "App", "logo_url": "https://example.com/logo"},
        name=None,
        logo_url=None,
        theme=None,
        clear_logo=True,
    ) == {"name": "App"}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "/logo.svg", "not-a-url"])
def test_invalid_logo_rejected(url):
    with pytest.raises(typer.BadParameter):
        branding_metadata({}, name="App", logo_url=url, theme=Theme.auto)


def test_oauth_branding_options_are_available():
    result = CliRunner().invoke(app, ["update", "--help"])
    assert result.exit_code == 0, result.output
    from rich.text import Text

    output = Text.from_ansi(result.output).plain
    assert "--logo-url" in output
    assert "--clear-logo" in output
    assert "--theme" in output


@pytest.mark.asyncio
async def test_update_command_sends_branding_and_preserves_existing_metadata(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from meshagent.api.client import OAuthClient
    from meshagent.cli import oauth_clients

    api = AsyncMock()
    api.__aenter__.return_value = api
    api.get_oauth_client.return_value = OAuthClient(
        client_id="client-1",
        project_id="project-1",
        grant_types=["authorization_code"],
        response_types=["code"],
        redirect_uris=["https://example.com/callback"],
        scope="openid",
        metadata={"name": "Existing", "custom": "preserved"},
    )
    api.update_oauth_client.return_value = {"ok": True}
    monkeypatch.setattr(oauth_clients, "get_client", AsyncMock(return_value=api))
    monkeypatch.setattr(
        oauth_clients, "resolve_project_id", AsyncMock(return_value="project-1")
    )
    monkeypatch.setattr(oauth_clients, "print_json", lambda **kwargs: None)
    await oauth_clients.update_client(
        client_id="client-1",
        project_id="project-1",
        logo_url="https://example.com/logo.svg",
        theme=Theme.dark,
    )
    api.update_oauth_client.assert_awaited_once_with(
        project_id="project-1",
        client_id="client-1",
        metadata={
            "name": "Existing",
            "custom": "preserved",
            "logo_url": "https://example.com/logo.svg",
            "theme": "dark",
        },
    )
