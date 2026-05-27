from __future__ import annotations

from click.testing import CliRunner

from meshagent.api.specs.service import ServiceTemplateSpec
from meshagent.cli import create as create_module
from meshagent.cli.doctor import diagnose_project
from meshagent.cli.create import create_command


def _assert_no_dockerfile(project_path) -> None:
    assert not (project_path / "Dockerfile").exists()


def _assert_runtime_image_mount_deploy_yaml(
    project_path,
    *,
    runtime: str,
    command: str,
) -> ServiceTemplateSpec:
    deploy_yaml = (project_path / ".meshagent" / "deploy.yaml").read_text(
        encoding="utf-8"
    )
    spec = ServiceTemplateSpec.from_yaml(
        deploy_yaml,
        values={
            "image": "registry.example.com/test-app:dev",
            "from_email": "from@example.com",
            "to_email": "to@example.com",
        },
    )

    assert spec.container is not None
    container = spec.container
    assert container.image == f"meshagent/{runtime}:default"
    assert container.command == command
    assert container.working_dir == "/app"
    assert container.storage is not None
    assert container.storage.images is not None
    assert len(container.storage.images) == 1
    image_mount = container.storage.images[0]
    assert image_mount.image == "registry.example.com/test-app:dev"
    assert image_mount.path == "/app"
    assert image_mount.subpath == "app"
    assert image_mount.read_only is True
    return spec


def _assert_node_content_toolkit(
    source: str,
    *,
    language_label: str,
    class_prefix: str,
    content_path: str,
) -> None:
    assert "RoomClient" in source
    assert "startHostedToolkit" in source
    assert "public_: true" in source
    assert "MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS" in source
    assert f"class {class_prefix}ContentToolkit extends Toolkit" in source
    assert 'name: "create"' in source
    assert 'name: "update"' in source
    assert 'name: "search"' in source
    assert f'title: "{language_label} Local Content Toolkit"' in source
    assert "room.agents.invokeTool" in source
    assert "meshagent-create-proof" in source
    assert content_path in source
    assert "MESHAGENT_CREATE_DEV_PROBE" in source
    assert "room.storage.upload" not in source
    assert "readContentSync" not in source
    assert "fs.readFileSync" not in source
    assert "await fs.promises.readFile" in source


def _assert_node_agent_toolkit(
    source: str,
    *,
    language_label: str,
    class_prefix: str,
    proof_path: str,
) -> None:
    assert "RoomClient" in source
    assert "startHostedToolkit" in source
    assert "public_: true" in source
    assert "MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS" in source
    assert f"class {class_prefix}AgentToolkit extends Toolkit" in source
    assert 'name: "ping"' in source
    assert 'name: "status"' in source
    assert 'name: "echo"' in source
    assert f'title: "{language_label} Local Agent Toolkit"' in source
    assert "room.agents.invokeTool" in source
    assert proof_path in source
    assert "MESHAGENT_CREATE_DEV_PROBE" in source
    assert "ContentToolkit" not in source


def test_create_templates_are_self_contained_directories() -> None:
    for template in create_module.TEMPLATES.values():
        files = create_module._walk_template_files(template.template_dir)

        assert "README.md" in files
        assert "AGENTS.md" in files
        assert "CLAUDE.md" in files
        assert ".meshagent/deploy.yaml" in files
        assert not any(path.startswith("../") for path in files)
        assert not any("/node_modules/" in f"/{path}/" for path in files)
        assert "package-lock.json" not in files


def test_init_creates_python_backend_agent_by_default_in_non_tty(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(create_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "meshagent create" in result.output
    assert "Created a minimal deployable Python Agent Toolkit" in result.output
    assert "meshagent doctor" not in result.output
    assert "Next steps:" in result.output
    assert "1. Install dependencies" in result.output
    assert "./scripts/install.sh" in result.output
    assert "2. Run locally" in result.output
    assert "./scripts/dev.sh" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "3. Deploy" in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "To install an agent in your room that uses this tool run:" in result.output
    assert "meshagent process deploy --room <room>" in result.output
    assert "--agent-name meshagent-create-python-agent" in result.output
    assert "--require-toolkit meshagent.create.python-agent" in result.output
    assert (
        "Use the meshagent.create.python-agent toolkit to answer ping, status, and echo requests."
        in result.output
    )
    assert "--meshagent-token full" not in result.output
    assert " -e " not in result.output
    assert "--liveness" not in result.output
    assert "--public" not in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    _assert_no_dockerfile(tmp_path)

    assert "Python Agent Toolkit" in readme
    assert "./scripts/install.sh" in readme
    assert "./scripts/dev.sh" in readme
    assert "./scripts/deploy.sh" in readme
    assert "meshagent process deploy --room <room>" in readme
    assert "--agent-name meshagent-create-python-agent" in readme
    assert "--require-toolkit meshagent.create.python-agent" in readme
    assert "Read `README.md`" in agents_md
    assert "Read `README.md`" in claude_md
    assert 'requires-python = ">=3.13"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert '"meshagent-tools==' in pyproject
    assert '"openai~=2.25.0"' in pyproject
    assert "aiohttp" not in pyproject
    assert "aiofiles" in pyproject
    assert "import aiofiles" in server_py
    assert "asyncio.to_thread" not in server_py
    assert "from aiohttp import web" not in server_py
    assert "RoomClient(protocol_factory=protocol.create_factory())" in server_py
    assert "WebSocketClientProtocol" in server_py
    assert "FunctionTool" in server_py
    assert "class PythonAgentToolkit" in server_py
    assert 'name="ping"' in server_py
    assert 'name="status"' in server_py
    assert 'name="echo"' in server_py
    assert "_start_hosted_toolkit" in server_py
    assert "agent-proof.json" in server_py
    assert "ThreadingHTTPServer" not in server_py
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="python",
        command="python -m server",
    )
    assert 'PYTHON="${PYTHON:-python3.13}"' in install_sh
    assert 'VENV="${VENV:-.venv}"' in install_sh
    assert 'PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"' in install_sh
    assert 'meshagent room connect -- "$VENV_PYTHON" -u server.py' in dev_sh
    assert "./scripts/install.sh" not in dev_sh
    assert "MESHAGENT_CREATE_DEV_PROBE" in server_py
    assert "room.agents.invoke_tool" in server_py
    assert "room.storage.upload" not in server_py
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait'
        in deploy_sh
    )

    assert diagnosis.language == "Python"
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.is_headless_backend_agent is True
    assert diagnosis.python_has_pyproject is True
    assert diagnosis.python_source_uses_sdk is True


def test_init_colorizes_next_steps_when_color_is_enabled(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "chatbot",
            "--no-interactive",
            str(tmp_path),
        ],
        color=True,
    )

    assert result.exit_code == 0
    assert "\x1b[" in result.output
    assert "Next steps:" in result.output
    assert "1. Install dependencies" in result.output
    assert "2. Run locally" in result.output
    assert "3. Deploy" in result.output


