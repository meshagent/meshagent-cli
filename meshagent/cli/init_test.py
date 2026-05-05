from __future__ import annotations

from click.testing import CliRunner

from meshagent.cli import init as init_module
from meshagent.cli.doctor import diagnose_project
from meshagent.cli.init import init_command


def test_init_creates_python_backend_agent_by_default_in_non_tty(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(init_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "meshagent init" in result.output
    assert "Created a minimal deployable Python backend agent" in result.output
    assert "meshagent doctor" in result.output
    assert "meshagent deploy ." in result.output
    assert "--meshagent-token full" in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")

    assert 'requires-python = ">=3.13"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert '"aiohttp[speedups]~=3.13.0"' in pyproject
    assert "from aiohttp import web" in server_py
    assert "RoomClient(protocol_factory=protocol.create_factory())" in server_py
    assert "WebSocketClientProtocol" in server_py
    assert "ThreadingHTTPServer" not in server_py
    assert "COPY pyproject.toml server.py ./" in dockerfile
    assert "RUN pip install --no-cache-dir ." in dockerfile

    assert diagnosis.language == "Python"
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.python_has_pyproject is True
    assert diagnosis.python_source_uses_sdk is True


def test_init_creates_python_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "python",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable Python web server" in result.output
    assert "--meshagent-token full" not in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")

    assert '"aiohttp[speedups]~=3.13.0"' in pyproject
    assert "meshagent-api" not in pyproject
    assert "from aiohttp import web" in server_py
    assert "RoomClient" not in server_py
    assert diagnosis.sdk is None
    assert diagnosis.python_has_pyproject is True
    assert diagnosis.python_source_uses_sdk is False


def test_init_creates_javascript_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "javascript",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable JavaScript web server" in result.output
    assert "meshagent deploy ." in result.output
    package_json = tmp_path / "package.json"
    server_js = tmp_path / "server.js"
    dockerfile = tmp_path / "Dockerfile"
    assert package_json.is_file()
    assert server_js.is_file()
    assert dockerfile.is_file()
    assert '"start": "node server.js"' in package_json.read_text(encoding="utf-8")
    assert "@meshagent/meshagent" not in package_json.read_text(encoding="utf-8")
    assert "hello from meshagent init" in server_js.read_text(encoding="utf-8")
    assert "FROM node:22-alpine" in dockerfile.read_text(encoding="utf-8")


def test_init_creates_javascript_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "javascript",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable JavaScript backend agent" in result.output
    assert "--meshagent-token full" in result.output
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert "RoomClient" in (tmp_path / "server.js").read_text(encoding="utf-8")
    assert "npm install --omit=dev" in (tmp_path / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_init_creates_dotnet_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            ".NET",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable .NET backend agent" in result.output
    assert "--meshagent-token full" in result.output
    assert 'PackageReference Include="Meshagent.Api"' in (
        tmp_path / "MeshAgentHello.csproj"
    ).read_text(encoding="utf-8")
    assert "RoomClient" in (tmp_path / "Program.cs").read_text(encoding="utf-8")


def test_init_creates_flutter_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "flutter",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable Flutter web server" in result.output
    assert "--room-mount /:/data:rw" in result.output
    assert (tmp_path / "pubspec.yaml").is_file()
    assert (tmp_path / "lib" / "main.dart").is_file()
    assert (tmp_path / "web" / "index.html").is_file()
    assert "flutter build web --release" in (tmp_path / "Dockerfile").read_text(
        encoding="utf-8"
    )


def test_init_creates_dart_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "dart",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable Dart backend agent" in result.output
    assert "--meshagent-token full" in result.output
    assert "meshagent:" in (tmp_path / "pubspec.yaml").read_text(encoding="utf-8")
    assert "RoomClient" in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "FROM dart:stable" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")


def test_init_rejects_unknown_language(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        ["--language", "rust", "--no-interactive", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Unsupported language: rust" in result.output
    assert "python, javascript, dotnet, dart-flutter" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_rejects_unknown_focus(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        ["--focus", "desktop", "--no-interactive", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Unsupported focus: desktop" in result.output
    assert "webserver, backend-agent" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_launches_tui_when_tty_and_language_or_focus_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(init_module, "_stdio_is_interactive", lambda: True)

    captured_languages: list[tuple[str, str, str]] = []
    captured_focuses: list[tuple[str, str, str]] = []

    def fake_run_init_tui(*, language_choices, focus_choices):
        captured_languages.extend(language_choices)
        captured_focuses.extend(focus_choices)
        return "javascript", "backend-agent"

    monkeypatch.setattr(init_module, "_run_init_tui", fake_run_init_tui)

    result = CliRunner().invoke(init_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert [choice[0] for choice in captured_languages] == [
        "python",
        "javascript",
        "dotnet",
        "dart-flutter",
    ]
    assert [choice[0] for choice in captured_focuses] == [
        "webserver",
        "backend-agent",
    ]
    assert (tmp_path / "server.js").is_file()
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / "server.py").exists()


def test_init_no_interactive_bypasses_tui_even_when_tty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(init_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        init_module,
        "_run_init_tui",
        lambda *, language_choices, focus_choices: (_ for _ in ()).throw(
            AssertionError("TUI should not launch")
        ),
    )

    result = CliRunner().invoke(
        init_command,
        [
            "--no-interactive",
            "--language",
            "python",
            "--focus",
            "backend-agent",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (tmp_path / "server.py").is_file()
    assert (tmp_path / "pyproject.toml").is_file()


def test_init_interactive_requires_tty_when_language_missing(tmp_path) -> None:
    result = CliRunner().invoke(init_command, ["--interactive", str(tmp_path)])

    assert result.exit_code == 1
    assert "Interactive mode requires a TTY" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_recommends_doctor_for_existing_code(tmp_path) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")

    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "javascript",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "meshagent init" in result.output
    assert "Existing application code" in result.output
    assert "No files were written" in result.output
    assert "meshagent doctor" in result.output
    assert not (tmp_path / "Dockerfile").exists()
