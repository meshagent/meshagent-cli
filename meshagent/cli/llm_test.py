from datetime import datetime, timezone
from types import SimpleNamespace

from meshagent.cli.testing import CliRunner
import pytest
from rich.panel import Panel
from textual.widgets import TextArea
import typer

from meshagent.cli import async_typer, cli
from meshagent.cli import llm as llm_module
from meshagent.llm_proxy.usage import RequestActivityEvent, UsageSnapshot


def test_llm_help_lists_proxy_and_logger_subcommands() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["llm", "--help"])

    assert result.exit_code == 0
    assert "proxy" in result.output
    assert "logger" in result.output
    assert "--project-id" not in result.output


def test_llm_logger_help_exposes_logger_commands() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["llm", "logger", "--help"]
    )

    assert result.exit_code == 0
    assert "create" in result.output
    assert "update" in result.output
    assert "delete" in result.output


def test_llm_proxy_help_exposes_proxy_options() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["llm", "proxy", "--help"]
    )

    assert result.exit_code == 0
    assert "Usage: meshagent llm proxy" in result.output
    assert "--project-id" in result.output
    assert "--token-from-env" in result.output


def test_render_usage_overview_returns_panel() -> None:
    panel = llm_module._render_usage_overview(
        snapshot=UsageSnapshot(
            total_requests=2,
            subtotal=1.25,
            surcharge=0.0625,
            total=1.3125,
            summaries=(),
            recent_events=(),
            recent_requests=(),
        ),
        proxy=SimpleNamespace(base_url="http://127.0.0.1:8000"),
        project_id="project-123",
        secure=True,
    )

    assert isinstance(panel, Panel)
    assert panel.title == "meshagent llm proxy"


def test_render_recent_activity_renders_status_and_request_total() -> None:
    table = llm_module._render_recent_activity(
        (
            RequestActivityEvent(
                provider="openai",
                transport="http",
                method="POST",
                path="/openai/v1/chat/completions",
                status=403,
                result="Invalid token.",
                request_id="req-123",
                total=None,
                timestamp=datetime.now(timezone.utc),
            ),
        )
    )

    assert table.title == "Recent Activity"


def test_build_proxy_status_messages_includes_setup_details() -> None:
    messages = llm_module._build_proxy_status_messages(
        proxy=SimpleNamespace(base_url="http://127.0.0.1:8000"),
        project_id="project-123",
        secure=True,
        upstream_token_message="Using MeshAgent token from $MESHAGENT_TOKEN for upstream requests.",
        generated=False,
        runtime_bearer="stored-token",
        explicit_bearer=None,
    )

    assert messages == (
        "Listening on http://127.0.0.1:8000 for project project-123 (secure).",
        "Using MeshAgent token from $MESHAGENT_TOKEN for upstream requests.",
        "Reusing the stored local bearer token from your meshagent config.",
    )


def test_configure_setup_text_area_populates_selectable_setup_text() -> None:
    setup_view = TextArea(
        "",
        read_only=True,
        show_cursor=True,
        soft_wrap=True,
        highlight_cursor_line=False,
    )

    llm_module._configure_setup_text_area(
        setup_view=setup_view,
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:8000/openai/v1",
            "OPENAI_API_KEY": "local-token",
        },
        status_messages=(
            "Listening on http://127.0.0.1:8000 for project project-123 (secure).",
        ),
    )

    assert setup_view.border_title == "Setup"
    assert setup_view.border_subtitle == "Drag to select | c copies all | q quits"
    assert "OPENAI_BASE_URL" in setup_view.text
    assert "OPENAI_API_KEY" in setup_view.text


def test_build_proxy_setup_text_returns_plain_shell_text() -> None:
    setup_text = llm_module._build_proxy_setup_text(
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:8000/openai/v1",
            "OPENAI_API_KEY": "local-token",
        },
        status_messages=(
            "Listening on http://127.0.0.1:8000 for project project-123 (secure).",
        ),
    )

    assert "[bold]" not in setup_text
    assert "Use these environment variables:" in setup_text
    assert "export OPENAI_BASE_URL=http://127.0.0.1:8000/openai/v1" in setup_text