def test_init_colorizes_agent_toolkit_pairing_guidance(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
        color=True,
    )

    assert result.exit_code == 0
    assert "\x1b[" in result.output
    assert "To install an agent in your room that uses this tool run:" in result.output
    assert "--agent-name meshagent-create-typescript-agent" in result.output
    assert "--require-toolkit meshagent.create.typescript-agent" in result.output


def test_init_renders_meshagent_image_prefix_from_environment(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESHAGENT_IMAGE_PREFIX", "registry.example.com/custom")

    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "python",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    _assert_no_dockerfile(tmp_path)


def test_init_creates_python_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable Python Web App" in result.output
    assert "--meshagent-token" not in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    dev_content = (tmp_path / "dev-content.json").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    _assert_no_dockerfile(tmp_path)

    assert "Python Web App" in readme
    assert "./scripts/install.sh" in readme
    assert "./scripts/dev.sh" in readme
    assert "./scripts/deploy.sh" in readme
    assert "Read `README.md`" in agents_md
    assert "Read `README.md`" in claude_md
    assert '"aiohttp[speedups]~=3.13.0"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert '"meshagent-tools==' in pyproject
    assert '"openai~=2.25.0"' in pyproject
    assert "aiofiles" in pyproject
    assert "from aiohttp import web" in server_py
    assert "import aiofiles" in server_py
    assert "read_content_sync" not in server_py
    assert "asyncio.to_thread" not in server_py
    assert "content = await read_content()" in server_py
    assert 'app.router.add_get("/status", status)' in server_py
    assert 'app.router.add_get("/api/ping", ping)' in server_py
    assert "RoomClient" in server_py
    assert "FunctionTool" in server_py
    assert "class PythonContentToolkit" in server_py
    assert 'name="create"' in server_py
    assert 'name="update"' in server_py
    assert 'name="search"' in server_py
    assert "_start_hosted_toolkit" in server_py
    assert "dev-content.json" in server_py
    assert "MESHAGENT_CREATE_DEV_PROBE" in server_py
    assert "room.agents.invoke_tool" in server_py
    assert "room.storage.upload" not in server_py
    assert "hello from meshagent create" in dev_content
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="python",
        command="python -m server",
    )
    assert 'PYTHON="${PYTHON:-python3.13}"' in install_sh
    assert 'VENV="${VENV:-.venv}"' in install_sh
    assert 'PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"' in install_sh
    assert 'meshagent room connect -- "$VENV_PYTHON" -u server.py' in dev_sh
    assert "./scripts/install.sh" not in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait'
        in deploy_sh
    )
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.python_has_pyproject is True
    assert diagnosis.python_source_uses_sdk is True


def test_init_creates_python_contact_form_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "python",
            "--focus",
            "contact-form",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable Python Contact Form" in result.output
    assert "2. Create room" in result.output
    assert "3. Run locally" in result.output
    assert "4. Deploy" in result.output
    assert "meshagent rooms create <room> --if-not-exists" in result.output
    assert (
        "Before testing a submission, set up the sender mailbox for that room"
        in result.output
    )
    assert "New mailbox:" in result.output
    assert "Existing mailbox for that room:" in result.output
    assert (
        "meshagent mailbox create --address contact-<room-slug>@mail.meshagent.com --room <room> --queue contact-<room-slug>@mail.meshagent.com --public"
        in result.output
    )
    assert (
        "meshagent mailbox update contact-<room-slug>@mail.meshagent.com --room <room> --queue contact-<room-slug>@mail.meshagent.com --public"
        in result.output
    )
    assert "If create returns 409" in result.output
    assert "If CONTACT_FORM_TO is also a private MeshAgent mailbox" in result.output
    assert "CONTACT_FORM_FROM" in result.output
    assert "CONTACT_FORM_TO" in result.output
    assert "./scripts/dev.sh --room <room>" in result.output
    assert (
        "CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh --room <room>"
        in result.output
    )

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    deploy_yaml = (tmp_path / ".meshagent" / "deploy.yaml").read_text(encoding="utf-8")
    _assert_no_dockerfile(tmp_path)

    assert "Python Contact Form" in readme
    assert "meshagent rooms create <room> --if-not-exists" in readme
    assert "./scripts/dev.sh --room <room>" in readme
    assert "CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh --room <room>" in readme
    assert (
        "meshagent mailbox create --address contact-<room-slug>@mail.meshagent.com"
        in readme
    )
    assert "CONTACT_FORM_FROM" in readme
    assert "CONTACT_FORM_TO" in readme
    assert "Read `README.md`" in agents_md
    assert "Read `README.md`" in claude_md

    assert '"aiohttp[speedups]~=3.13.0"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert "EmailMessage" in server_py
    assert "smtplib.SMTP" in server_py
    assert "CONTACT_FORM_FROM" in server_py
    assert "CONTACT_FORM_TO" in server_py
    assert "SMTP_HOSTNAME" in server_py
    assert "MESHAGENT_MAIL_DOMAIN" in server_py
    assert "Unable to send mail: {detail}" in server_py
    assert 'app.router.add_get("/health", health)' in server_py
    assert 'app.router.add_post("/contact", submit_contact)' in server_py
    assert "asyncio.to_thread" in server_py
    assert "starttls" in server_py
    assert "await runner.cleanup()" in server_py
    assert "except KeyboardInterrupt" in server_py
    assert "Stopped contact form." in server_py
    assert "webbrowser" not in server_py
    assert "contact@mail.meshagent.com" in server_py
    assert "you@example.com" in server_py
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="python",
        command="python -m server",
    )
    assert 'PYTHON="${PYTHON:-python3.13}"' in install_sh
    assert 'VENV="${VENV:-.venv}"' in install_sh
    assert 'PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"' in install_sh
    assert "CONTACT_FORM_FROM" in dev_sh
    assert "mailbox_from_room" in dev_sh
    assert "CONTACT_FORM_TO" in dev_sh
    assert "SMTP_HOSTNAME" in dev_sh
    assert "SMTP_USERNAME" in dev_sh
    assert "mail.meshagent.com" in dev_sh
    assert "Browser will launch at $LOCAL_URL" in dev_sh
    assert "webbrowser.open(sys.argv[1])" in dev_sh
    assert "CONTACT_FORM_OPEN_BROWSER" in dev_sh
    assert "CONTACT_FORM_OPEN_BROWSER_PROMPT" not in dev_sh
    assert "read -r OPEN_BROWSER_ANSWER" not in dev_sh
    assert 'meshagent rooms create "$ROOM_NAME" --if-not-exists' in dev_sh
    assert 'meshagent room connect "$@" -- "$VENV_PYTHON" -u server.py' in dev_sh
    assert 'if [ "$status" -eq 130 ] || [ "$status" -eq 143 ]; then' in dev_sh
    assert "Stopped contact form." in dev_sh
    assert "If the room does not exist yet, create it first:" in dev_sh
    assert "meshagent rooms create <room> --if-not-exists" in dev_sh
    assert 'meshagent rooms create "$ROOM_NAME" --if-not-exists' not in deploy_sh
    assert "--meshagent-token agentDefault" in deploy_sh
    assert '--set "from_email=$CONTACT_FORM_FROM"' in deploy_sh
    assert '--set "to_email=$CONTACT_FORM_TO"' in deploy_sh
    assert "mailbox_from_room" in deploy_sh
    assert '"$@"' in deploy_sh
    assert "If you passed --room and the room does not exist yet" not in deploy_sh
    assert "meshagent deploy will prompt for a room interactively" not in deploy_sh
    assert "exec meshagent deploy ." in deploy_sh
    assert 'if [ "$status" -eq 130 ]; then' not in deploy_sh
    assert '  "$@" \\' in deploy_sh
    assert '  --tag "$IMAGE_TAG" \\' in deploy_sh
    assert "  --meshagent-token agentDefault" in deploy_sh
    assert "kind: ServiceTemplate" in deploy_yaml
    assert "name: from_email" in deploy_yaml
    assert "name: to_email" in deploy_yaml
    assert "type: email" in deploy_yaml
    assert "template: agent" in deploy_yaml
    assert "num: 8000" in deploy_yaml
    assert "CONTACT_FORM_FROM" in deploy_yaml
    assert "CONTACT_FORM_TO" in deploy_yaml
    assert diagnosis.language == "Python"
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.has_health_route is True
    assert diagnosis.has_http_port_hint is True


