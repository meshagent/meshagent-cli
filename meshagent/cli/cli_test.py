import warnings
import sys
import types

import pytest

from meshagent.api import RoomException
from meshagent.agents import channel_process
from meshagent.cli import async_typer
from meshagent.cli import cli
from meshagent.cli.testing import CliRunner


def test_configure_warning_filters_suppresses_pydantic_serializer_warnings(
    monkeypatch,
) -> None:
    recorded_calls: list[dict[str, object]] = []

    def _fake_filterwarnings(action, message="", category=Warning, module="", **kwargs):
        recorded_calls.append(
            {
                "action": action,
                "message": message,
                "category": category,
                "module": module,
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr(warnings, "filterwarnings", _fake_filterwarnings)

    cli._configure_warning_filters()

    assert recorded_calls == [
        {
            "action": "ignore",
            "message": r"Pydantic serializer warnings:.*",
            "category": UserWarning,
            "module": r"pydantic\.main",
            "kwargs": {},
        }
    ]


def test_root_help_lists_create_and_doctor_but_hides_legacy_command_namespaces() -> (
    None
):
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "│ build" in result.output
    assert "│ deploy" in result.output
    assert "│ create" in result.output
    assert "│ doctor" in result.output
    assert "│ iam" in result.output
    assert "│ service-account" in result.output
    assert "│ init" not in result.output
    assert "│ api-key" not in result.output
    assert "│ launch" in result.output
    assert "│ room" in result.output
    assert "│ volumes" in result.output
    assert "│ call" not in result.output
    assert "│ package" not in result.output
    assert "│ multi" not in result.output
    assert "│ image" not in result.output


def test_root_registers_create_and_doctor_as_visible_commands() -> None:
    registrations = {
        registration.name: registration
        for registration in cli.app.registered_lazy_commands
    }

    assert registrations["doctor"].module == "meshagent.cli.doctor"
    assert registrations["doctor"].hidden is False
    assert registrations["create"].module == "meshagent.cli.create"
    assert registrations["create"].hidden is False
    assert registrations["iam"].module == "meshagent.cli.iam"
    assert registrations["iam"].hidden is False
    assert registrations["service-account"].module == "meshagent.cli.service_accounts"
    assert registrations["service-account"].hidden is False
    assert "init" not in registrations
    assert "api-key" not in registrations


def test_lazy_loader_accepts_typer_command_targets() -> None:
    from meshagent.cli.ask import ask_command

    assert isinstance(ask_command, async_typer.typer_click.Command)
    assert async_typer._coerce_to_click_command(ask_command) is ask_command


def test_lazy_loader_resolves_command_path_from_typer_group(monkeypatch) -> None:
    module = types.ModuleType("meshagent.cli._lazy_path_test")
    module.app = async_typer.AsyncTyper()

    @module.app.command("describe")
    def describe_command() -> None:
        pass

    @module.app.command("build", hidden=True)
    def build_command() -> None:
        pass

    monkeypatch.setitem(sys.modules, module.__name__, module)
    command = async_typer.LazyLoadedCommand(
        registration=async_typer.LazyCommandRegistration(
            name="build",
            module=module.__name__,
            command_path=("build",),
        )
    )

    assert command._load_command().name == "build"


def test_app_prints_room_exception_without_traceback(capsys) -> None:
    app = async_typer.AsyncTyper()

    @app.callback()
    def app_callback() -> None:
        pass

    @app.command("fail")
    def fail_command() -> None:
        raise RoomException("roomserver failed")

    with pytest.raises(SystemExit) as exc_info:
        app(["fail"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == "roomserver failed\n"
    assert captured.out == ""


def test_main_exits_with_status_returned_by_async_typer(monkeypatch) -> None:
    monkeypatch.setattr(channel_process, "dispatch_main", lambda **kwargs: False)
    monkeypatch.setattr(cli, "app", lambda: 23)
    monkeypatch.setattr(sys, "argv", ["meshagent", "build"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 23


def test_room_help_lists_agents_command() -> None:
    result = CliRunner().invoke(cli.app, ["room", "--help"])

    assert result.exit_code == 0
    assert "│ agents" in result.output


def test_volumes_help_lists_durable_lifecycle_commands() -> None:
    result = CliRunner().invoke(cli.app, ["volumes", "--help"])

    assert result.exit_code == 0
    assert "│ list" in result.output
    assert "│ create" in result.output
    assert "│ expand" in result.output
    assert "│ delete" in result.output
    assert "lease" not in result.output.lower()

    create_result = CliRunner().invoke(cli.app, ["volumes", "create", "--help"])
    assert create_result.exit_code == 0
    assert "--type" in create_result.output
    assert "--max-size" in create_result.output

    expand_result = CliRunner().invoke(cli.app, ["volumes", "expand", "--help"])
    assert expand_result.exit_code == 0
    assert "--max-size" in expand_result.output


def test_room_mounts_help_is_list_only() -> None:
    result = CliRunner().invoke(cli.app, ["room", "mounts", "--help"])

    assert result.exit_code == 0
    assert "│ list" in result.output
    assert "│ mount" not in result.output
    assert "│ unmount" not in result.output
    assert "lease" not in result.output.lower()


def test_room_agents_help_lists_call_command() -> None:
    result = CliRunner().invoke(cli.app, ["room", "agents", "--help"])

    assert result.exit_code == 0
    assert "│ call" in result.output


def test_agents_help_lists_use_command() -> None:
    result = CliRunner().invoke(cli.app, ["agents", "--help"])

    assert result.exit_code == 0
    assert "│ use" in result.output


def test_agent_help_lists_command_descriptions() -> None:
    result = CliRunner().invoke(cli.app, ["agent", "--help"])

    assert result.exit_code == 0
    assert "│ delete  Delete a managed agent from the project." in result.output
    assert "│ update  Update a managed agent configuration." in result.output
    assert "│ list    List managed agents in the project." in result.output
    assert "│ get     Get a managed agent configuration." in result.output


def test_room_agents_call_help_lists_call_targets() -> None:
    result = CliRunner().invoke(cli.app, ["room", "agents", "call", "--help"])

    assert result.exit_code == 0
    assert "│ agent" in result.output
    assert "│ tool" in result.output
    assert "│ toolkit" in result.output
    assert "│ schema" in result.output


def test_room_dataset_help_does_not_list_sql_exec() -> None:
    result = CliRunner().invoke(cli.app, ["room", "dataset", "--help"])

    assert result.exit_code == 0
    assert "│ sql " in result.output
    assert "sql-exec" not in result.output
