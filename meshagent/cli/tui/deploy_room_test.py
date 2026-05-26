import asyncio

import pytest
from textual.widgets import Log
import typer

from meshagent.cli.tui import deploy_room
from meshagent.cli.tui.deploy_room import (
    DeployDomainPromptApp,
    DeployDomainPromptResult,
    DeployProgressApp,
    DeployProgressHandle,
    DeployProgressResult,
    DeployTemplateVariablePrompt,
    DeployTemplateVariablesResult,
    DeployTemplateVariablesApp,
)


def test_deploy_domain_prompt_prefills_subdomain_from_room_name() -> None:
    app = DeployDomainPromptApp(
        service_name="web",
        port="8080",
        room_name="My Room! 2026",
        pages_domain=".meshagent.dev",
    )

    assert app._default_subdomain == "my-room-2026"
    assert app._pages_domain == "meshagent.dev"


def test_deploy_domain_prompt_accepts_subdomain_only() -> None:
    assert DeployDomainPromptApp._is_valid_subdomain("my-room") is True
    assert DeployDomainPromptApp._is_valid_subdomain("my.room") is False
    assert DeployDomainPromptApp._is_valid_subdomain("https://my-room") is False


def test_deploy_template_variables_records_required_value() -> None:
    app = DeployTemplateVariablesApp(
        variables=[
            DeployTemplateVariablePrompt(
                name="domain",
                title="Domain",
                description="Public route",
                default="demo.meshagent.app",
                optional=False,
            )
        ]
    )

    app._submit_current_value("custom.meshagent.app")

    assert app.result.status == "completed"
    assert app.result.values == {"domain": "custom.meshagent.app"}


def test_deploy_template_variables_rejects_empty_required_value() -> None:
    app = DeployTemplateVariablesApp(
        variables=[
            DeployTemplateVariablePrompt(
                name="domain",
                title="Domain",
                description="Public route",
                default="",
                optional=False,
            )
        ]
    )

    app._submit_current_value("")

    assert app.result.status == "canceled"
    assert app._index == 0


def test_deploy_progress_uses_selectable_log_widget() -> None:
    app = DeployProgressApp(operation=lambda handle: asyncio.sleep(0))

    log_widget = next(
        widget for widget in app.compose() if widget.id == "deploy-progress-log"
    )

    assert isinstance(log_widget, Log)
    assert log_widget.allow_select is True


@pytest.mark.asyncio
async def test_deploy_progress_handle_records_status_and_logs_it() -> None:
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    recorded: list[str] = []
    handle = DeployProgressHandle(queue, status_recorder=recorded.append)

    await handle.status("Waiting for service to go live: app (service-1)")

    assert recorded == ["Waiting for service to go live: app (service-1)"]
    assert await queue.get() == (
        "status",
        "Waiting for service to go live: app (service-1)",
    )
    assert await queue.get() == (
        "status_log",
        "Waiting for service to go live: app (service-1)",
    )


@pytest.mark.asyncio
async def test_deploy_progress_handle_records_transient_status_without_log() -> None:
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    recorded: list[str] = []
    handle = DeployProgressHandle(queue, status_recorder=recorded.append)

    await handle.transient_status("Connecting to room 'jesse'...")

    assert recorded == ["Connecting to room 'jesse'..."]
    assert await queue.get() == ("status", "Connecting to room 'jesse'...")
    assert queue.empty()


@pytest.mark.asyncio
async def test_deploy_progress_handle_delegates_prompts() -> None:
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    captured: dict[str, object] = {}

    async def _domain_prompt(
        *,
        service_name: str,
        port: str,
        room_name: str,
        pages_domain: str,
    ) -> DeployDomainPromptResult:
        captured["domain"] = (service_name, port, room_name, pages_domain)
        return DeployDomainPromptResult(
            status="completed",
            domain="demo.meshagent.dev",
        )

    async def _variables_prompt(
        *,
        variables: list[DeployTemplateVariablePrompt],
    ) -> DeployTemplateVariablesResult:
        captured["variables"] = variables
        return DeployTemplateVariablesResult(
            status="completed",
            values={"domain": "demo.meshagent.dev"},
        )

    variables = [
        DeployTemplateVariablePrompt(
            name="domain",
            title="Domain",
            description="Public route",
            default="demo.meshagent.dev",
            optional=False,
        )
    ]
    handle = DeployProgressHandle(
        queue,
        domain_prompt_handler=_domain_prompt,
        template_variables_prompt_handler=_variables_prompt,
    )

    domain_result = await handle.prompt_domain(
        service_name="web",
        port="8080",
        room_name="jesse",
        pages_domain="meshagent.dev",
    )
    variables_result = await handle.prompt_template_variables(variables=variables)

    assert domain_result.domain == "demo.meshagent.dev"
    assert variables_result.values == {"domain": "demo.meshagent.dev"}
    assert captured["domain"] == ("web", "8080", "jesse", "meshagent.dev")
    assert captured["variables"] == variables


