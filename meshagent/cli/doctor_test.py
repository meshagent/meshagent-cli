from __future__ import annotations

import json

from typer._click.testing import CliRunner

from meshagent.cli import doctor as doctor_module
from meshagent.cli.doctor import (
    _format_finding_label,
    diagnose_project,
    doctor_command,
)
from meshagent.cli.version import __version__ as MESHAGENT_CLIENT_VERSION


def test_doctor_recommends_init_for_empty_project(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Unknown" in result.output
    assert "No identifiable deployable project was detected" in result.output
    assert "meshagent create" in result.output
    assert "Python backend agent project" in result.output
    assert "Deployment checks" in result.output
    assert "Project detection check" in result.output
    assert "meshagent deploy ." not in result.output


def test_doctor_reports_python_roomclient_deploy_gaps(tmp_path) -> None:
    project = tmp_path / "testproj"
    project.mkdir()
    (project / "requirements.txt").write_text(
        "meshagent-api==0.5.18\n",
        encoding="utf-8",
    )
    (project / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "ThreadingHTTPServer(('0.0.0.0', 8000), Handler)\n"
        "if self.path == '/health': pass\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(project)])

    assert result.exit_code == 0
    assert "Detected project: Python" in result.output
    assert "Official RoomClient SDK: detected (meshagent-api)" in result.output
    assert "Python project metadata: add pyproject.toml" in result.output
    assert (
        "Python meshagent-api version mismatch: requirements.txt has 0.5.18"
        in result.output
    )
    assert f"meshagent client is {MESHAGENT_CLIENT_VERSION}" in result.output
    assert "[warning] Dockerfile missing" in result.output
    assert "--wait" in result.output
    assert "--tag testproj:latest" in result.output
    assert "--tag <repository>:<tag>" not in result.output
    assert result.output.count("meshagent deploy .") == 1
    assert "env -u" not in result.output
    assert "--meshagent-token agentDefault" in result.output
    assert "--meshagent-token full" not in result.output
    assert " -e MESHAGENT_" not in result.output
    assert "--no-optimize" not in result.output
    assert "Deployment checks" in result.output
    assert "Auto-fix project files" in result.output
    assert "meshagent doctor --fix" in result.output
    assert "Create pyproject.toml" in result.output
    assert "python-sdk-slim" in result.output
    assert "MeshAgent Python deployments must target Python 3.13" in result.output
    assert (
        "Python virtualenv check: no local Python virtual environment" in result.output
    )
    assert "python3 -m py_compile server.py" in result.output
    assert "WebSocketClientProtocol" in result.output
    assert "websocket_room_url" in result.output
    assert "MESHAGENT_ROOM_URL` is the in-room HTTP endpoint" in result.output
    assert "WSServerHandshakeError: 200" in result.output


def test_doctor_reports_python_source_sdk_import_without_project_dependency(
    tmp_path,
) -> None:
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "from aiohttp import web\n"
        "app = web.Application()\n"
        "app.router.add_get('/health', lambda request: web.Response(text='ok'))\n"
        "web.run_app(app, host='0.0.0.0', port=8000)\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "Python"
    assert diagnosis.sdk is None
    assert diagnosis.python_has_pyproject is False
    assert diagnosis.python_source_uses_sdk is True
    assert "Official RoomClient SDK: not detected" in result.output
    assert "Python project metadata: add pyproject.toml" in result.output
    assert "Python RoomClient SDK dependency: add meshagent-api" in result.output
    assert "does not declare `meshagent-api`" in result.output
    assert "meshagent doctor --fix" in result.output
    assert "Create pyproject.toml" in result.output
    assert "--meshagent-token agentDefault" in result.output
    assert "--meshagent-token full" not in result.output


def test_doctor_fix_writes_missing_python_pyproject_only(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "requests==2.32.3\nmeshagent-api==0.5.18\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "async def main():\n"
        "    async with RoomClient() as room:\n"
        "        await room.wait_for_close()\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "Applied fixes" in result.output
    assert "Create pyproject.toml with Python runtime metadata" in result.output
    assert "Run `meshagent doctor` and address remaining findings" in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    assert not (tmp_path / "Dockerfile").exists()
    assert 'requires-python = ">=3.13"' in pyproject
    assert '"requests==2.32.3"' in pyproject
    assert f'"meshagent-api=={MESHAGENT_CLIENT_VERSION}"' in pyproject
    assert '"meshagent-api==0.5.18"' not in pyproject


def test_doctor_fix_skips_multi_file_python_project(tmp_path) -> None:
    (tmp_path / "server.py").write_text(
        "from app import create_app\napp = create_app()\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "def create_app():\n    return object()\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    fix_result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "meshagent doctor --fix" not in result.output
    assert fix_result.exit_code == 0
    assert "No auto-fixable missing files were found" in fix_result.output
    assert not (tmp_path / "Dockerfile").exists()
    assert not (tmp_path / "pyproject.toml").exists()


def test_doctor_fix_skips_node_project_without_ncc_metadata(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "const { RoomClient } = require('@meshagent/meshagent');\n"
        "console.log(RoomClient);\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    fix_result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "meshagent doctor --fix" in result.output
    assert "Node ncc optimization check" in result.output
    assert '"build": "ncc build server.js -o dist"' in result.output
    assert fix_result.exit_code == 0
    assert "Applied fixes" in fix_result.output
    assert "Update package.json MeshAgent SDK version and deploy script" in (
        fix_result.output
    )
    assert not (tmp_path / "Dockerfile").exists()
    package_json = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert package_json["dependencies"]["@meshagent/meshagent"] == (
        MESHAGENT_CLIENT_VERSION
    )


def test_doctor_fix_skips_node_dockerfile_when_no_package_update_is_needed(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"ncc build server.js -o dist",'
        '"start":"node dist/index.js"},'
        '"devDependencies":{"@vercel/ncc":"^0.38.3"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "console.log('hello');\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "No auto-fixable missing files were found" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_doctor_fix_reports_no_autofix_for_empty_project(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "No auto-fixable missing files were found" in result.output
    assert "Run `meshagent doctor` and address remaining findings" in result.output
    assert not (tmp_path / "Dockerfile").exists()
    assert not (tmp_path / "pyproject.toml").exists()


def test_doctor_fix_does_not_overwrite_existing_files(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "existing"\n',
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\nprint(RoomClient)\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])

    assert result.exit_code == 0
    assert "No auto-fixable missing files were found" in result.output
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == (
        "FROM python:3.13-slim\n"
    )
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == (
        '[project]\nname = "existing"\n'
    )


def test_doctor_reports_matching_python_sdk_pin(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'requires-python = ">=3.13"\n'
        "dependencies = [\n"
        f'  "meshagent-api=={MESHAGENT_CLIENT_VERSION}",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "print(RoomClient)\n"
        "if '/health': pass\n"
        "PORT = 8000\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.python_sdk_versions == (
        ("pyproject.toml", MESHAGENT_CLIENT_VERSION),
    )
    assert (
        "Python meshagent-api version matches meshagent client: "
        f"{MESHAGENT_CLIENT_VERSION} (pyproject.toml)"
    ) in result.output
    assert "Python meshagent-api version mismatch" not in result.output


def test_doctor_allows_headless_python_backend_agent_without_ports(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'requires-python = ">=3.13"\n'
        "dependencies = [\n"
        f'  "meshagent-api=={MESHAGENT_CLIENT_VERSION}",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "from meshagent.api import RoomClient\n"
        "async def main():\n"
        "    async with RoomClient() as room:\n"
        "        await room.wait_for_close()\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.is_headless_backend_agent is True
    assert diagnosis.has_health_route is False
    assert diagnosis.has_http_port_hint is False
    assert "Backend agent does not require an HTTP /health route" in result.output
    assert (
        "Backend agent does not require exposed or published HTTP ports"
        in result.output
    )
    assert (
        "Headless backend-agent rule: RoomClient-only services can omit"
        in result.output
    )
    assert "RoomClient deploy-token check" not in result.output
    assert "[error] RoomClient deployment needs" not in result.output
    assert "--meshagent-token agentDefault" in result.output
    assert "--meshagent-token full" not in result.output
    deploy_commands = [
        line.strip()
        for line in result.output.splitlines()
        if "meshagent deploy ." in line
    ]
    assert len(deploy_commands) == 1
    assert "--public" not in deploy_commands[0]
    assert "--domain" not in deploy_commands[0]
    assert "--liveness" not in deploy_commands[0]


def test_doctor_warns_when_project_virtualenv_meshagent_api_version_mismatches(
    tmp_path,
) -> None:
    site_packages = (
        tmp_path
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "meshagent_api-0.1.0.dist-info"
    )
    site_packages.mkdir(parents=True)
    (tmp_path / ".venv" / "pyvenv.cfg").write_text(
        "home = /opt/python3.13\nversion = 3.13.5\n",
        encoding="utf-8",
    )
    (site_packages / "METADATA").write_text(
        "Name: meshagent-api\nVersion: 0.1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "print('python app')\nif '/health': pass\nPORT = 8000\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "Python"
    assert diagnosis.python_sdk_versions == ((".venv installed package", "0.1.0"),)
    assert (
        "Python meshagent-api version mismatch: .venv installed package has 0.1.0"
        in result.output
    )
    assert f"meshagent client is {MESHAGENT_CLIENT_VERSION}" in result.output


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
        "ThreadingHTTPServer(('0.0.0.0', 8000), Handler)\n"
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
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output


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
        "ThreadingHTTPServer(('0.0.0.0', 8000), Handler)\n"
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
        "const port = Number(process.env.PORT || 3000);\n"
        "server.listen(port, '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    diagnosis = diagnose_project(tmp_path)

    assert diagnosis.language == "JavaScript"
    assert diagnosis.javascript_flavor == "Node.js"
    assert diagnosis.sdk == "@meshagent/meshagent"
    assert diagnosis.sdk_versions == (("package.json", "0.38.4"),)
    assert diagnosis.has_deployment_artifact is False
    assert diagnosis.liveness_path == "/health"


def test_doctor_warns_when_javascript_roomclient_has_no_deploy_script(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        'FROM scratch\nVOLUME ["/data"]\n',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(Number(process.env.PORT || 3000), '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "[warning] RoomClient deploy-token check: add a package.json deploy script"
        in result.output
    )
    assert "`--tag`" in result.output
    assert "`--meshagent-token agentDefault`" in result.output
    assert "meshagent doctor --fix" in result.output

    fix_result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])
    package_json = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))

    assert fix_result.exit_code == 0
    assert "Update package.json MeshAgent SDK version and deploy script" in (
        fix_result.output
    )
    deploy_script = package_json["scripts"]["deploy"]
    assert deploy_script.startswith("meshagent deploy .")
    assert '--room "$MESHAGENT_ROOM"' in deploy_script
    assert f"--tag {tmp_path.name}:latest" in deploy_script
    assert "--public" in deploy_script
    assert "--domain <domain>" in deploy_script
    assert "--liveness /health" in deploy_script
    assert "--room-mount /:/data:rw" in deploy_script
    assert "--wait" in deploy_script
    assert "--meshagent-token agentDefault" in deploy_script
    assert package_json["dependencies"]["@meshagent/meshagent"] == (
        MESHAGENT_CLIENT_VERSION
    )


def test_doctor_warns_when_javascript_roomclient_deploy_script_lacks_token(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js","deploy":"meshagent deploy . --wait"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(Number(process.env.PORT || 3000), '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "[warning] RoomClient deploy-token check: package.json deploy script should "
        "include `--tag` and `--meshagent-token agentDefault`."
    ) in result.output
    assert "meshagent doctor --fix" in result.output

    fix_result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])
    package_json = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))

    assert fix_result.exit_code == 0
    assert package_json["scripts"]["deploy"] == (
        f"meshagent deploy . --wait --tag {tmp_path.name}:latest "
        "--meshagent-token agentDefault"
    )
    assert package_json["dependencies"]["@meshagent/meshagent"] == (
        MESHAGENT_CLIENT_VERSION
    )


