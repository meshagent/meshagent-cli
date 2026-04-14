import asyncio

from meshagent.cli import root_commands
from meshagent.cli.tui.setup import SetupWizardResult


def test_setup_command_launches_ask_after_success(monkeypatch) -> None:
    launched: list[dict[str, object]] = []

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        assert kwargs["active_project_id"] is None
        return SetupWizardResult(status="completed", message="done")

    async def _fake_ask(*, project_id, message, model="gpt-5.4") -> None:
        launched.append(
            {
                "project_id": project_id,
                "message": message,
                "model": model,
            }
        )

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.ask.ask",
        _fake_ask,
    )

    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert launched == [
        {
            "project_id": None,
            "message": None,
            "model": "gpt-5.4",
        }
    ]


def test_setup_command_does_not_launch_ask_when_not_completed(monkeypatch) -> None:
    launched = False

    async def _fake_get_active_project() -> str | None:
        return None

    async def _fake_run_setup_wizard_tui(**kwargs) -> SetupWizardResult:
        del kwargs
        return SetupWizardResult(status="canceled", message="Setup canceled.")

    async def _fake_ask(*, project_id, message, model="gpt-5.4") -> None:
        del project_id, message, model
        nonlocal launched
        launched = True

    monkeypatch.setattr(
        "meshagent.cli.helper.get_active_project",
        _fake_get_active_project,
    )
    monkeypatch.setattr(
        "meshagent.cli.tui.setup.run_setup_wizard_tui",
        _fake_run_setup_wizard_tui,
    )
    monkeypatch.setattr(
        "meshagent.cli.ask.ask",
        _fake_ask,
    )

    monkeypatch.setattr(
        root_commands,
        "_run_async",
        lambda coro: asyncio.run(coro),
    )

    callback = root_commands.setup_command.callback
    assert callback is not None
    callback()

    assert launched is False