def test_init_creates_javascript_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable JavaScript Web App" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    package_json = tmp_path / "package.json"
    npmrc = tmp_path / ".npmrc"
    server_js = tmp_path / "server.js"
    dev_content_json = tmp_path / "dev-content.json"
    assert package_json.is_file()
    assert npmrc.is_file()
    assert server_js.is_file()
    assert dev_content_json.is_file()
    _assert_no_dockerfile(tmp_path)
    package_text = package_json.read_text(encoding="utf-8")
    npmrc_text = npmrc.read_text(encoding="utf-8")
    dev_content = dev_content_json.read_text(encoding="utf-8")
    assert "cache=.npm-cache" in npmrc_text
    assert "audit=false" in npmrc_text
    assert '"build": "ncc build server.js -o dist"' in package_text
    assert '"dev": "meshagent room connect -- node server.js"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-javascript:dev --public --liveness /health --wait"'
        in package_text
    )
    assert '"start": "node dist/index.js"' in package_text
    assert '"@vercel/ncc"' in package_text
    assert "@meshagent/meshagent" in package_text
    server_text = server_js.read_text(encoding="utf-8")
    assert "hello from meshagent create" in server_text
    assert 'request.url === "/status"' in server_text
    assert 'request.url === "/api/ping"' in server_text
    _assert_node_content_toolkit(
        server_text,
        language_label="JavaScript",
        class_prefix="JavaScript",
        content_path="dev-content.json",
    )
    assert "await readContent()" in server_text
    assert "hello from meshagent create" in dev_content
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )


def test_init_creates_javascript_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable JavaScript Agent Toolkit" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert "--meshagent-token full" not in result.output
    assert " -e " not in result.output
    assert "--liveness" not in result.output
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert "cache=.npm-cache" in (tmp_path / ".npmrc").read_text(encoding="utf-8")
    assert not (tmp_path / "dev-content.json").exists()
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    assert '"dev": "meshagent room connect -- node server.js"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-javascript-agent:dev --meshagent-token agentDefault --wait"'
        in package_text
    )
    server_js = (tmp_path / "server.js").read_text(encoding="utf-8")
    _assert_node_agent_toolkit(
        server_js,
        language_label="JavaScript",
        class_prefix="JavaScript",
        proof_path="agent-proof.json",
    )
    assert "server.listen" not in server_js
    _assert_no_dockerfile(tmp_path)
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )


def test_init_creates_typescript_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Web App" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "src" / "server.ts").is_file()
    assert (tmp_path / "src" / "dev-content.json").is_file()
    _assert_no_dockerfile(tmp_path)
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    npmrc_text = (tmp_path / ".npmrc").read_text(encoding="utf-8")
    assert "cache=.npm-cache" in npmrc_text
    assert "audit=false" in npmrc_text
    assert '"build": "ncc build src/server.ts -o dist"' in package_text
    assert '"dev": "meshagent room connect -- tsx src/server.ts"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript:dev --public --liveness /health --wait"'
        in package_text
    )
    assert '"start": "node dist/index.js"' in package_text
    assert '"tsx"' in package_text
    assert '"@vercel/ncc"' in package_text
    assert "@meshagent/meshagent" in package_text
    server_ts = (tmp_path / "src" / "server.ts").read_text(encoding="utf-8")
    assert "createServer" in server_ts
    assert 'request.url === "/status"' in server_ts
    assert 'request.url === "/api/ping"' in server_ts
    _assert_node_content_toolkit(
        server_ts,
        language_label="TypeScript",
        class_prefix="TypeScript",
        content_path="src/dev-content.json",
    )
    assert "await readContent()" in server_ts
    assert "hello from meshagent create" in (
        tmp_path / "src" / "dev-content.json"
    ).read_text(encoding="utf-8")
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk == "@meshagent/meshagent"


def test_init_creates_typescript_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "ts",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Agent Toolkit" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert "--meshagent-token full" not in result.output
    assert " -e " not in result.output
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert "cache=.npm-cache" in (tmp_path / ".npmrc").read_text(encoding="utf-8")
    assert not (tmp_path / "src" / "dev-content.json").exists()
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    assert '"dev": "meshagent room connect -- tsx src/server.ts"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-agent:dev --meshagent-token agentDefault --wait"'
        in package_text
    )
    server_ts = (tmp_path / "src" / "server.ts").read_text(encoding="utf-8")
    _assert_node_agent_toolkit(
        server_ts,
        language_label="TypeScript",
        class_prefix="TypeScript",
        proof_path="src/agent-proof.json",
    )
    assert "server.listen" not in server_ts
    _assert_no_dockerfile(tmp_path)
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk == "@meshagent/meshagent"
    assert diagnosis.is_headless_backend_agent is True


