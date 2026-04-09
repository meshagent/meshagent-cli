from click.testing import CliRunner

from meshagent.cli import async_typer
from meshagent.cli.database import app


def test_database_help_groups_branch_commands() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["--help"])

    assert result.exit_code == 0
    assert "branch" in result.output
    assert "branch-create" not in result.output
    assert "branch-delete" not in result.output


def test_database_branch_help_lists_branch_subcommands() -> None:
    result = CliRunner().invoke(async_typer.get_command(app), ["branch", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output
    assert "create" in result.output
    assert "delete" in result.output
