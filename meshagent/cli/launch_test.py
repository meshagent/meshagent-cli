from meshagent.cli.testing import CliRunner

from meshagent.cli import cli as root_cli
from meshagent.cli.async_typer import get_command


def test_launch_codex_command_launches_with_forwarded_args(monkeypatch) -> None:
    launches: list[dict[str, object]] = []

    monkeypatch.setattr(
        "meshagent.cli.launch.launch_codex",
        lambda **kwargs: launches.append(kwargs) or 7,
    )
    monkeypatch.setattr(
        "meshagent.cli.launch.resolve_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )

    result = CliRunner().invoke(
        get_command(root_cli.app),
        [
            "launch",
            "codex",
            "--project-id",
            "project-123",
            "--api-url",
            "https://api.meshagent.test",
            "--",
            "--search",
            "fix auth flow",
        ],
    )

    assert result.exit_code == 7
    assert launches == [
        {
            "project_id": "project-123",
            "api_url": "https://api.meshagent.test",
            "extra_args": ["--search", "fix auth flow"],
            "meshagent_executable": "/tmp/current/bin/meshagent",
        }
    ]


def test_launch_claude_command_launches_with_forwarded_args(monkeypatch) -> None:
    launches: list[dict[str, object]] = []

    monkeypatch.setattr(
        "meshagent.cli.launch.launch_claude",
        lambda **kwargs: launches.append(kwargs) or 5,
    )
    monkeypatch.setattr(
        "meshagent.cli.launch.resolve_current_meshagent_executable",
        lambda: "/tmp/current/bin/meshagent",
    )

    result = CliRunner().invoke(
        get_command(root_cli.app),
        [
            "launch",
            "claude",
            "--project-id",
            "project-123",
            "--api-url",
            "https://api.meshagent.test",
            "--",
            "-p",
            "say hi",
        ],
    )

    assert result.exit_code == 5
    assert launches == [
        {
            "project_id": "project-123",
            "api_url": "https://api.meshagent.test",
            "extra_args": ["-p", "say hi"],
            "meshagent_executable": "/tmp/current/bin/meshagent",
        }
    ]


def test_launch_codex_command_prints_errors_and_exits(monkeypatch) -> None:
    monkeypatch.setattr(
        "meshagent.cli.launch.launch_codex",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Codex is not installed.")),
    )

    result = CliRunner().invoke(get_command(root_cli.app), ["launch", "codex"])

    assert result.exit_code == 1
    assert "Codex is not installed." in result.output


def test_launch_claude_command_prints_errors_and_exits(monkeypatch) -> None:
    monkeypatch.setattr(
        "meshagent.cli.launch.launch_claude",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("Claude is not installed.")
        ),
    )

    result = CliRunner().invoke(get_command(root_cli.app), ["launch", "claude"])

    assert result.exit_code == 1
    assert "Claude is not installed." in result.output