def test_init_creates_typescript_chatbot_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "chatbot",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript OpenAI Chatbot" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert "meshagent doctor" not in result.output
    assert "--public" not in result.output
    assert "--liveness" not in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "src" / "server.ts").is_file()
    assert not (tmp_path / "src" / "dev-content.json").exists()
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    assert '"name": "meshagent-create-typescript-chatbot"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-chatbot:dev --public --liveness /health --meshagent-token agentDefault --wait"'
        in package_text
    )
    assert "@meshagent/meshagent" not in package_text
    server_ts = (tmp_path / "src" / "server.ts").read_text(encoding="utf-8")
    assert 'const http = require("node:http")' in server_ts
    assert "http.createServer" in server_ts
    assert "OPENAI_BASE_URL" in server_ts
    assert "OPENAI_API_KEY" in server_ts
    assert "/responses" in server_ts
    assert "/chat/completions" not in server_ts
    assert "/api/chat" in server_ts
    assert "messages" in server_ts
    assert "completeChat" in server_ts
    assert "extractResponseText" in server_ts
    assert "RoomClient" not in server_ts
    assert "startHostedToolkit" not in server_ts
    assert "extends Tool" not in server_ts
    assert "extends Toolkit" not in server_ts
    assert "room.sync" not in server_ts
    assert "room.invoke" not in server_ts
    assert "room.storage" not in server_ts
    assert "chatbot-proof.json" not in server_ts
    assert "chatbot-storage-proof.json" not in server_ts
    assert "server.listen" in server_ts
    _assert_no_dockerfile(tmp_path)
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk is None
    assert diagnosis.has_health_route is True
    assert diagnosis.has_http_port_hint is True
    assert diagnosis.is_headless_backend_agent is False


def test_init_creates_typescript_anthropic_chatbot_non_interactively(
    tmp_path,
) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "chatbot-anthropic",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Anthropic Chatbot" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert "meshagent doctor" not in result.output
    assert "--public" not in result.output
    assert "--liveness" not in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "src" / "server.ts").is_file()
    assert not (tmp_path / "src" / "dev-content.json").exists()
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    assert '"name": "meshagent-create-typescript-chatbot-anthropic"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-chatbot-anthropic:dev --public --liveness /health --meshagent-token agentDefault --wait"'
        in package_text
    )
    assert "@meshagent/meshagent" not in package_text
    server_ts = (tmp_path / "src" / "server.ts").read_text(encoding="utf-8")
    assert 'const http = require("node:http")' in server_ts
    assert "http.createServer" in server_ts
    assert "ANTHROPIC_BASE_URL" in server_ts
    assert "ANTHROPIC_API_KEY" in server_ts
    assert "ANTHROPIC_MODEL" in server_ts
    assert "anthropic-version" in server_ts
    assert "/v1/messages" in server_ts
    assert "/responses" not in server_ts
    assert "/chat/completions" not in server_ts
    assert "OPENAI_BASE_URL" not in server_ts
    assert "OPENAI_API_KEY" not in server_ts
    assert "/api/chat" in server_ts
    assert "messages" in server_ts
    assert "completeChat" in server_ts
    assert "anthropicSystem" in server_ts
    assert "anthropicMessages" in server_ts
    assert "extractMessageText" in server_ts
    assert "RoomClient" not in server_ts
    assert "startHostedToolkit" not in server_ts
    assert "extends Tool" not in server_ts
    assert "extends Toolkit" not in server_ts
    assert "room.sync" not in server_ts
    assert "room.invoke" not in server_ts
    assert "room.storage" not in server_ts
    assert "chatbot-proof.json" not in server_ts
    assert "chatbot-storage-proof.json" not in server_ts
    assert "server.listen" in server_ts
    _assert_no_dockerfile(tmp_path)
    _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node index.js",
    )
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk is None
    assert diagnosis.has_health_route is True
    assert diagnosis.has_http_port_hint is True
    assert diagnosis.is_headless_backend_agent is False


def test_init_rejects_non_typescript_chatbot(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "javascript",
            "--focus",
            "chatbot",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported template combination" in result.output
    assert "JavaScript does not support chatbot" in result.output
    assert not (tmp_path / "package.json").exists()


def test_init_rejects_non_typescript_anthropic_chatbot(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "javascript",
            "--focus",
            "chatbot-anthropic",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported template combination" in result.output
    assert "JavaScript does not support chatbot-anthropic" in result.output
    assert not (tmp_path / "package.json").exists()


def test_init_creates_react_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "react",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "Created a minimal deployable React/Vite Web App" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "vite.config.ts").is_file()
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "scripts" / "dev-content-toolkit.js").is_file()
    assert (tmp_path / "src" / "dev-content.json").is_file()
    assert (tmp_path / "src" / "main.tsx").is_file()
    _assert_no_dockerfile(tmp_path)
    package_json = (tmp_path / "package.json").read_text(encoding="utf-8")
    npmrc = (tmp_path / ".npmrc").read_text(encoding="utf-8")
    main_tsx = (tmp_path / "src" / "main.tsx").read_text(encoding="utf-8")
    dev_content = (tmp_path / "src" / "dev-content.json").read_text(encoding="utf-8")
    assert "cache=.npm-cache" in npmrc
    assert "audit=false" in npmrc
    assert '"react"' in package_json
    assert '"vite"' in package_json
    assert (
        '"dev": "meshagent room connect -- sh -c '
        "'node scripts/dev-content-toolkit.js & vite --host 0.0.0.0'\"" in package_json
    )
    assert "@meshagent/meshagent" in package_json
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-react:dev --public --liveness /health --room-mount /:/data:rw --wait"'
        in package_json
    )
    dev_toolkit = (tmp_path / "scripts" / "dev-content-toolkit.js").read_text(
        encoding="utf-8"
    )
    assert "createRequire" in dev_toolkit
    assert "RoomClient" in dev_toolkit
    assert "startHostedToolkit" in dev_toolkit
    assert "public_: true" in dev_toolkit
    assert "MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS" in dev_toolkit
    assert "class ReactContentToolkit extends Toolkit" in dev_toolkit
    assert 'name: "create"' in dev_toolkit
    assert 'name: "update"' in dev_toolkit
    assert 'name: "search"' in dev_toolkit
    assert "room.agents.invokeTool" in dev_toolkit
    assert "meshagent-create-proof" in dev_toolkit
    assert "src/dev-content.json" in dev_toolkit
    assert "MESHAGENT_CREATE_DEV_PROBE" in dev_toolkit
    assert "room.storage.upload" not in dev_toolkit
    assert 'import devContent from "./dev-content.json"' in main_tsx
    assert "content.items[content.activeId]" in main_tsx
    assert "hello from meshagent create" in dev_content
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "React/Vite"
    assert diagnosis.sdk == "@meshagent/meshagent"


