import warnings

from click.testing import CliRunner

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
    assert "│ launch" in result.output
    assert "│ room" in result.output
    assert "│ call" not in result.output
    assert "│ package" not in result.output
    assert "│ multi" not in result.output
    assert "│ image" not in result.output


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


def test_room_agents_call_help_lists_call_targets() -> None:
    result = CliRunner().invoke(
        async_typer.get_command(cli.app), ["room", "agents", "call", "--help"]
    )

    assert result.exit_code == 0
    assert "│ agent" in result.output
    assert "│ tool" in result.output
    assert "│ toolkit" in result.output
    assert "│ schema" in result.output
