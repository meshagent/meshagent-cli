from meshagent.cli.testing import CliRunner
from meshagent.cli.room import app as room_app
from meshagent.cli.sqlite import app


def test_room_help_lists_sqlite_command() -> None:
    result = CliRunner().invoke(room_app, ["--help"])

    assert result.exit_code == 0
    assert "sqlite" in result.output


def test_sqlite_help_lists_supported_commands_without_dataset_only_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "database" in result.output
    assert "table" in result.output
    assert "create" in result.output
    assert "search" in result.output
    assert "sql" in result.output
    assert "│ branch" not in result.output
    assert "│ version" not in result.output
    assert "│ index" not in result.output
    assert "│ optimize" not in result.output
    assert "│ stats" not in result.output
    assert "│ install" not in result.output
    assert "│ restore" not in result.output


def test_sqlite_database_help_lists_database_subcommands() -> None:
    result = CliRunner().invoke(app, ["database", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "drop" in result.output
    assert "inspect" in result.output


def test_sqlite_sql_help_lists_output_format_options_without_dataset_table_refs() -> (
    None
):
    result = CliRunner().invoke(app, ["sql", "--help"])

    assert result.exit_code == 0
    assert "--database" in result.output
    assert "--format" in result.output
    assert "excel" in result.output
    assert "--output" in result.output
    assert "--table" not in result.output
    assert "--tables-json" not in result.output
    assert "--branch" not in result.output
    assert "--version" not in result.output


def test_sqlite_import_help_omits_unsupported_merge_mode() -> None:
    result = CliRunner().invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    assert "--database" in result.output
    assert "--format" in result.output
    assert "parquet" in result.output
    assert "excel" in result.output
    assert "--mode" in result.output
    assert "merge" not in result.output
    assert "--on" not in result.output
    assert "--branch" not in result.output
