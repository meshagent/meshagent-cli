from __future__ import annotations

import asyncio
import os
import shutil
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import typer
from rich import print
from rich.panel import Panel
from rich.table import Table

from meshagent.api.helpers import meshagent_base_url
from meshagent.cli import async_typer, auth_async
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.helper import (
    get_llm_proxy_bearer_token,
    resolve_project_id,
    set_llm_proxy_bearer_token,
)
from meshagent.llm_proxy.local_proxy import (
    DEFAULT_PROXY_HOST,
    DEFAULT_PROXY_PORT,
    LocalLLMProxyServer,
    UpstreamBearerTokenProvider,
)
from meshagent.llm_proxy.usage import RequestActivityEvent, UsageSnapshot, UsageSummary

if TYPE_CHECKING:
    from textual.widgets import TextArea


app = async_typer.AsyncTyper(help="Local LLM proxy utilities")
_DEFAULT_TOKEN_ENV = "MESHAGENT_TOKEN"


@app.callback()
def _callback() -> None:
    return None


def _format_dollars(value: float) -> str:
    return f"${value:.6f}"


def _format_quantity(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _format_token_summary(tokens: dict[str, float]) -> str:
    if not tokens:
        return "-"
    return ", ".join(
        f"{key}={_format_quantity(value)}" for key, value in sorted(tokens.items())
    )


def _render_model_table(summaries: tuple[UsageSummary, ...]) -> Table:
    table = Table(title="Usage By Model")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Requests", justify="right")
    table.add_column("Subtotal", justify="right")
    table.add_column("Surcharge", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Tokens")

    if not summaries:
        table.add_row(
            "-",
            "-",
            "0",
            _format_dollars(0),
            _format_dollars(0),
            _format_dollars(0),
            "-",
        )
        return table

    for summary in summaries:
        table.add_row(
            summary.provider,
            summary.model,
            str(summary.request_count),
            _format_dollars(summary.subtotal),
            _format_dollars(summary.surcharge),
            _format_dollars(summary.total),
            _format_token_summary(summary.tokens),
        )
    return table


def _render_recent_activity(events: tuple[RequestActivityEvent, ...]) -> Table:
    table = Table(title="Recent Activity")
    table.add_column("Time")
    table.add_column("Provider")
    table.add_column("Request")
    table.add_column("Result")
    table.add_column("Total", justify="right")

    if not events:
        table.add_row("-", "-", "-", "-", "-")
        return table

    for event in events[:10]:
        request_method = "WS" if event.transport == "websocket" else event.method
        result = event.result
        if event.status is not None:
            result = f"{event.status} {result}"
        table.add_row(
            event.timestamp.astimezone().strftime("%H:%M:%S"),
            event.provider,
            f"{request_method} {event.path}",
            result,
            _format_dollars(event.total) if event.total is not None else "-",
        )
    return table


def _render_usage_overview(
    *,
    snapshot: UsageSnapshot,
    proxy: LocalLLMProxyServer,
    project_id: str,
    secure: bool,
) -> Panel:
    summary = Table.grid(padding=(0, 2))
    summary.add_column()
    summary.add_column()
    summary.add_row("Project", project_id)
    summary.add_row("Base URL", proxy.base_url)
    summary.add_row("Auth", "secure" if secure else "insecure")
    summary.add_row("Requests", str(snapshot.total_requests))
    summary.add_row("Subtotal", _format_dollars(snapshot.subtotal))
    summary.add_row("Surcharge", _format_dollars(snapshot.surcharge))
    summary.add_row("Total", _format_dollars(snapshot.total))

    return Panel(summary, title="meshagent llm proxy", border_style="cyan")


def _build_proxy_status_messages(
    *,
    proxy: LocalLLMProxyServer,
    project_id: str,
    secure: bool,
    upstream_token_message: str,
    generated: bool,
    runtime_bearer: str | None,
    explicit_bearer: str | None,
) -> tuple[str, ...]:
    messages = [
        f"Listening on {proxy.base_url} for project {project_id} "
        f"({'secure' if secure else 'insecure'}).",
        upstream_token_message,
    ]

    if generated:
        messages.append(
            "Generated and stored a new local bearer token in your meshagent config."
        )
    elif runtime_bearer is not None and explicit_bearer is None:
        messages.append(
            "Reusing the stored local bearer token from your meshagent config."
        )
    elif runtime_bearer is not None:
        messages.append(
            "Using the explicit local bearer token provided on the command line."
        )

    return tuple(messages)


def _build_proxy_setup_text(
    *,
    env: dict[str, str],
    status_messages: tuple[str, ...],
) -> str:
    return "\n".join(_build_proxy_setup_lines(env=env, status_messages=status_messages))


def _build_proxy_setup_lines(
    *,
    env: dict[str, str],
    status_messages: tuple[str, ...],
) -> tuple[str, ...]:
    lines = [*status_messages, "", "Use these environment variables:"]
    lines.extend(f"export {key}={shlex.quote(value)}" for key, value in env.items())
    return tuple(lines)


def _copy_text_to_clipboard(
    *,
    text: str,
    terminal_copy: Callable[[str], None],
) -> str:
    clipboard_commands: tuple[tuple[str, ...], ...] = ()
    if sys.platform == "darwin":
        clipboard_commands = (("pbcopy",),)
    elif sys.platform == "win32":
        clipboard_commands = (("clip",),)
    else:
        clipboard_commands = (
            ("wl-copy",),
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
        )

    for command in clipboard_commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                check=True,
            )
            return f"Copied setup block to the clipboard with {command[0]}."
        except (OSError, subprocess.SubprocessError):
            continue

    terminal_copy(text)
    return "Sent the setup block to your terminal clipboard."


def _configure_setup_text_area(
    *,
    setup_view: TextArea,
    env: dict[str, str],
    status_messages: tuple[str, ...],
) -> None:
    setup_view.border_title = "Setup"
    setup_view.border_subtitle = "Drag to select | c copies all | q quits"
    setup_view.text = _build_proxy_setup_text(
        env=env,
        status_messages=status_messages,
    )


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None:
        return

    parsed_features = [
        value.strip() for value in raw_features.split(",") if value.strip() != ""
    ]
    if len(parsed_features) == 0:
        return

    filtered_features = [
        value for value in parsed_features if value.lower() not in ("debug", "devtools")
    ]
    if len(filtered_features) == len(parsed_features):
        return

    if len(filtered_features) == 0:
        os.environ.pop("TEXTUAL", None)
        return

    os.environ["TEXTUAL"] = ",".join(filtered_features)


async def _wait_forever() -> None:
    while True:
        await asyncio.sleep(3600)


async def _run_tui(
    *,
    proxy: LocalLLMProxyServer,
    project_id: str,
    secure: bool,
    env: dict[str, str],
    status_messages: tuple[str, ...],
) -> None:
    _suppress_textual_debug_features()

    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal
        from textual.widgets import Static, TextArea
    except ImportError as exc:
        print(
            "[bold red]Textual is required for the llm proxy UI. Install meshagent-cli dependencies and retry.[/bold red]"
        )
        raise typer.Exit(1) from exc

    class _LLMProxyTextualApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            background: #050911;
            padding: 1;
        }
        #top {
            layout: horizontal;
            height: auto;
            margin: 0 0 1 0;
        }
        #setup {
            width: 2fr;
            height: 12;
            margin: 0 1 0 0;
            border: tall green;
        }
        #overview {
            width: 1fr;
        }
        #body {
            layout: horizontal;
            height: 1fr;
        }
        #models {
            width: 3fr;
            height: 1fr;
            margin: 0 1 0 0;
        }
        #recent {
            width: 2fr;
            height: 1fr;
        }
        """

        BINDINGS = [
            Binding("c", "copy_setup", "Copy Setup", priority=True),
            Binding("q", "quit", "Quit", priority=True),
            Binding("ctrl+q", "quit", "Quit", priority=True),
        ]

        def __init__(
            self,
            *,
            proxy: LocalLLMProxyServer,
            project_id: str,
            secure: bool,
            env: dict[str, str],
            status_messages: tuple[str, ...],
        ) -> None:
            super().__init__()
            self._proxy = proxy
            self._project_id = project_id
            self._secure = secure
            self._env = env
            self._status_messages = status_messages
            self._setup_view: TextArea | None = None
            self._overview_view: Static | None = None
            self._models_view: Static | None = None
            self._recent_view: Static | None = None
            self._refresh_task: asyncio.Task[None] | None = None

        def compose(self) -> ComposeResult:
            with Horizontal(id="top"):
                yield TextArea(
                    "",
                    id="setup",
                    read_only=True,
                    show_cursor=True,
                    soft_wrap=True,
                    highlight_cursor_line=False,
                )
                yield Static("", id="overview")
            with Horizontal(id="body"):
                yield Static("", id="models")
                yield Static("", id="recent")

        async def on_mount(self) -> None:
            self._setup_view = self.query_one("#setup", TextArea)
            self._overview_view = self.query_one("#overview", Static)
            self._models_view = self.query_one("#models", Static)
            self._recent_view = self.query_one("#recent", Static)
            _configure_setup_text_area(
                setup_view=self._setup_view,
                env=self._env,
                status_messages=self._status_messages,
            )
            self._setup_view.focus()
            await self._refresh_usage()
            self._refresh_task = asyncio.create_task(self._poll_usage())

        async def on_unmount(self) -> None:
            if self._refresh_task is not None and not self._refresh_task.done():
                self._refresh_task.cancel()
                await asyncio.gather(self._refresh_task, return_exceptions=True)
                self._refresh_task = None

        async def _poll_usage(self) -> None:
            while True:
                await asyncio.sleep(0.25)
                await self._refresh_usage()

        async def _refresh_usage(self) -> None:
            if (
                self._overview_view is None
                or self._models_view is None
                or self._recent_view is None
            ):
                return

            snapshot = await self._proxy.usage_collector.snapshot()
            self._overview_view.update(
                _render_usage_overview(
                    snapshot=snapshot,
                    proxy=self._proxy,
                    project_id=self._project_id,
                    secure=self._secure,
                )
            )
            self._models_view.update(_render_model_table(snapshot.summaries))
            self._recent_view.update(_render_recent_activity(snapshot.recent_requests))

        def action_copy_setup(self) -> None:
            message = _copy_text_to_clipboard(
                text=_build_proxy_setup_text(
                    env=self._env,
                    status_messages=self._status_messages,
                ),
                terminal_copy=self.copy_to_clipboard,
            )
            self.notify(message, title="Setup")

    await _LLMProxyTextualApp(
        proxy=proxy,
        project_id=project_id,
        secure=secure,
        env=env,
        status_messages=status_messages,
    ).run_async()


def _print_env(env: dict[str, str]) -> None:
    print()
    print("Use these environment variables:")
    for key, value in env.items():
        print(f"export {key}={shlex.quote(value)}")
    print()


async def _resolve_runtime_bearer(
    *,
    insecure: bool,
    bearer: str | None,
) -> tuple[str | None, bool]:
    if insecure:
        return None, False

    if isinstance(bearer, str):
        normalized = bearer.strip()
        if normalized == "":
            raise typer.BadParameter("--bearer cannot be empty")
        return normalized, False

    stored_bearer = await get_llm_proxy_bearer_token()
    if isinstance(stored_bearer, str) and stored_bearer.strip() != "":
        return stored_bearer.strip(), False

    generated_bearer = secrets.token_urlsafe(32)
    await set_llm_proxy_bearer_token(generated_bearer)
    return generated_bearer, True


def _normalize_token(token: str | None) -> str | None:
    if token is None:
        return None
    normalized = token.strip()
    return normalized or None


def _build_static_upstream_token_provider(token: str) -> UpstreamBearerTokenProvider:
    async def _provider() -> str:
        return token

    return _provider


async def _resolve_upstream_token_provider(
    *,
    token_from_env: str | None,
) -> tuple[UpstreamBearerTokenProvider, str]:
    explicit_env_name = None
    if token_from_env is not None:
        explicit_env_name = token_from_env.strip()
        if explicit_env_name == "":
            raise typer.BadParameter("--token-from-env cannot be empty")

    token_env_name = explicit_env_name or _DEFAULT_TOKEN_ENV
    env_token = _normalize_token(os.environ.get(token_env_name))
    if env_token is not None:
        return (
            _build_static_upstream_token_provider(env_token),
            f"Using MeshAgent token from ${token_env_name} for upstream requests.",
        )

    if explicit_env_name is not None:
        print(
            f"[red]{token_env_name} environment variable is not set or is empty.[/red]"
        )
        raise typer.Exit(code=1)

    access_token = await auth_async.get_access_token()
    normalized_access_token = _normalize_token(access_token)
    if normalized_access_token is None:
        print(
            "[red]No MeshAgent token or OAuth access token available. "
            "Set MESHAGENT_TOKEN or run `meshagent auth login` first.[/red]"
        )
        raise typer.Exit(code=1)

    return (
        auth_async.get_access_token,
        "Using OAuth access token from your meshagent auth session for upstream requests.",
    )


@app.async_command(
    "proxy",
    help="Expose a local MeshAgent-authenticated LLM proxy.",
)
async def proxy(
    *,
    project_id: ProjectIdOption,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Local host to bind the proxy to.",
        ),
    ] = DEFAULT_PROXY_HOST,
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="Local port to bind the proxy to.",
        ),
    ] = DEFAULT_PROXY_PORT,
    bearer: Annotated[
        str | None,
        typer.Option(
            "--bearer",
            help="Explicit local bearer token. If omitted, the stored token is reused or generated on first run.",
        ),
    ] = None,
    token_from_env: Annotated[
        str | None,
        typer.Option(
            "--token-from-env",
            help="Name of environment variable containing a MeshAgent token to forward upstream.",
        ),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Disable local bearer-token enforcement.",
        ),
    ] = False,
    tui: Annotated[
        bool,
        typer.Option(
            "--tui/--no-tui",
            help="Show the live usage dashboard when attached to a TTY.",
        ),
    ] = True,
):
    if insecure and bearer is not None:
        raise typer.BadParameter("--insecure cannot be combined with --bearer")

    if port < 0 or port > 65535:
        raise typer.BadParameter("--port must be between 0 and 65535")

    resolved_project_id = await resolve_project_id(project_id=project_id)
    (
        upstream_token_provider,
        upstream_token_message,
    ) = await _resolve_upstream_token_provider(token_from_env=token_from_env)

    runtime_bearer, generated = await _resolve_runtime_bearer(
        insecure=insecure,
        bearer=bearer,
    )
    proxy_server = LocalLLMProxyServer(
        api_base_url=meshagent_base_url().rstrip("/"),
        project_id=resolved_project_id,
        upstream_bearer_token_provider=upstream_token_provider,
        host=host,
        port=port,
        bearer_token=runtime_bearer,
        insecure=insecure,
    )

    try:
        await proxy_server.start()

        proxy_env = proxy_server.env()
        status_messages = _build_proxy_status_messages(
            proxy=proxy_server,
            project_id=resolved_project_id,
            secure=not insecure,
            upstream_token_message=upstream_token_message,
            generated=generated,
            runtime_bearer=runtime_bearer,
            explicit_bearer=bearer,
        )
        for message in status_messages:
            print(message)

        _print_env(proxy_env)
        show_tui = tui and sys.stdin.isatty() and sys.stdout.isatty()
        if show_tui:
            print(
                "Press q or Ctrl+Q to stop. Drag to select the setup block or press c to copy it."
            )
        else:
            print("Press Ctrl+C to stop.")
        if show_tui:
            await _run_tui(
                proxy=proxy_server,
                project_id=resolved_project_id,
                secure=not insecure,
                env=proxy_env,
                status_messages=status_messages,
            )
        else:
            await _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        await proxy_server.close()

    final_snapshot = await proxy_server.usage_collector.snapshot()
    print(
        f"Final totals: {final_snapshot.total_requests} requests, "
        f"{_format_dollars(final_snapshot.subtotal)} subtotal, "
        f"{_format_dollars(final_snapshot.surcharge)} surcharge, "
        f"{_format_dollars(final_snapshot.total)} total."
    )
