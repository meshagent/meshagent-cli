from click.testing import CliRunner
import pytest

from meshagent.cli import async_typer
from meshagent.cli import mailbot
from meshagent.cli import multi
from meshagent.cli import task_runner
from meshagent.cli import worker


@pytest.mark.parametrize(
    ("module", "subcommand"),
    [
        pytest.param(task_runner, "join", id="task-runner"),
        pytest.param(mailbot, "join", id="mailbot"),
        pytest.param(worker, "join", id="worker"),
    ],
)
def test_join_help_retains_storage_mount_options(module, subcommand: str) -> None:
    command = async_typer.get_command(module.app).get_command(None, subcommand)

    assert command is not None
    options = {
        option
        for param in command.params
        for option in [
            *getattr(param, "opts", []),
            *getattr(param, "secondary_opts", []),
        ]
    }
    assert "--storage-tool-local-path" in options
    assert "--storage-tool-room-path" in options


@pytest.mark.parametrize("subcommand", ["chatbot", "mailbot", "worker"])
def test_multi_join_preserves_deprecated_aliases(subcommand: str) -> None:
    result = CliRunner().invoke(
        async_typer.get_command(multi.cli_join),
        [subcommand, "--require-storage", "--help"],
    )

    assert result.exit_code == 0
    assert "--storage" in result.output