def test_init_creates_typescript_chatbot_ui_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "chatbot-ui",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Agent UI" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "next.config.ts").is_file()
    assert (tmp_path / "next-env.d.ts").is_file()
    assert not (tmp_path / "server.ts").exists()
    assert (tmp_path / "app" / "layout.tsx").is_file()
    assert (tmp_path / "app" / "page.tsx").is_file()
    assert (tmp_path / "app" / "globals.css").is_file()
    assert (tmp_path / "app" / "health" / "route.ts").is_file()
    _assert_no_dockerfile(tmp_path)

    package_json = (tmp_path / "package.json").read_text(encoding="utf-8")
    page_tsx = (tmp_path / "app" / "page.tsx").read_text(encoding="utf-8")
    assert '"name": "meshagent-create-typescript-chatbot-ui"' in package_json
    assert '"@msgpack/msgpack"' in package_json
    assert '"next"' in package_json
    assert '"next": "^16.2.6"' in package_json
    assert '"@vercel/ncc"' not in package_json
    assert '"build": "next build"' in package_json
    assert '"start": "node .next/standalone/server.js"' in package_json
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-chatbot-ui:dev --private --validation-mode=cookie --extra-port=assistant:/messages --liveness /health --wait"'
        in package_json
    )
    assert 'from "@msgpack/msgpack"' in page_tsx
    assert "meshagent.agent.thread.start" in page_tsx
    assert "meshagent.agent.turn.start" in page_tsx
    assert "meshagent.agent.thread.status" in page_tsx
    assert "meshagent.agent.turn.ended" in page_tsx
    assert "/messages" in page_tsx
    assert "disabled={!canSend}" in page_tsx
    spec = _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node server.js",
    )
    assert spec.container is not None
    assert spec.container.environment is not None
    env = {entry.name: entry.value for entry in spec.container.environment}
    assert env["HOSTNAME"] == "0.0.0.0"
    assert env["PORT"] == "3000"


def test_init_creates_typescript_room_chat_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "room-chat",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Room Chat" in result.output
    assert "npm run dev" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "next.config.ts").is_file()
    assert (tmp_path / "next-env.d.ts").is_file()
    assert (tmp_path / "dev-server.mjs").is_file()
    assert (tmp_path / "app" / "layout.tsx").is_file()
    assert (tmp_path / "app" / "page.tsx").is_file()
    assert (tmp_path / "app" / "globals.css").is_file()
    assert (tmp_path / "app" / "health" / "route.ts").is_file()
    assert (tmp_path / "Dockerfile").is_file()

    package_json = (tmp_path / "package.json").read_text(encoding="utf-8")
    next_config = (tmp_path / "next.config.ts").read_text(encoding="utf-8")
    page_tsx = (tmp_path / "app" / "page.tsx").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert '"name": "meshagent-create-typescript-room-chat"' in package_json
    assert '"@meshagent/meshagent": "^' in package_json
    assert '"@meshagent/meshagent-node-ts": "^' in package_json
    assert '"next": "^16.2.6"' in package_json
    assert "http-proxy" not in package_json
    assert '"@msgpack/msgpack"' not in package_json
    assert '"dev": "meshagent room connect -- node dev-server.mjs"' in package_json
    assert '"build": "next build"' in package_json
    assert '"start": "node .next/standalone/server.js"' in package_json
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-room-chat:dev --private --validation-mode=cookie --liveness /health --wait"'
        in package_json
    )
    assert 'from "@meshagent/meshagent"' in page_tsx
    assert "RoomClient.withIAP()" in page_tsx
    assert "room.messaging.enable()" in page_tsx
    assert "room.messaging.remoteParticipants" in page_tsx
    assert "room.messaging.sendMessage" in page_tsx
    assert "meshagent.room-chat.message" in page_tsx
    assert "resolveAlias" not in next_config
    assert "meshAgentBrowserEntry" not in next_config
    assert "./.well-known/meshagent/room/connect" in readme
    assert (
        "starts a local raw\n   websocket proxy at `/.well-known/meshagent/room/connect`"
        in readme
    )
    dev_server = (tmp_path / "dev-server.mjs").read_text(encoding="utf-8")
    assert 'from "@meshagent/meshagent-node-ts"' in dev_server
    assert "attachRoomWebSocketProxy(server" in dev_server
    assert "FROM scratch" in dockerfile
    assert "LABEL meshagent.runtime=node" in dockerfile
    assert "COPY --from=build /app/.next/standalone /app" in dockerfile
    assert "COPY --from=build /app/.next/static /app/.next/static" in dockerfile
    spec = _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node server.js",
    )
    assert spec.container is not None
    assert spec.container.environment is not None
    env = {entry.name: entry.value for entry in spec.container.environment}
    assert env["HOSTNAME"] == "0.0.0.0"
    assert env["PORT"] == "3000"


def test_init_creates_typescript_meeting_app_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "typescript",
            "--focus",
            "meeting-app",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable TypeScript Meeting App" in result.output
    assert "npm run dev" in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / "app" / "page.tsx").is_file()
    assert (tmp_path / "app" / "globals.css").is_file()
    assert (tmp_path / "dev-server.mjs").is_file()
    assert (tmp_path / "Dockerfile").is_file()

    package_json = (tmp_path / "package.json").read_text(encoding="utf-8")
    page_tsx = (tmp_path / "app" / "page.tsx").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert '"name": "meshagent-create-typescript-meeting-app"' in package_json
    assert '"@meshagent/meshagent": "^' in package_json
    assert '"@meshagent/meshagent-react": "^' in package_json
    assert '"@meshagent/meshagent-tailwind": "^' in package_json
    assert '"@meshagent/meshagent-livekit": "^' in package_json
    assert '"dev": "meshagent room connect -- node dev-server.mjs"' in package_json
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-create-typescript-meeting-app:dev --private --validation-mode=cookie --liveness /health --wait"'
        in package_json
    )
    assert 'from "@meshagent/meshagent"' in page_tsx
    assert "RoomClient.withIAP()" in page_tsx
    assert "nextRoom.messaging.enable()" in page_tsx
    assert "ChatBotView" in page_tsx
    assert "MeetingScope" in page_tsx
    assert "MeetingView" in page_tsx
    assert "FilePreview" in page_tsx
    assert "project" not in page_tsx.lower()
    assert "room switch" not in page_tsx.lower()
    assert "does not include project or room switching UI" in readme
    spec = _assert_runtime_image_mount_deploy_yaml(
        tmp_path,
        runtime="node",
        command="node server.js",
    )
    assert spec.container is not None
    assert spec.container.environment is not None
    env = {entry.name: entry.value for entry in spec.container.environment}
    assert env["HOSTNAME"] == "0.0.0.0"
    assert env["PORT"] == "3000"