@pytest.mark.asyncio
async def test_deploy_progress_error_uses_last_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_statuses: list[str] = []
    captured_logs: list[str] = []
    help_messages: list[str] = []
    exited = False

    async def _operation(handle: DeployProgressHandle) -> None:
        await handle.status("Service container exited before the service was live")
        raise typer.Exit(1)

    app = DeployProgressApp(operation=_operation)

    def nonlocal_set_exited() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "_set_status", captured_statuses.append)
    monkeypatch.setattr(app, "_append_log_line", captured_logs.append)
    monkeypatch.setattr(app, "_set_log_visible", lambda visible: None)
    monkeypatch.setattr(app, "_set_detail", lambda message: None)
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_set_help", help_messages.append)
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: nonlocal_set_exited())

    consumer_task = asyncio.create_task(app._consume_events())
    try:
        await app._run_operation()
    finally:
        consumer_task.cancel()
        await asyncio.gather(consumer_task, return_exceptions=True)

    assert app.result.status == "error"
    assert app.result.message == "Service container exited before the service was live"
    assert captured_statuses == [
        "Service container exited before the service was live",
        "Deploy failed: Service container exited before the service was live",
    ]
    assert captured_logs == ["Service container exited before the service was live"]
    assert app._prompt_mode == "finished"
    assert help_messages[-1] == (
        "Deploy failed. Press Enter, Esc, or Ctrl+C to exit. Ctrl+Y copies logs."
    )
    assert exited is False


@pytest.mark.asyncio
async def test_deploy_progress_error_stays_open_when_event_queue_does_not_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited = False

    async def _operation(handle: DeployProgressHandle) -> None:
        raise RuntimeError("Status=400, body=validation failed")

    app = DeployProgressApp(operation=_operation)

    def nonlocal_set_exited() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(
        deploy_room,
        "DEPLOY_PROGRESS_QUEUE_DRAIN_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(app, "_set_detail", lambda message: None)
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_set_help", lambda message: None)
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: nonlocal_set_exited())

    await asyncio.wait_for(app._run_operation(), timeout=0.2)

    assert app.result.status == "error"
    assert app.result.message == "Status=400, body=validation failed"
    assert app._prompt_mode == "finished"
    assert exited is False


@pytest.mark.asyncio
async def test_deploy_progress_ctrl_c_exits_finished_error_without_canceling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited = False
    app = DeployProgressApp(operation=lambda handle: asyncio.sleep(0))
    app._prompt_mode = "finished"
    app.result = DeployProgressResult(
        status="error",
        message="Status=400, body=validation failed",
    )

    def nonlocal_set_exited() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "_copy_selected_text_to_clipboard", lambda: False)
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: nonlocal_set_exited())

    await app.action_cancel_deploy()

    assert app.result.status == "error"
    assert app.result.message == "Status=400, body=validation failed"
    assert exited


@pytest.mark.asyncio
async def test_deploy_progress_ctrl_c_copies_selection_before_canceling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exited = False
    app = DeployProgressApp(operation=lambda handle: asyncio.sleep(0))

    def nonlocal_set_exited() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "_copy_selected_text_to_clipboard", lambda: True)
    monkeypatch.setattr(app, "exit", lambda *args, **kwargs: nonlocal_set_exited())

    await app.action_cancel_deploy()

    assert app.result.status == "canceled"
    assert exited is False


def test_deploy_progress_copy_deploy_logs_copies_buffered_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    help_messages: list[str] = []
    app = DeployProgressApp(operation=lambda handle: asyncio.sleep(0))
    app._log_lines = ["line one", "line two"]

    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "_set_help", help_messages.append)

    app.action_copy_deploy_logs()

    assert copied == ["line one\nline two"]
    assert help_messages == ["Copied deploy logs."]
