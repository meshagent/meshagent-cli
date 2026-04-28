from __future__ import annotations

from click.testing import CliRunner

from meshagent.cli.doctor import diagnose_project, doctor_command


def test_root_help_lists_doctor_command() -> None:
    from pathlib import Path

    cli_source = Path(__file__).with_name("cli.py").read_text(encoding="utf-8")

    assert 'name="doctor"' in cli_source
    assert 'module="meshagent.cli.doctor"' in cli_source


def test_doctor_reports_python_roomclient_deploy_gaps(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "meshagent-api==0.5.18\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "ThreadingHTTPServer(('0.0.0.0', 8080), Handler)\n"
        "if self.path == '/health': pass\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Python" in result.output
    assert "Official RoomClient SDK: detected (meshagent-api)" in result.output
    assert "[missing] Deployment artifact" in result.output
    assert "--no-wait" in result.output
    assert "--meshagent-token full" in result.output
    assert 'MESHAGENT_ROOM="$MESHAGENT_ROOM"' in result.output


def test_doctor_reports_javascript_roomclient_deploy_gaps(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js"},"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(8080, '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    diagnosis = diagnose_project(tmp_path)

    assert diagnosis.language == "JavaScript"
    assert diagnosis.sdk == "@meshagent/meshagent"
    assert diagnosis.has_deployment_artifact is False
    assert "npm install --omit=dev" in diagnosis.dockerfile


def test_doctor_reports_go_without_roomclient_token_guidance(tmp_path) -> None:
    (tmp_path / "server.go").write_text(
        'package main\nfunc main() { http.ListenAndServe(":8080", nil) }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Go" in result.output
    assert "Official RoomClient SDK: not detected" in result.output
    assert "--meshagent-token full" not in result.output
    assert "go build -o server server.go" in result.output


def test_doctor_reports_existing_dockerfile(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ruby:3.4-alpine\n",
        encoding="utf-8",
    )
    (tmp_path / "server.rb").write_text(
        'TCPServer.new("0.0.0.0", 8080)\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Ruby" in result.output
    assert "[ok] Deployment artifact found: Dockerfile" in result.output