def test_init_rejects_react_backend_agent(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "react",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported template combination" in result.output
    assert "React does not support backend-agent" in result.output
    assert "Supported focus: webserver" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_rejects_non_typescript_chatbot_ui(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "react",
            "--focus",
            "chatbot-ui",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported template combination" in result.output
    assert "React does not support chatbot-ui" in result.output
    assert "Supported focus: webserver" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_rejects_non_typescript_room_chat(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "react",
            "--focus",
            "room-chat",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported template combination" in result.output
    assert "React does not support room-chat" in result.output
    assert "Supported focus: webserver" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_creates_dotnet_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable .NET Agent Toolkit" in result.output
    assert "./scripts/dev.sh" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "--meshagent-token full" not in result.output
    assert 'PackageReference Include="Meshagent.Api"' in (
        tmp_path / "MeshAgentHello.csproj"
    ).read_text(encoding="utf-8")
    csproj = (tmp_path / "MeshAgentHello.csproj").read_text(encoding="utf-8")
    program_cs = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    _assert_no_dockerfile(tmp_path)
    assert '<Project Sdk="Microsoft.NET.Sdk">' in csproj
    assert "<OutputType>Exe</OutputType>" in csproj
    assert "RoomClient" in program_cs
    assert "DotNetAgentToolkitHost" in program_cs
    assert '"room.register_toolkit"' in program_cs
    assert "room.tool_call." in program_cs
    assert '"room.tool_call_response"' in program_cs
    assert "room.Agents.InvokeTool" in program_cs
    assert '"meshagent.create.dotnet-agent"' in program_cs
    assert '".NET Local Agent Toolkit"' in program_cs
    assert '"ping"' in program_cs
    assert '"status"' in program_cs
    assert '"echo"' in program_cs
    assert "agent-proof.json" in program_cs
    assert "MESHAGENT_CREATE_DEV_PROBE" in program_cs
    assert "MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS" in program_cs
    assert "MESHAGENT_CREATE_DEV_READY_PATH" not in program_cs
    assert "Storage.Upload" not in program_cs
    assert "MapGet" not in program_cs
    assert 'DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"' in install_sh
    assert 'NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"' in install_sh
    assert "command -v dotnet" in install_sh
    assert "command -v docker" not in install_sh
    assert "docker run" not in install_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" not in install_sh
    assert "The .NET SDK 9.0 is required on the host" in install_sh
    assert "dotnet restore" in install_sh
    assert "meshagent room connect -- dotnet run" in dev_sh
    assert "command -v docker" not in dev_sh
    assert "docker run" not in dev_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" not in dev_sh
    assert "The .NET SDK 9.0 is required on the host" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait'
        in deploy_sh
    )


def test_init_creates_dotnet_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        [
            "--language",
            "dotnet",
            "--focus",
            "webserver",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Created a minimal deployable .NET Web App" in result.output
    assert "--meshagent-token" not in result.output
    csproj = (tmp_path / "MeshAgentHello.csproj").read_text(encoding="utf-8")
    program_cs = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    _assert_no_dockerfile(tmp_path)
    assert '<Project Sdk="Microsoft.NET.Sdk.Web">' in csproj
    assert 'PackageReference Include="Meshagent.Api"' in csproj
    assert 'MapGet("/health"' in program_cs
    assert 'MapGet("/status"' in program_cs
    assert 'MapGet("/api/ping"' in program_cs
    assert "RoomClient" in program_cs
    assert "MESHAGENT_CREATE_DEV_READY_PATH" in program_cs
    assert "Storage.Upload" in program_cs
    assert 'DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"' in install_sh
    assert 'NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"' in install_sh
    assert "command -v dotnet" in install_sh
    assert "command -v docker" not in install_sh
    assert "docker run" not in install_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" not in install_sh
    assert "The .NET SDK 9.0 is required on the host" in install_sh
    assert "dotnet restore" in install_sh
    assert "meshagent room connect -- dotnet run" in dev_sh
    assert "command -v docker" not in dev_sh
    assert "docker run" not in dev_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" not in dev_sh
    assert "The .NET SDK 9.0 is required on the host" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait'
        in deploy_sh
    )


def test_init_creates_flutter_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable Flutter Web App" in result.output
    assert "./scripts/dev.sh" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "./scripts/deploy.sh" in result.output
    assert (tmp_path / "pubspec.yaml").is_file()
    assert (tmp_path / "lib" / "main.dart").is_file()
    assert (tmp_path / "tool" / "dev_room_proof.dart").is_file()
    assert (tmp_path / "web" / "index.html").is_file()
    assert not (tmp_path / "Makefile").exists()
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    _assert_no_dockerfile(tmp_path)
    assert 'PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"' in install_sh
    assert "command -v flutter" in install_sh
    assert "command -v docker" not in install_sh
    assert "docker run" not in install_sh
    assert "ghcr.io/cirruslabs/flutter:stable" not in install_sh
    assert "The Flutter SDK is required on the host" in install_sh
    assert "flutter pub get" in install_sh
    assert "meshagent room connect -- sh -c" in dev_sh
    assert "MESHAGENT_CREATE_DEV_PROBE" in dev_sh
    assert "meshagent room connect -- docker run --rm" not in dev_sh
    assert "docker run" not in dev_sh
    assert "ghcr.io/cirruslabs/flutter:stable" not in dev_sh
    assert "command -v dart" in dev_sh
    assert "The Flutter SDK and Dart SDK are required on the host" in dev_sh
    assert "dart run tool/dev_room_proof.dart" in dev_sh
    assert "flutter run -d web-server" in dev_sh
    dev_probe = (tmp_path / "tool" / "dev_room_proof.dart").read_text(encoding="utf-8")
    assert "RoomClient" in dev_probe
    assert "MESHAGENT_CREATE_DEV_READY_PATH" in dev_probe
    assert "room.storage.upload" in dev_probe
    assert "room.dispose()" in dev_probe
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --room-mount /:/data:rw --wait'
        in deploy_sh
    )


def test_init_creates_dart_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
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
    assert "Created a minimal deployable Dart Agent Toolkit" in result.output
    assert "./scripts/dev.sh" in result.output
    assert "MESHAGENT_ROOM=<room>" not in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "--meshagent-token full" not in result.output
    assert "meshagent:" in (tmp_path / "pubspec.yaml").read_text(encoding="utf-8")
    server_dart = (tmp_path / "bin" / "server.dart").read_text(encoding="utf-8")
    assert "RoomClient" in server_dart
    assert "FunctionTool" in server_dart
    assert "class DartAgentToolkit extends Toolkit" in server_dart
    assert "startHostedToolkit" in server_dart
    assert "public: true" in server_dart
    assert "room.agents.invokeTool" in server_dart
    assert "ToolContentInput" in server_dart
    assert "'meshagent.create.dart-agent'" in server_dart
    assert "'Dart Local Agent Toolkit'" in server_dart
    assert "name: 'ping'" in server_dart
    assert "name: 'status'" in server_dart
    assert "name: 'echo'" in server_dart
    assert "agent-proof.json" in server_dart
    assert "MESHAGENT_CREATE_DEV_PROBE" in server_dart
    assert "MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS" in server_dart
    assert "MESHAGENT_CREATE_DEV_READY_PATH" not in server_dart
    assert "room.storage.upload" not in server_dart
    assert "wroteDevProof" not in server_dart
    assert "room.dispose()" in server_dart
    assert "HttpServer" not in server_dart
    _assert_no_dockerfile(tmp_path)
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    assert 'PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"' in install_sh
    assert "command -v dart" in install_sh
    assert "command -v docker" not in install_sh
    assert "docker run" not in install_sh
    assert "dart:stable" not in install_sh
    assert "The Dart SDK is required on the host" in install_sh
    assert "dart pub get" in install_sh
    assert "meshagent room connect -- dart run bin/server.dart" in dev_sh
    assert "meshagent room connect -- docker run --rm" not in dev_sh
    assert "docker run" not in dev_sh
    assert "dart:stable" not in dev_sh
    assert "The Dart SDK is required on the host" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait'
        in deploy_sh
    )