def test_copy_text_to_clipboard_uses_os_clipboard_when_available(
    monkeypatch,
) -> None:
    calls: list[object] = []

    def _fake_run(command, *, input, text, check):  # type: ignore[no-untyped-def]
        calls.append((tuple(command), input, text, check))

    monkeypatch.setattr(llm_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        llm_module.shutil,
        "which",
        lambda command: "/usr/bin/pbcopy" if command == "pbcopy" else None,
    )
    monkeypatch.setattr(llm_module.subprocess, "run", _fake_run)

    message = llm_module._copy_text_to_clipboard(
        text="export OPENAI_API_KEY=token",
        terminal_copy=lambda text: calls.append(("terminal", text)),
    )

    assert calls == [(("pbcopy",), "export OPENAI_API_KEY=token", True, True)]
    assert message == "Copied setup block to the clipboard with pbcopy."


def test_copy_text_to_clipboard_falls_back_to_terminal_copy(monkeypatch) -> None:
    calls: list[object] = []

    monkeypatch.setattr(llm_module.sys, "platform", "linux")
    monkeypatch.setattr(llm_module.shutil, "which", lambda command: None)

    message = llm_module._copy_text_to_clipboard(
        text="export OPENAI_API_KEY=token",
        terminal_copy=lambda text: calls.append(text),
    )

    assert calls == ["export OPENAI_API_KEY=token"]
    assert message == "Sent the setup block to your terminal clipboard."


def test_suppress_textual_debug_features_strips_debug_flags(monkeypatch) -> None:
    monkeypatch.setenv("TEXTUAL", "debug,foo,devtools")

    llm_module._suppress_textual_debug_features()

    assert llm_module.os.environ["TEXTUAL"] == "foo"


@pytest.mark.asyncio
async def test_resolve_upstream_token_provider_prefers_meshagent_token_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESHAGENT_TOKEN", " env-token ")

    async def _unexpected_get_access_token() -> str:
        raise AssertionError("OAuth lookup should not run when MESHAGENT_TOKEN is set")

    monkeypatch.setattr(
        llm_module.auth_async,
        "get_access_token",
        _unexpected_get_access_token,
    )

    provider, message = await llm_module._resolve_upstream_token_provider(
        token_from_env=None
    )

    assert await provider() == "env-token"
    assert (
        message == "Using MeshAgent token from $MESHAGENT_TOKEN for upstream requests."
    )


@pytest.mark.asyncio
async def test_resolve_upstream_token_provider_uses_explicit_env_name(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CUSTOM_MESHAGENT_TOKEN", "custom-env-token")

    async def _unexpected_get_access_token() -> str:
        raise AssertionError("OAuth lookup should not run when a token env is selected")

    monkeypatch.setattr(
        llm_module.auth_async,
        "get_access_token",
        _unexpected_get_access_token,
    )

    provider, message = await llm_module._resolve_upstream_token_provider(
        token_from_env="CUSTOM_MESHAGENT_TOKEN"
    )

    assert await provider() == "custom-env-token"
    assert (
        message
        == "Using MeshAgent token from $CUSTOM_MESHAGENT_TOKEN for upstream requests."
    )


@pytest.mark.asyncio
async def test_resolve_upstream_token_provider_falls_back_to_oauth(monkeypatch) -> None:
    monkeypatch.delenv("MESHAGENT_TOKEN", raising=False)

    async def _fake_get_access_token() -> str:
        return "oauth-access-token"

    monkeypatch.setattr(
        llm_module.auth_async,
        "get_access_token",
        _fake_get_access_token,
    )

    provider, message = await llm_module._resolve_upstream_token_provider(
        token_from_env=None
    )

    assert await provider() == "oauth-access-token"
    assert (
        message
        == "Using OAuth access token from your meshagent auth session for upstream requests."
    )


@pytest.mark.asyncio
async def test_resolve_upstream_token_provider_requires_explicit_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CUSTOM_MESHAGENT_TOKEN", raising=False)

    with pytest.raises(typer.Exit) as exc_info:
        await llm_module._resolve_upstream_token_provider(
            token_from_env="CUSTOM_MESHAGENT_TOKEN"
        )

    assert exc_info.value.exit_code == 1
