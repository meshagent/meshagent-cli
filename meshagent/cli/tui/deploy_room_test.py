import asyncio

import pytest
import typer

from meshagent.cli.tui.deploy_room import (
    DeployDomainPromptApp,
    DeployProgressApp,
    DeployProgressHandle,
    DeployTemplateVariablePrompt,
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
async def test_deploy_progress_error_uses_last_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_statuses: list[str] = []
    captured_logs: list[str] = []

    async def _operation(handle: DeployProgressHandle) -> None:
        await handle.status("Service container exited before the service was live")
        raise typer.Exit(1)

    app = DeployProgressApp(operation=_operation)
    monkeypatch.setattr(app, "_set_status", captured_statuses.append)
    monkeypatch.setattr(app, "_append_log_line", captured_logs.append)
    monkeypatch.setattr(app, "_set_log_visible", lambda visible: None)
    monkeypatch.setattr(app, "_set_detail", lambda message: None)
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_set_help", lambda message: None)

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