def test_doctor_fix_replaces_javascript_roomclient_deploy_token_value(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js",'
        '"deploy":"meshagent deploy . --meshagent-token full"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(Number(process.env.PORT || 3000), '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])
    package_json = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert package_json["scripts"]["deploy"] == (
        f"meshagent deploy . --meshagent-token agentDefault "
        f"--tag {tmp_path.name}:latest"
    )
    assert package_json["dependencies"]["@meshagent/meshagent"] == (
        MESHAGENT_CLIENT_VERSION
    )


def test_doctor_fix_adds_mount_for_javascript_dockerfile_volume(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js",'
        '"deploy":"meshagent deploy . --wait --tag app:latest '
        '--meshagent-token agentDefault"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text(
        'FROM scratch\nVOLUME ["/data"]\n',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(Number(process.env.PORT || 3000), '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "[warning] RoomClient deploy-token check: package.json deploy script should "
        "include a mount for Dockerfile volume `/data`."
    ) in result.output

    fix_result = CliRunner().invoke(doctor_command, ["--fix", str(tmp_path)])
    package_json = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))

    assert fix_result.exit_code == 0
    assert package_json["scripts"]["deploy"] == (
        "meshagent deploy . --wait --tag app:latest "
        "--meshagent-token agentDefault --room-mount /:/data:rw"
    )


