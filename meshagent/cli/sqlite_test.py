from meshagent.cli.room import app as room_app
from meshagent.cli.sqlite import app
from meshagent.cli.testing import CliRunner
from rich.text import Text


def test_room_help_lists_sqlite_command() -> None:
    result = CliRunner().invoke(room_app, ["--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.output).plain
    assert "sqlite" in output


def test_sqlite_help_lists_supported_commands_without_dataset_only_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.output).plain
    assert "database" in output
    assert "table" in output
    assert "create" in output
    assert "search" in output
    assert "sql" in output
    assert "│ branch" not in output
    assert "│ version" not in output
    assert "│ index" not in output
    assert "│ optimize" not in output
    assert "│ stats" not in output
    assert "│ install" not in output
    assert "restore" in output
    assert "backup" in output
    assert "recover" in output


def test_sqlite_database_help_lists_database_subcommands() -> None:
    result = CliRunner().invoke(app, ["database", "--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.output).plain
    assert "list" in output
    assert "create" in output
    assert "drop" in output
    assert "inspect" in output


def test_sqlite_sql_help_lists_output_format_options_without_dataset_table_refs() -> (
    None
):
    result = CliRunner().invoke(app, ["sql", "--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.output).plain
    assert "--database" in output
    assert "--format" in output
    assert "excel" in output
    assert "--output" in output
    assert "--table" not in output
    assert "--tables-json" not in output
    assert "--branch" not in output
    assert "--version" not in output


def test_sqlite_import_help_omits_unsupported_merge_mode() -> None:
    result = CliRunner().invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    output = Text.from_ansi(result.output).plain
    assert "--database" in output
    assert "--format" in output
    assert "parquet" in output
    assert "excel" in output
    assert "--mode" in output
    assert "merge" not in output
    assert "--on" not in output
    assert "--branch" not in output


def test_sqlite_backup_restore_and_recovery_have_distinct_inputs() -> None:
    for command, required, absent in [
        ("backup", "--output", "--source"),
        ("restore", "--input", "--source"),
        ("recover", "--source", "--room"),
    ]:
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0
        output = Text.from_ansi(result.output).plain
        assert required in output
        assert absent not in output
    result = CliRunner().invoke(app, ["database", "--help"])
    assert "restore" not in Text.from_ansi(result.output).plain
