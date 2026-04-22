from click.testing import CliRunner

from meshagent.cli import cli as root_cli
from meshagent.cli.async_typer import get_command


def test_claude_code_command_launches_with_forwarded_args(monkeypatch) -> None:
    launches: list[dict[str, object]] = []

    monkeypatch.setattr(
        "meshagent.cli.claude_code.launch_claude_code",
        lambda **kwargs: launches.append(kwargs) or 7,
    )

    result = CliRunner().invoke(
        get_command(root_cli.app),
        [
            "claude-code",
            "--project-id",
            "project-123",
            "--api-url",
            "https://api.meshagent.test",
            "--",
            "-p",
            "say hi",
        ],
    )

    assert result.exit_code == 7
    assert launches == [
        {
            "project_id": "project-123",
            "api_url": "https://api.meshagent.test",
            "extra_args": ["-p", "say hi"],
        }
    ]
