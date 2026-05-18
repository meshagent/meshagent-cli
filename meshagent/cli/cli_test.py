import warnings

from click.testing import CliRunner
import pytest

from meshagent.api import RoomException
from meshagent.cli import async_typer
from meshagent.cli import cli


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


def test_root_help_hides_legacy_command_namespaces() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["--help"])

    assert result.exit_code == 0
    assert "│ build" in result.output
    assert "│ deploy" in result.output
    assert "│ doctor" not in result.output
    assert "│ init" not in result.output
    assert "│ launch" in result.output
    assert "│ room" in result.output
    assert "│ call" not in result.output
    assert "│ package" not in result.output
    assert "│ multi" not in result.output
    assert "│ image" not in result.output


def test_root_registers_init_and_doctor_as_hidden_commands() -> None:
    registrations = {
        registration.name: registration
        for registration in cli.app.registered_lazy_commands
    }

    assert registrations["doctor"].module == "meshagent.cli.doctor"
    assert registrations["doctor"].hidden is True
    assert registrations["init"].module == "meshagent.cli.init"
    assert registrations["init"].hidden is True


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


def test_room_help_lists_agents_command() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["room", "--help"])

    assert result.exit_code == 0
    assert "│ agents" in result.output


def test_room_agents_help_lists_call_command() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["room", "agents", "--help"]
    )

    assert result.exit_code == 0
    assert "│ call" in result.output


def test_agents_help_lists_use_command() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["agents", "--help"])

    assert result.exit_code == 0
    assert "│ use" in result.output


def test_agent_help_lists_command_descriptions() -> None:
    result = CliRunner().invoke(async_typer.get_command(cli.app), ["agent", "--help"])

    assert result.exit_code == 0
    assert "│ delete  Delete a managed agent from the project." in result.output
    assert "│ update  Update a managed agent configuration." in result.output
    assert "│ list    List managed agents in the project." in result.output
    assert "│ get     Show a managed agent configuration." in result.output


def test_room_agents_call_help_lists_call_targets() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["room", "agents", "call", "--help"]
    )

    assert result.exit_code == 0
    assert "│ agent" in result.output
    assert "│ tool" in result.output
    assert "│ toolkit" in result.output
    assert "│ schema" in result.output


def test_room_dataset_help_does_not_list_sql_exec() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["room", "dataset", "--help"]
    )

    assert result.exit_code == 0
    assert "│ sql " in result.output
    assert "sql-exec" not in result.output