def test_doctor_accepts_javascript_roomclient_deploy_script_with_token(
    tmp_path,
) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"start":"node server.js",'
        '"deploy":"meshagent deploy . --wait --tag app:latest '
        '--meshagent-token agentDefault"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "server.listen(Number(process.env.PORT || 3000), '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "[ok] RoomClient deploy-token check: package.json deploy script injects "
        "`--meshagent-token agentDefault`."
    ) in result.output
    assert "[warning] RoomClient deploy-token check" not in result.output
    assert "Deploy from this directory:" in result.output
    assert "npm run deploy" in result.output
    assert "meshagent deploy ." not in result.output


def test_doctor_reports_node_roomclient_commonjs_guidance(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"type":"module","scripts":{"start":"node server.js"},'
        '"dependencies":{"@meshagent/meshagent":"0.38.4"}}',
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text(
        "import { RoomClient } from '@meshagent/meshagent';\n"
        "const port = Number(process.env.PORT || 3000);\n"
        "server.listen(port, '0.0.0.0');\n"
        "if (req.url === '/health') {}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: JavaScript" in result.output
    assert "JavaScript flavor: Node.js" in result.output
    assert "SDK checks" in result.output
    assert 'require("@meshagent/meshagent")' in result.output
    assert "compile TypeScript to CommonJS" in result.output
    assert (
        "@meshagent/meshagent version is behind meshagent client: "
        "package.json has 0.38.4"
    ) in result.output
    assert "Deployment checks" in result.output
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
        "const port = Number(process.env.PORT || 3000);\n"
        "http.createServer((req, res) => { if (req.url === '/health') res.end('ok'); }).listen(port, '0.0.0.0');\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk_versions == (("package.json", "0.38.4"),)
    assert "JavaScript flavor: Node.js/TypeScript" in result.output
    assert (
        "@meshagent/meshagent version is behind meshagent client: "
        "package.json has 0.38.4"
    ) in result.output
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output
    assert "Local build check: `npm install && npm run build`" in result.output
    assert '`compilerOptions.module` to `"CommonJS"`' in result.output
    assert '`moduleResolution` to `"Node"`' in result.output
    assert '`"type": "module"`' in result.output
    assert "`node dist/server.js`" in result.output
    assert "tsconfig `module: NodeNext`" in result.output
    assert 'curl -fsS "$PUBLIC_URL/"' in result.output
    assert (
        "[warning] RoomClient deploy-token check: add a package.json deploy script"
        in result.output
    )
    assert "--meshagent-token agentDefault" in result.output
    assert "--meshagent-token full" not in result.output


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
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output
    assert "--room-mount /:/data:rw" in result.output
    assert "nginx /health route returning 200" in result.output
    assert "serve the generated `dist` or `build` directory with nginx" in result.output
    assert "writable `/data` room mount" in result.output
    assert "must not write pid, cache, or temp files under `/var`" in result.output
    assert 'curl -fsS "$PUBLIC_URL/"' in result.output
    assert "Add a package.json start script" not in result.output
    assert "--meshagent-token" not in result.output


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
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output
    assert "--liveness /" in result.output
    assert "Next.js Docker context check: add a `.dockerignore`" in result.output
    assert "`node_modules`, `.next`, `dist`, `build`" in result.output
    assert "public root URL returns HTTP 200" in result.output


def test_doctor_reports_go_without_roomclient_token_guidance(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    (tmp_path / "server.go").write_text(
        'package main\nfunc main() { http.ListenAndServe(":8001", nil) }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Go" in result.output
    assert "Official RoomClient SDK: not detected" in result.output
    assert "--meshagent-token" not in result.output
    assert "env -u" not in result.output
    assert "Deployment checks" in result.output
    assert "go build -o /tmp/meshagent-doctor-server server.go" in result.output
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output


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
        'app.Run("http://0.0.0.0:5000");\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: .NET" in result.output
    assert "Official RoomClient SDK: detected (Meshagent.Api)" in result.output
    assert (
        "Meshagent.Api version is behind meshagent client: "
        "DoctorDotnetRoomClient.csproj has 0.38.4"
    ) in result.output
    assert "SDK checks" in result.output
    assert "using Meshagent.Api.Room;" in result.output
    assert "Deployment checks" in result.output
    assert "dotnet publish -c Release" in result.output
    assert "--disable-build-servers" in result.output
    assert "/p:UseSharedCompilation=false" in result.output
    assert "Dockerfile missing: add Dockerfile or meshagent.yaml" in result.output


def test_doctor_warns_when_dart_sdk_version_is_behind(tmp_path) -> None:
    (tmp_path / "pubspec.yaml").write_text(
        'name: dart_roomclient\npublish_to: "none"\ndependencies:\n'
        "  meshagent: ^0.38.4\n",
        encoding="utf-8",
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "server.dart").write_text(
        "import 'package:meshagent/meshagent.dart';\n"
        "Future<void> main() async { RoomClient(); }\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert diagnosis.language == "Dart"
    assert diagnosis.sdk == "meshagent"
    assert diagnosis.sdk_versions == (("pubspec.yaml", "0.38.4"),)
    assert (
        "meshagent version is behind meshagent client: pubspec.yaml has 0.38.4"
        in result.output
    )


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
        'app.Run("http://0.0.0.0:5000");\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "Local build check: unavailable here because `dotnet` is not on PATH"
        in result.output
    )
    assert "Local build check: `dotnet publish" not in result.output


def test_doctor_reports_existing_dockerfile(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ruby:3.4-alpine\n",
        encoding="utf-8",
    )
    (tmp_path / "server.rb").write_text(
        'TCPServer.new("0.0.0.0", 4567)\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Detected project: Ruby" in result.output
    assert "[ok] Deployment artifact found: Dockerfile" in result.output


def test_doctor_warns_when_deploy_spec_exists_without_dockerfile(tmp_path) -> None:
    (tmp_path / ".meshagent").mkdir()
    (tmp_path / ".meshagent" / "deploy.yaml").write_text("", encoding="utf-8")
    (tmp_path / "server.py").write_text(
        "print('python app')\nif '/health': pass\nPORT = 8000\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "[ok] Deployment artifact found: .meshagent/deploy.yaml" in result.output
    assert (
        "[warning] Dockerfile missing: add Dockerfile or meshagent.yaml"
        in result.output
    )
    assert "[ok] Deploy spec found: .meshagent/deploy.yaml" in result.output


def test_doctor_warns_for_non_optimized_python_dockerfile(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text(
        'FROM python:3.13-slim\nWORKDIR /app\nCOPY . .\nCMD ["python", "server.py"]\n',
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.13"\n',
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        "print('python app')\nif '/health': pass\nPORT = 8000\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(doctor_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "[ok] Deployment artifact found: Dockerfile" in result.output
    assert "[warning] Dockerfile is not MeshAgent-optimized" in result.output
    assert "LABEL meshagent.runtime=python or node" in result.output
    assert "Auto-fix project files" not in result.output


def test_doctor_finding_labels_use_rich_status_colors() -> None:
    assert _format_finding_label("ok") == "[green]\\[ok][/]"
    assert _format_finding_label("warning") == "[yellow]\\[warning][/]"
    assert _format_finding_label("error") == "[red]\\[error][/]"