def test_init_rejects_unknown_language(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        ["--language", "rust", "--no-interactive", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Unsupported language: rust" in result.output
    for expected_language in (
        "python",
        "javascript",
        "typescript",
        "react",
        "dotnet",
        "dart-flutter",
    ):
        assert expected_language in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_rejects_unknown_focus(tmp_path) -> None:
    result = CliRunner().invoke(
        create_command,
        ["--focus", "desktop", "--no-interactive", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Unsupported focus: desktop" in result.output
    for expected_focus in (
        "webserver",
        "backend-agent",
        "chatbot",
        "chatbot-anthropic",
        "chatbot-ui",
        "room-chat",
        "meeting-app",
        "contact-form",
    ):
        assert expected_focus in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_launches_tui_when_tty_and_language_or_focus_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)

    captured_languages: list[tuple[str, str, str, tuple[str, ...]]] = []
    captured_focuses: list[tuple[str, str, str]] = []

    def fake_run_create_tui(*, language_choices, focus_choices):
        captured_languages.extend(language_choices)
        captured_focuses.extend(focus_choices)
        return "javascript", "backend-agent"

    monkeypatch.setattr(create_module, "_run_create_tui", fake_run_create_tui)

    result = CliRunner().invoke(create_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert [choice[0] for choice in captured_languages] == [
        "python",
        "javascript",
        "typescript",
        "react",
        "dotnet",
        "dart-flutter",
    ]
    assert [choice[0] for choice in captured_focuses] == [
        "webserver",
        "backend-agent",
        "chatbot",
        "chatbot-anthropic",
        "chatbot-ui",
        "room-chat",
        "meeting-app",
        "contact-form",
    ]
    focus_labels = {choice[0]: choice[1] for choice in captured_focuses}
    assert focus_labels["webserver"] == "Web App"
    assert focus_labels["backend-agent"] == "Agent Toolkit"
    assert focus_labels["chatbot"] == "OpenAI Chatbot"
    assert focus_labels["chatbot-anthropic"] == "Anthropic Chatbot"
    assert focus_labels["chatbot-ui"] == "Agent UI"
    assert focus_labels["room-chat"] == "Room Chat"
    assert focus_labels["meeting-app"] == "Meeting App"
    assert focus_labels["contact-form"] == "Contact Form"
    focus_descriptions = {choice[0]: choice[2] for choice in captured_focuses}
    assert focus_descriptions["webserver"] == (
        "Public HTTP service with a health endpoint."
    )
    assert focus_descriptions["backend-agent"] == (
        "Expose custom functionality to agents in the room."
    )
    assert focus_descriptions["chatbot"] == (
        "Browser chat app backed by the room OpenAI proxy."
    )
    assert focus_descriptions["chatbot-anthropic"] == (
        "Browser chat app backed by the room Anthropic proxy."
    )
    assert "TypeScript" not in focus_descriptions["chatbot"]
    assert "TypeScript" not in focus_descriptions["chatbot-anthropic"]
    assert focus_descriptions["chatbot-ui"] == (
        "Browser chat interface for a deployed MeshAgent agent."
    )
    assert "TypeScript/Next.js" not in focus_descriptions["chatbot-ui"]
    assert focus_descriptions["room-chat"] == (
        "Browser multi-user chat backed by the room messaging API."
    )
    assert focus_descriptions["meeting-app"] == (
        "Browser room app with chat, meetings, and files."
    )
    assert focus_descriptions["contact-form"] == (
        "Public HTML contact form that sends email through a room mailbox."
    )
    assert {choice[0]: choice[3] for choice in captured_languages}["python"] == (
        "webserver",
        "backend-agent",
        "contact-form",
    )
    assert {choice[0]: choice[3] for choice in captured_languages}["react"] == (
        "webserver",
    )
    assert {choice[0]: choice[3] for choice in captured_languages}["typescript"] == (
        "webserver",
        "backend-agent",
        "chatbot",
        "chatbot-anthropic",
        "chatbot-ui",
        "room-chat",
        "meeting-app",
    )
    assert (tmp_path / "server.js").is_file()
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / "server.py").exists()


def test_create_tui_cancel_uses_create_wording(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(create_module, "_run_create_tui", lambda **_: None)

    result = CliRunner().invoke(create_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Create canceled." in result.output
    assert "Init canceled." not in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_create_existing_project_tui_cancel_uses_create_wording(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")
    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        create_module, "_run_existing_project_tui", lambda *, root: None
    )

    result = CliRunner().invoke(create_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert "Create canceled." in result.output
    assert "Init canceled." not in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_tui_language_screen_lists_languages_only(monkeypatch) -> None:
    from meshagent.cli.tui.create import (
        CreateFocusChoice,
        CreateLanguageChoice,
        CreateWizardApp,
    )

    app = CreateWizardApp(
        languages=[
            CreateLanguageChoice(
                id="python",
                label="Python",
                description="Python 3.13 services and agents.",
                focus_ids=("webserver", "backend-agent"),
            ),
            CreateLanguageChoice(
                id="typescript",
                label="TypeScript",
                description="Node.js TypeScript services, agents, and chat apps.",
                focus_ids=("webserver", "backend-agent"),
            ),
        ],
        focuses=[
            CreateFocusChoice(
                id="webserver",
                label="Web App",
                description="Public HTTP service with a health endpoint.",
            ),
            CreateFocusChoice(
                id="backend-agent",
                label="Agent Toolkit",
                description="Expose custom functionality to agents in the room.",
            ),
        ],
    )
    captured_text: dict[str, str] = {}
    captured_options = []

    def fake_set_text(*, title: str, message: str, help_text: str) -> None:
        captured_text["title"] = title
        captured_text["message"] = message
        captured_text["help_text"] = help_text

    def fake_set_options(options) -> None:
        captured_options[:] = list(options)

    monkeypatch.setattr(app, "_set_text", fake_set_text)
    monkeypatch.setattr(app, "_set_options", fake_set_options)

    app._show_language_selection()

    assert captured_text["message"] == "Choose the language for the project."
    assert [str(option.prompt) for option in captured_options] == [
        "Python",
        "TypeScript",
        "Cancel",
    ]
    assert all(" - " not in str(option.prompt) for option in captured_options)
    assert all("RoomClient" not in str(option.prompt) for option in captured_options)


def test_init_tui_focus_screen_asks_for_webserver_or_backend_agent(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.create import (
        CreateFocusChoice,
        CreateLanguageChoice,
        CreateWizardApp,
    )

    app = CreateWizardApp(
        languages=[
            CreateLanguageChoice(
                id="python",
                label="Python",
                description="Python 3.13 services and agents.",
                focus_ids=("webserver", "backend-agent"),
            )
        ],
        focuses=[
            CreateFocusChoice(
                id="webserver",
                label="Web App",
                description="Public HTTP service with a health endpoint.",
            ),
            CreateFocusChoice(
                id="backend-agent",
                label="Agent Toolkit",
                description="Expose custom functionality to agents in the room.",
            ),
        ],
    )
    app._selected_language_id = "python"
    app._selected_language_label = "Python"
    captured_text: dict[str, str] = {}
    captured_options = []

    def fake_set_text(*, title: str, message: str, help_text: str) -> None:
        captured_text["title"] = title
        captured_text["message"] = message
        captured_text["help_text"] = help_text

    def fake_set_options(options) -> None:
        captured_options[:] = list(options)

    monkeypatch.setattr(app, "_set_text", fake_set_text)
    monkeypatch.setattr(app, "_set_options", fake_set_options)

    app._show_focus_selection()

    assert captured_text["message"] == "Choose what you want to build for Python."
    assert "Web App creates an HTTP service" in captured_text["help_text"]
    assert (
        "Agent Toolkit exposes custom functionality to agents in the room"
        in captured_text["help_text"]
    )
    assert [str(option.prompt) for option in captured_options] == [
        "Web App - Public HTTP service with a health endpoint.",
        "Agent Toolkit - Expose custom functionality to agents in the room.",
        "Back",
        "Cancel",
    ]


def test_init_tui_existing_project_screen_offers_subfolder_or_cancel(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.create import (
        CREATE_EXISTING_SUBFOLDER_OPTION_ID,
        CreateExistingProjectApp,
    )

    app = CreateExistingProjectApp()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        app,
        "_set_text",
        lambda *, title, message, help_text: captured.update(
            {
                "title": title,
                "message": message,
                "help_text": help_text,
            }
        ),
    )
    monkeypatch.setattr(app, "_clear_error", lambda: None)
    monkeypatch.setattr(app, "_hide_input", lambda: None)
    monkeypatch.setattr(
        app,
        "_set_options",
        lambda options: captured.update(
            {"options": [(str(option.prompt), option.id) for option in options]}
        ),
    )

    app._show_existing_project_choice()

    assert captured == {
        "title": "MeshAgent Create",
        "message": "This directory already contains project files.",
        "help_text": "Choose an option. Esc or Ctrl+C cancels.",
        "options": [
            (
                "Create a new project in a new subfolder.",
                CREATE_EXISTING_SUBFOLDER_OPTION_ID,
            ),
            ("Cancel", "__init_cancel__"),
        ],
    }


def test_init_tui_existing_project_subfolder_prompt_accepts_folder_name(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.create import (
        EXISTING_ACTION_SUBFOLDER,
        CreateExistingProjectApp,
    )

    app = CreateExistingProjectApp()
    exited = False

    def fake_exit() -> None:
        nonlocal exited
        exited = True

    monkeypatch.setattr(app, "exit", fake_exit)

    submitted = app._submit_subfolder_name("  hello-agent  ")

    assert submitted is True
    assert exited is True
    assert app.result.status == "completed"
    assert app.result.action == EXISTING_ACTION_SUBFOLDER
    assert app.result.subfolder_name == "hello-agent"


def test_init_tui_existing_project_subfolder_prompt_rejects_paths(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.create import CreateExistingProjectApp

    app = CreateExistingProjectApp()
    errors: list[str] = []
    monkeypatch.setattr(app, "_set_error_text", errors.append)

    submitted = app._submit_subfolder_name("../hello-agent")

    assert submitted is False
    assert errors == ["Enter a folder name, not a path."]
    assert app.result.status == "canceled"


def test_init_tui_existing_project_subfolder_prompt_rejects_used_folder(
    tmp_path,
    monkeypatch,
) -> None:
    from meshagent.cli.tui.create import CreateExistingProjectApp

    (tmp_path / "hello-agent").mkdir()
    app = CreateExistingProjectApp(root=tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(app, "_set_error_text", errors.append)

    submitted = app._submit_subfolder_name("hello-agent")

    assert submitted is False
    assert errors == ["Folder is already in use. Enter an empty folder name."]
    assert app.result.status == "canceled"


def test_init_existing_code_interactive_creates_project_in_subfolder(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")

    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        create_module,
        "_run_existing_project_tui",
        lambda *, root: create_module.ExistingProjectSelection(
            action="create-subfolder",
            subfolder_name="hello-agent",
        ),
    )
    monkeypatch.setattr(
        create_module,
        "_run_create_tui",
        lambda *, language_choices, focus_choices: ("typescript", "backend-agent"),
    )

    result = CliRunner().invoke(create_command, [str(tmp_path)])
    project_root = tmp_path / "hello-agent"

    assert result.exit_code == 0
    assert f"New project: {project_root.resolve()}" in result.output
    assert f"  cd {project_root.resolve()}" in result.output
    assert (project_root / "package.json").is_file()
    assert (project_root / "src" / "server.ts").is_file()
    _assert_no_dockerfile(project_root)
    _assert_no_dockerfile(tmp_path)
    assert "@meshagent/meshagent" in (project_root / "package.json").read_text(
        encoding="utf-8"
    )


def test_init_existing_code_interactive_rejects_existing_subfolder(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")
    (tmp_path / "hello-agent").mkdir()

    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        create_module,
        "_run_existing_project_tui",
        lambda *, root: create_module.ExistingProjectSelection(
            action="create-subfolder",
            subfolder_name="hello-agent",
        ),
    )

    result = CliRunner().invoke(create_command, [str(tmp_path)])

    assert result.exit_code == 1
    assert "Subfolder already exists" in result.output
    assert not (tmp_path / "hello-agent" / "Dockerfile").exists()


def test_init_no_interactive_bypasses_tui_even_when_tty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(create_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        create_module,
        "_run_create_tui",
        lambda *, language_choices, focus_choices: (_ for _ in ()).throw(
            AssertionError("TUI should not launch")
        ),
    )

    result = CliRunner().invoke(
        create_command,
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
    result = CliRunner().invoke(create_command, ["--interactive", str(tmp_path)])

    assert result.exit_code == 1
    assert "Interactive mode requires a TTY" in result.output
    assert not (tmp_path / "Dockerfile").exists()


def test_init_recommends_doctor_for_existing_code(tmp_path) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")

    result = CliRunner().invoke(
        create_command,
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
    assert "meshagent create" in result.output
    assert "Existing application code" in result.output
    assert "No files were written" in result.output
    assert "meshagent doctor" in result.output
    assert not (tmp_path / "Dockerfile").exists()
