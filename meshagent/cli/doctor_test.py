from __future__ import annotations

from click.testing import CliRunner

from meshagent.cli import doctor as doctor_module
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
    assert "--wait" in result.output
    assert "--meshagent-token full" in result.output
    assert 'MESHAGENT_ROOM="$MESHAGENT_ROOM"' in result.output
    assert "Diagnostics for Codex" in result.output
    assert "FROM python:3.13-slim" in result.output
    assert "MeshAgent Python deployments must target Python 3.13" in result.output
    assert "No local Python virtual environment detected" in result.output
    assert "python3 -m py_compile server.py" in result.output
    assert "WebSocketClientProtocol" in result.output
    assert "websocket_room_url" in result.output
    assert "MESHAGENT_ROOM_URL` is the in-room HTTP endpoint" in result.output
    assert "WSServerHandshakeError: 200" in result.output


def test_doctor_reports_older_python_runtime_upgrade_guidance(tmp_path) -> None:
    (tmp_path / ".python-version").write_text("3.11.9\n", encoding="utf-8")
    (tmp_path / "runtime.txt").write_text("python-3.11.9\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\nversion = 3.11.9\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.10,<3.13"\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "meshagent-api==0.5.18\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from http.server import ThreadingHTTPServer\n"
        "ThreadingHTTPServer(('0.0.0.0', 8080), Handler)\n"
        "if self.path == '/health': pass\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "Python"
    assert diagnosis.python_runtime_findings
    assert diagnosis.python_virtualenv_versions == ((".venv", "3.11.9"),)
    assert "Upgrade Python runtime metadata to 3.13 before deploying" in result.output
    assert "No local Python 3.13 virtual environment detected" in result.output
    assert "Local virtual environment `.venv` uses Python `3.11.9`" in result.output
    assert "python3.13 -m venv .venv" in result.output
    assert ".python-version declares `3.11.9`" in result.output
    assert "runtime.txt declares `python-3.11.9`" in result.output
    assert "`pyproject.toml` project.requires-python is `>=3.10,<3.13`" in result.output
    assert "MeshAgent Python deployments must target Python 3.13" in result.output
    assert "FROM python:3.13-slim" in result.output


def test_doctor_detects_nested_python_313_virtualenv(tmp_path) -> None:
    service_dir = tmp_path / "service"
    service_dir.mkdir()
    (service_dir / "venv").mkdir()
    (service_dir / "venv" / "pyvenv.cfg").write_text(
        "home = /opt/python3.13\nversion = 3.13.5\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "server.py").write_text(
        "from http.server import ThreadingHTTPServer\n"
        "ThreadingHTTPServer(('0.0.0.0', 8080), Handler)\n"
        "if self.path == '/health': pass\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.python_virtualenv_versions == (("service/venv", "3.13.5"),)
    assert "Local Python 3.13 virtual environment detected" in result.output
    assert (
        "Local virtual environment `service/venv` uses Python `3.13.5`" in result.output
    )


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
    assert diagnosis.javascript_flavor == "Node.js"
    assert diagnosis.sdk == "@meshagent/meshagent"
    assert diagnosis.has_deployment_artifact is False
    assert "npm install --omit=dev" in diagnosis.dockerfile
    assert diagnosis.liveness_path == "/health"


def test_doctor_reports_node_roomclient_commonjs_guidance(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"type":"module","scripts":{"start":"node server.js"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(8080, '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: JavaScript" in result.output
    assert "JavaScript flavor: Node.js" in result.output
    assert "SDK runtime guidance" in result.output
    assert 'require("@meshagent/meshagent")' in result.output
    assert "compile TypeScript to CommonJS" in result.output
    assert "Diagnostics for Codex" in result.output
    assert "ERR_MODULE_NOT_FOUND" in result.output
    assert 'module: "CommonJS"' in result.output
    assert 'curl -fsS "$PUBLIC_URL/"' in result.output


def test_doctor_reports_typescript_node_build_guidance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    (tmp_path / "package.json").write_text(
        '{"type":"module","scripts":{"build":"tsc","start":"node dist/server.js"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"},'
        '"devDependencies":{"typescript":"5.8.2"}}',
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"outDir":"dist","module":"NodeNext"}}',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.ts").write_text(
        "import http from 'node:http';\n"
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "http.createServer((req, res) => { if (req.url === '/health') res.end('ok'); }).listen(8080, '0.0.0.0');\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert "JavaScript flavor: Node.js/TypeScript" in result.output
    assert "RUN npm run build && npm prune --omit=dev" in result.output
    assert (
        "Fast local build/syntax check: `npm install && npm run build`" in result.output
    )
    assert '`compilerOptions.module` to `"CommonJS"`' in result.output
    assert '`moduleResolution` to `"Node"`' in result.output
    assert '`"type": "module"`' in result.output
    assert "`node dist/server.js`" in result.output
    assert "tsconfig `module: NodeNext`" in result.output
    assert 'curl -fsS "$PUBLIC_URL/"' in result.output
    assert "--meshagent-token full" in result.output


def test_doctor_reports_react_vite_static_deploy_guidance(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"vite build"},'
        '"dependencies":{"@vitejs/plugin-react":"latest","vite":"latest","react":"latest","react-dom":"latest"},'
        '"devDependencies":{"typescript":"5.8.2"}}',
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export function App() { return <main>ok</main>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "React/Vite"
    assert diagnosis.liveness_path == "/health"
    assert "JavaScript flavor: React/Vite" in result.output
    assert "nginx:1.27-alpine" in result.output
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in result.output
    assert "nginx /health route returning 200" in result.output
    assert "serve the generated `dist` or `build` directory with nginx" in result.output
    assert 'curl -fsS "$PUBLIC_URL/"' in result.output
    assert "Add a package.json start script" not in result.output
    assert "--meshagent-token full" not in result.output


def test_doctor_reports_nextjs_liveness_root_guidance(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"next build","start":"next start"},'
        '"dependencies":{"next":"latest","react":"latest","react-dom":"latest"}}',
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "export default function Page() { return <main>ok</main>; }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "JavaScript"
    assert diagnosis.javascript_flavor == "Next.js"
    assert diagnosis.liveness_path == "/"
    assert "JavaScript flavor: Next.js" in result.output
    assert "ENV HOSTNAME=0.0.0.0" in result.output
    assert 'CMD ["npm", "start", "--", "-H", "0.0.0.0", "-p", "8080"]' in result.output
    assert "--liveness /" in result.output
    assert "public root URL returns HTTP 200" in result.output


def test_doctor_reports_go_without_roomclient_token_guidance(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    (tmp_path / "server.go").write_text(
        'package main\nfunc main() { http.ListenAndServe(":8080", nil) }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Go" in result.output
    assert "Official RoomClient SDK: not detected" in result.output
    assert "--meshagent-token full" not in result.output
    assert "Diagnostics for Codex" in result.output
    assert "go build -o server server.go" in result.output


def test_doctor_reports_dotnet_roomclient_namespace_guidance(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    (tmp_path / "DoctorDotnetRoomClient.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        "  <ItemGroup>\n"
        '    <PackageReference Include="Meshagent.Api" Version="0.38.4" />\n'
        "  </ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )
    (tmp_path / "Program.cs").write_text(
        "using Meshagent.Api;\n"
        "var room = new RoomClient();\n"
        'app.MapGet("/health", () => "ok");\n'
        'app.Run("http://0.0.0.0:8080");\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: .NET" in result.output
    assert "Official RoomClient SDK: detected (Meshagent.Api)" in result.output
    assert "SDK runtime guidance" in result.output
    assert "using Meshagent.Api.Room;" in result.output
    assert "Diagnostics for Codex" in result.output
    assert "dotnet publish -c Release" in result.output


def test_doctor_reports_unavailable_local_tool_without_recommending_check(
    tmp_path, monkeypatch
) -> None:
    def fake_which(name: str) -> str | None:
        return None if name == "dotnet" else f"/usr/bin/{name}"

    monkeypatch.setattr(doctor_module.shutil, "which", fake_which)

    (tmp_path / "DoctorDotnetRoomClient.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        "  <ItemGroup>\n"
        '    <PackageReference Include="Meshagent.Api" Version="0.38.4" />\n'
        "  </ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )
    (tmp_path / "Program.cs").write_text(
        "using Meshagent.Api;\n"
        'app.MapGet("/health", () => "ok");\n'
        'app.Run("http://0.0.0.0:8080");\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "Local build/syntax check unavailable here because `dotnet` is not on PATH"
        in result.output
    )
    assert "Fast local build/syntax check: `dotnet publish" not in result.output


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
