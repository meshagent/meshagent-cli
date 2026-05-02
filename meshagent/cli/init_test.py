from __future__ import annotations

from click.testing import CliRunner

from meshagent.cli.init import init_command


def test_init_creates_minimal_python_project_in_empty_directory(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(init_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "meshagent init" in result.output
    assert "Created a minimal deployable Python hello world project" in result.output
    assert "meshagent doctor" in result.output
    assert "meshagent deploy ." in result.output
    server_py = tmp_path / "server.py"
    dockerfile = tmp_path / "Dockerfile"
    assert server_py.is_file()
    assert dockerfile.is_file()
    assert "hello from meshagent init" in server_py.read_text(encoding="utf-8")
    assert 'self.path == "/health"' in server_py.read_text(encoding="utf-8")
    assert "FROM python:3.13-slim" in dockerfile.read_text(encoding="utf-8")


def test_init_recommends_doctor_for_existing_code(tmp_path) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")

    result = CliRunner().invoke(init_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "meshagent init" in result.output
    assert "Existing application code" in result.output
    assert "No files were written" in result.output
    assert "meshagent doctor" in result.output
    assert not (tmp_path / "Dockerfile").exists()
