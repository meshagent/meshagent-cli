from __future__ import annotations

from click.testing import CliRunner

from meshagent.cli import init as init_module
from meshagent.cli.doctor import diagnose_project
from meshagent.cli.init import init_command


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
    assert "MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS" in source
    assert f"class {class_prefix}ContentToolkit extends Toolkit" in source
    assert 'name: "create"' in source
    assert 'name: "update"' in source
    assert 'name: "search"' in source
    assert f'title: "{language_label} Local Content Toolkit"' in source
    assert "room.agents.invokeTool" in source
    assert "meshagent-init-proof" in source
    assert content_path in source
    assert "MESHAGENT_INIT_DEV_PROBE" in source
    assert "room.storage.upload" not in source


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
    assert "MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS" in source
    assert f"class {class_prefix}AgentToolkit extends Toolkit" in source
    assert 'name: "ping"' in source
    assert 'name: "status"' in source
    assert 'name: "echo"' in source
    assert f'title: "{language_label} Local Agent Toolkit"' in source
    assert "room.agents.invokeTool" in source
    assert proof_path in source
    assert "MESHAGENT_INIT_DEV_PROBE" in source
    assert "ContentToolkit" not in source
    assert "dev-content.json" not in source


def test_init_creates_python_backend_agent_by_default_in_non_tty(tmp_path) -> None:
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")

    result = CliRunner().invoke(init_command, [str(tmp_path)])
    diagnosis = diagnose_project(tmp_path)

    assert result.exit_code == 0
    assert "meshagent init" in result.output
    assert "Created a minimal deployable Python backend agent" in result.output
    assert "meshagent doctor" in result.output
    assert "MESHAGENT_ROOM=<room> ./scripts/dev.sh" in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "--meshagent-token full" not in result.output
    assert " -e " not in result.output
    assert "--liveness" not in result.output
    assert "--public" not in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()

    assert 'requires-python = ">=3.13"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert '"meshagent-tools==' in pyproject
    assert "aiohttp" not in pyproject
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
    assert "COPY . ." in dockerfile
    assert "RUN python -m pip install --no-cache-dir --target /out ." in dockerfile
    assert "python-sdk-slim" in dockerfile
    assert "FROM scratch" in dockerfile
    assert "LABEL meshagent.runtime=python" in dockerfile
    assert 'CMD ["-m", "server"]' in dockerfile
    assert "EXPOSE" not in dockerfile
    assert 'PYTHON="${PYTHON:-python3.13}"' in install_sh
    assert 'VENV="${VENV:-.venv}"' in install_sh
    assert 'PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"' in install_sh
    assert 'meshagent room connect -- "$VENV_PYTHON" server.py' in dev_sh
    assert "./scripts/install.sh" not in dev_sh
    assert "MESHAGENT_INIT_DEV_PROBE" in server_py
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


def test_init_renders_meshagent_image_prefix_from_environment(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESHAGENT_IMAGE_PREFIX", "registry.example.com/custom")

    result = CliRunner().invoke(
        init_command,
        [
            "--language",
            "python",
            "--focus",
            "backend-agent",
            "--no-interactive",
            str(tmp_path),
        ],
    )

    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")

    assert result.exit_code == 0
    assert "ARG MESHAGENT_IMAGE_PREFIX=registry.example.com/custom/" in dockerfile


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
    assert "--meshagent-token" not in result.output

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    server_py = (tmp_path / "server.py").read_text(encoding="utf-8")
    dev_content = (tmp_path / "dev-content.json").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()

    assert '"aiohttp[speedups]~=3.13.0"' in pyproject
    assert '"meshagent-api==' in pyproject
    assert '"meshagent-tools==' in pyproject
    assert "from aiohttp import web" in server_py
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
    assert "MESHAGENT_INIT_DEV_PROBE" in server_py
    assert "room.agents.invoke_tool" in server_py
    assert "room.storage.upload" not in server_py
    assert "hello from meshagent init" in dev_content
    assert "python-sdk-slim" in dockerfile
    assert "FROM scratch" in dockerfile
    assert "LABEL meshagent.runtime=python" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert 'PYTHON="${PYTHON:-python3.13}"' in install_sh
    assert 'VENV="${VENV:-.venv}"' in install_sh
    assert 'PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}"' in install_sh
    assert 'meshagent room connect -- "$VENV_PYTHON" server.py' in dev_sh
    assert "./scripts/install.sh" not in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait'
        in deploy_sh
    )
    assert diagnosis.sdk == "meshagent-api"
    assert diagnosis.python_has_pyproject is True
    assert diagnosis.python_source_uses_sdk is True


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
    assert "MESHAGENT_ROOM=<room> npm run dev" in result.output
    assert "npm run deploy" in result.output
    package_json = tmp_path / "package.json"
    npmrc = tmp_path / ".npmrc"
    server_js = tmp_path / "server.js"
    dev_content_json = tmp_path / "dev-content.json"
    dockerfile = tmp_path / "Dockerfile"
    assert package_json.is_file()
    assert npmrc.is_file()
    assert server_js.is_file()
    assert dev_content_json.is_file()
    assert dockerfile.is_file()
    package_text = package_json.read_text(encoding="utf-8")
    npmrc_text = npmrc.read_text(encoding="utf-8")
    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    dev_content = dev_content_json.read_text(encoding="utf-8")
    assert "cache=.npm-cache" in npmrc_text
    assert "audit=false" in npmrc_text
    assert '"build": "ncc build server.js -o dist"' in package_text
    assert '"dev": "meshagent room connect -- node server.js"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-init-javascript:dev --public --liveness /health --wait"'
        in package_text
    )
    assert '"start": "node dist/index.js"' in package_text
    assert '"@vercel/ncc"' in package_text
    assert "@meshagent/meshagent" in package_text
    server_text = server_js.read_text(encoding="utf-8")
    assert "hello from meshagent init" in server_text
    assert 'request.url === "/status"' in server_text
    assert 'request.url === "/api/ping"' in server_text
    _assert_node_content_toolkit(
        server_text,
        language_label="JavaScript",
        class_prefix="JavaScript",
        content_path="dev-content.json",
    )
    assert "readContentSync" in server_text
    assert "hello from meshagent init" in dev_content
    assert "node-sdk" in dockerfile_text
    assert "RUN npm run build" in dockerfile_text
    assert "COPY --from=build /app/dist/index.js /app/index.js" in dockerfile_text
    assert "FROM scratch" in dockerfile_text
    assert "LABEL meshagent.runtime=node" in dockerfile_text
    assert "EXPOSE 3000" in dockerfile_text


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
    assert "MESHAGENT_ROOM=<room> npm run dev" in result.output
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
        '"deploy": "meshagent deploy . --tag meshagent-init-javascript-agent:dev --meshagent-token agentDefault --wait"'
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
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "node-sdk" in dockerfile
    assert "RUN npm install" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=build /app/dist/index.js /app/index.js" in dockerfile
    assert "FROM scratch" in dockerfile
    assert "LABEL meshagent.runtime=node" in dockerfile
    assert "EXPOSE" not in dockerfile


def test_init_creates_typescript_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
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
    assert "Created a minimal deployable TypeScript web server" in result.output
    assert "MESHAGENT_ROOM=<room> npm run dev" in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "src" / "server.ts").is_file()
    assert (tmp_path / "src" / "dev-content.json").is_file()
    assert (tmp_path / "Dockerfile").is_file()
    package_text = (tmp_path / "package.json").read_text(encoding="utf-8")
    npmrc_text = (tmp_path / ".npmrc").read_text(encoding="utf-8")
    dockerfile_text = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "cache=.npm-cache" in npmrc_text
    assert "audit=false" in npmrc_text
    assert '"build": "ncc build src/server.ts -o dist"' in package_text
    assert '"dev": "meshagent room connect -- tsx src/server.ts"' in package_text
    assert (
        '"deploy": "meshagent deploy . --tag meshagent-init-typescript:dev --public --liveness /health --wait"'
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
    assert "readContentSync" in server_ts
    assert "hello from meshagent init" in (
        tmp_path / "src" / "dev-content.json"
    ).read_text(encoding="utf-8")
    assert "node-sdk" in dockerfile_text
    assert "RUN npm run build" in dockerfile_text
    assert "COPY --from=build /app/dist/index.js /app/index.js" in dockerfile_text
    assert "FROM scratch" in dockerfile_text
    assert "LABEL meshagent.runtime=node" in dockerfile_text
    assert "EXPOSE 3000" in dockerfile_text
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk == "@meshagent/meshagent"


def test_init_creates_typescript_backend_agent_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
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
    assert "Created a minimal deployable TypeScript backend agent" in result.output
    assert "MESHAGENT_ROOM=<room> npm run dev" in result.output
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
        '"deploy": "meshagent deploy . --tag meshagent-init-typescript-agent:dev --meshagent-token agentDefault --wait"'
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
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "node-sdk" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=build /app/dist/index.js /app/index.js" in dockerfile
    assert "FROM scratch" in dockerfile
    assert "LABEL meshagent.runtime=node" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "Node.js/TypeScript"
    assert diagnosis.sdk == "@meshagent/meshagent"
    assert diagnosis.is_headless_backend_agent is True


def test_init_creates_react_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
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
    assert "Created a minimal deployable React web server" in result.output
    assert "MESHAGENT_ROOM=<room> npm run dev" in result.output
    assert "npm run deploy" in result.output
    assert (tmp_path / "package.json").is_file()
    assert (tmp_path / ".npmrc").is_file()
    assert (tmp_path / "tsconfig.json").is_file()
    assert (tmp_path / "vite.config.ts").is_file()
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "scripts" / "dev-content-toolkit.js").is_file()
    assert (tmp_path / "src" / "dev-content.json").is_file()
    assert (tmp_path / "src" / "main.tsx").is_file()
    package_json = (tmp_path / "package.json").read_text(encoding="utf-8")
    npmrc = (tmp_path / ".npmrc").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
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
        '"deploy": "meshagent deploy . --tag meshagent-init-react:dev --public --liveness /health --room-mount /:/data:rw --wait"'
        in package_json
    )
    dev_toolkit = (tmp_path / "scripts" / "dev-content-toolkit.js").read_text(
        encoding="utf-8"
    )
    assert "createRequire" in dev_toolkit
    assert "RoomClient" in dev_toolkit
    assert "startHostedToolkit" in dev_toolkit
    assert "public_: true" in dev_toolkit
    assert "MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS" in dev_toolkit
    assert "class ReactContentToolkit extends Toolkit" in dev_toolkit
    assert 'name: "create"' in dev_toolkit
    assert 'name: "update"' in dev_toolkit
    assert 'name: "search"' in dev_toolkit
    assert "room.agents.invokeTool" in dev_toolkit
    assert "meshagent-init-proof" in dev_toolkit
    assert "src/dev-content.json" in dev_toolkit
    assert "MESHAGENT_INIT_DEV_PROBE" in dev_toolkit
    assert "room.storage.upload" not in dev_toolkit
    assert 'import devContent from "./dev-content.json"' in main_tsx
    assert "content.items[content.activeId]" in main_tsx
    assert "hello from meshagent init" in dev_content
    assert "nginx:1.27-alpine" in dockerfile
    assert "listen 80" in dockerfile
    assert "location = /status" in dockerfile
    assert "location = /api/ping" in dockerfile
    assert "EXPOSE 80" in dockerfile
    assert diagnosis.language == "TypeScript"
    assert diagnosis.javascript_flavor == "React/Vite"
    assert diagnosis.sdk == "@meshagent/meshagent"


def test_init_rejects_react_backend_agent(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
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
    assert "MESHAGENT_ROOM=<room> ./scripts/dev.sh" in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "--meshagent-token full" not in result.output
    assert 'PackageReference Include="Meshagent.Api"' in (
        tmp_path / "MeshAgentHello.csproj"
    ).read_text(encoding="utf-8")
    csproj = (tmp_path / "MeshAgentHello.csproj").read_text(encoding="utf-8")
    program_cs = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    assert '<Project Sdk="Microsoft.NET.Sdk">' in csproj
    assert "<OutputType>Exe</OutputType>" in csproj
    assert "RoomClient" in program_cs
    assert "MESHAGENT_INIT_DEV_READY_PATH" in program_cs
    assert "Storage.Upload" in program_cs
    assert "MapGet" not in program_cs
    assert "mcr.microsoft.com/dotnet/runtime:9.0" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert 'DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"' in install_sh
    assert 'NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"' in install_sh
    assert "command -v dotnet" in install_sh
    assert "command -v docker" in install_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" in install_sh
    assert "dotnet restore" in install_sh
    assert "meshagent room connect -- dotnet run" in dev_sh
    assert "meshagent room connect -- docker run --rm" in dev_sh
    assert "-e MESHAGENT_API_URL" in dev_sh
    assert "-e MESHAGENT_PROJECT_ID" in dev_sh
    assert "-e MESHAGENT_ROOM" in dev_sh
    assert "-e MESHAGENT_TOKEN" in dev_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait'
        in deploy_sh
    )


def test_init_creates_dotnet_webserver_non_interactively(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
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
    assert "Created a minimal deployable .NET web server" in result.output
    assert "--meshagent-token" not in result.output
    csproj = (tmp_path / "MeshAgentHello.csproj").read_text(encoding="utf-8")
    program_cs = (tmp_path / "Program.cs").read_text(encoding="utf-8")
    dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    assert '<Project Sdk="Microsoft.NET.Sdk.Web">' in csproj
    assert 'PackageReference Include="Meshagent.Api"' in csproj
    assert 'MapGet("/health"' in program_cs
    assert 'MapGet("/status"' in program_cs
    assert 'MapGet("/api/ping"' in program_cs
    assert "RoomClient" in program_cs
    assert "MESHAGENT_INIT_DEV_READY_PATH" in program_cs
    assert "Storage.Upload" in program_cs
    assert "mcr.microsoft.com/dotnet/aspnet:9.0" in dockerfile
    assert "EXPOSE 5000" in dockerfile
    assert 'DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"' in install_sh
    assert 'NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"' in install_sh
    assert "command -v dotnet" in install_sh
    assert "command -v docker" in install_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" in install_sh
    assert "dotnet restore" in install_sh
    assert "meshagent room connect -- dotnet run" in dev_sh
    assert "meshagent room connect -- docker run --rm" in dev_sh
    assert "-e MESHAGENT_API_URL" in dev_sh
    assert "-e MESHAGENT_PROJECT_ID" in dev_sh
    assert "-e MESHAGENT_ROOM" in dev_sh
    assert "-e MESHAGENT_TOKEN" in dev_sh
    assert "mcr.microsoft.com/dotnet/sdk:9.0" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait'
        in deploy_sh
    )


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
    assert "MESHAGENT_ROOM=<room> ./scripts/dev.sh" in result.output
    assert "./scripts/deploy.sh" in result.output
    assert (tmp_path / "pubspec.yaml").is_file()
    assert (tmp_path / "lib" / "main.dart").is_file()
    assert (tmp_path / "tool" / "dev_room_proof.dart").is_file()
    assert (tmp_path / "web" / "index.html").is_file()
    assert not (tmp_path / "Makefile").exists()
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "flutter build web --release" in (tmp_path / "Dockerfile").read_text(
        encoding="utf-8"
    )
    flutter_dockerfile = (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "listen 80" in flutter_dockerfile
    assert "location = /status" in flutter_dockerfile
    assert "location = /api/ping" in flutter_dockerfile
    assert "EXPOSE 80" in flutter_dockerfile
    assert 'PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"' in install_sh
    assert "command -v flutter" in install_sh
    assert "command -v docker" in install_sh
    assert "ghcr.io/cirruslabs/flutter:stable" in install_sh
    assert "flutter pub get" in install_sh
    assert "meshagent room connect -- sh -c" in dev_sh
    assert "MESHAGENT_INIT_DEV_PROBE" in dev_sh
    assert "meshagent room connect -- docker run --rm" in dev_sh
    assert "-p 3000:3000" in dev_sh
    assert "-e MESHAGENT_API_URL" in dev_sh
    assert "-e MESHAGENT_PROJECT_ID" in dev_sh
    assert "-e MESHAGENT_ROOM" in dev_sh
    assert "-e MESHAGENT_TOKEN" in dev_sh
    assert "ghcr.io/cirruslabs/flutter:stable" in dev_sh
    assert "dart run tool/dev_room_proof.dart" in dev_sh
    assert "flutter run -d web-server" in dev_sh
    dev_probe = (tmp_path / "tool" / "dev_room_proof.dart").read_text(encoding="utf-8")
    assert "RoomClient" in dev_probe
    assert "MESHAGENT_INIT_DEV_READY_PATH" in dev_probe
    assert "room.storage.upload" in dev_probe
    assert "room.dispose()" in dev_probe
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --room-mount /:/data:rw --wait'
        in deploy_sh
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
    assert "MESHAGENT_ROOM=<room> ./scripts/dev.sh" in result.output
    assert "./scripts/deploy.sh" in result.output
    assert "--meshagent-token full" not in result.output
    assert "meshagent:" in (tmp_path / "pubspec.yaml").read_text(encoding="utf-8")
    assert "RoomClient" in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "MESHAGENT_INIT_DEV_READY_PATH" in (
        tmp_path / "bin" / "server.dart"
    ).read_text(encoding="utf-8")
    assert "room.storage.upload" in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "wroteDevProof" in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "room.dispose()" in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "HttpServer" not in (tmp_path / "bin" / "server.dart").read_text(
        encoding="utf-8"
    )
    assert "FROM dart:stable" in (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE" not in (tmp_path / "Dockerfile").read_text(encoding="utf-8")
    install_sh = (tmp_path / "scripts" / "install.sh").read_text(encoding="utf-8")
    dev_sh = (tmp_path / "scripts" / "dev.sh").read_text(encoding="utf-8")
    deploy_sh = (tmp_path / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert not (tmp_path / "Makefile").exists()
    assert 'PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"' in install_sh
    assert "command -v dart" in install_sh
    assert "command -v docker" in install_sh
    assert "dart:stable" in install_sh
    assert "dart pub get" in install_sh
    assert "meshagent room connect -- dart run bin/server.dart" in dev_sh
    assert "meshagent room connect -- docker run --rm" in dev_sh
    assert "-e MESHAGENT_API_URL" in dev_sh
    assert "-e MESHAGENT_PROJECT_ID" in dev_sh
    assert "-e MESHAGENT_ROOM" in dev_sh
    assert "-e MESHAGENT_TOKEN" in dev_sh
    assert "dart:stable" in dev_sh
    assert (
        'meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait'
        in deploy_sh
    )


def test_init_rejects_unknown_language(tmp_path) -> None:
    result = CliRunner().invoke(
        init_command,
        ["--language", "rust", "--no-interactive", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Unsupported language: rust" in result.output
    assert (
        "python, javascript, typescript, react, dotnet, dart-flutter" in result.output
    )
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

    captured_languages: list[tuple[str, str, str, tuple[str, ...]]] = []
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
        "typescript",
        "react",
        "dotnet",
        "dart-flutter",
    ]
    assert [choice[0] for choice in captured_focuses] == [
        "webserver",
        "backend-agent",
    ]
    assert {choice[0]: choice[3] for choice in captured_languages}["react"] == (
        "webserver",
    )
    assert {choice[0]: choice[3] for choice in captured_languages}["typescript"] == (
        "webserver",
        "backend-agent",
    )
    assert (tmp_path / "server.js").is_file()
    assert "@meshagent/meshagent" in (tmp_path / "package.json").read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / "server.py").exists()


def test_init_tui_language_screen_lists_languages_only(monkeypatch) -> None:
    from meshagent.cli.tui.init import (
        InitFocusChoice,
        InitLanguageChoice,
        InitWizardApp,
    )

    app = InitWizardApp(
        languages=[
            InitLanguageChoice(
                id="python",
                label="Python",
                description="Python 3.13 service or RoomClient backend.",
                focus_ids=("webserver", "backend-agent"),
            ),
            InitLanguageChoice(
                id="typescript",
                label="TypeScript",
                description="Node.js service or RoomClient backend in TypeScript.",
                focus_ids=("webserver", "backend-agent"),
            ),
        ],
        focuses=[
            InitFocusChoice(
                id="webserver",
                label="Web server",
                description="HTTP app with a health endpoint.",
            ),
            InitFocusChoice(
                id="backend-agent",
                label="Backend agent",
                description="RoomClient SDK service.",
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
    from meshagent.cli.tui.init import (
        InitFocusChoice,
        InitLanguageChoice,
        InitWizardApp,
    )

    app = InitWizardApp(
        languages=[
            InitLanguageChoice(
                id="python",
                label="Python",
                description="Python 3.13.",
                focus_ids=("webserver", "backend-agent"),
            )
        ],
        focuses=[
            InitFocusChoice(
                id="webserver",
                label="Web server",
                description="HTTP app with a health endpoint and public route.",
            ),
            InitFocusChoice(
                id="backend-agent",
                label="Backend agent",
                description="Headless RoomClient SDK service without a public port.",
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
    assert "Web server creates an HTTP app" in captured_text["help_text"]
    assert (
        "Backend agent creates a RoomClient SDK service" in captured_text["help_text"]
    )
    assert [str(option.prompt) for option in captured_options] == [
        "Web server - HTTP app with a health endpoint and public route.",
        "Backend agent - Headless RoomClient SDK service without a public port.",
        "Back",
        "Cancel",
    ]


def test_init_tui_existing_project_screen_offers_doctor_or_subfolder(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.init import (
        INIT_EXISTING_DOCTOR_OPTION_ID,
        INIT_EXISTING_SUBFOLDER_OPTION_ID,
        InitExistingProjectApp,
    )

    app = InitExistingProjectApp()
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
        "title": "MeshAgent Init",
        "message": "This directory already contains project files.",
        "help_text": "Choose an option. Esc or Ctrl+C cancels.",
        "options": [
            ("Run meshagent doctor here.", INIT_EXISTING_DOCTOR_OPTION_ID),
            (
                "Create a new project in a new subfolder.",
                INIT_EXISTING_SUBFOLDER_OPTION_ID,
            ),
            ("Cancel", "__init_cancel__"),
        ],
    }


def test_init_tui_existing_project_subfolder_prompt_accepts_folder_name(
    monkeypatch,
) -> None:
    from meshagent.cli.tui.init import (
        EXISTING_ACTION_SUBFOLDER,
        InitExistingProjectApp,
    )

    app = InitExistingProjectApp()
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
    from meshagent.cli.tui.init import InitExistingProjectApp

    app = InitExistingProjectApp()
    errors: list[str] = []
    monkeypatch.setattr(app, "_set_error_text", errors.append)

    submitted = app._submit_subfolder_name("../hello-agent")

    assert submitted is False
    assert errors == ["Enter a folder name, not a path."]
    assert app.result.status == "canceled"


def test_init_existing_code_interactive_can_run_doctor_here(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")
    doctor_paths: list[object] = []

    monkeypatch.setattr(init_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        init_module,
        "_run_existing_project_tui",
        lambda: init_module.ExistingProjectSelection(action="run-doctor"),
    )
    monkeypatch.setattr(init_module, "_run_doctor", doctor_paths.append)
    monkeypatch.setattr(
        init_module,
        "_run_init_tui",
        lambda *, language_choices, focus_choices: (_ for _ in ()).throw(
            AssertionError("language/focus TUI should not launch")
        ),
    )

    result = CliRunner().invoke(init_command, [str(tmp_path)])

    assert result.exit_code == 0
    assert doctor_paths == [tmp_path.resolve()]
    assert not (tmp_path / "Dockerfile").exists()


def test_init_existing_code_interactive_creates_project_in_subfolder(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")

    monkeypatch.setattr(init_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        init_module,
        "_run_existing_project_tui",
        lambda: init_module.ExistingProjectSelection(
            action="create-subfolder",
            subfolder_name="hello-agent",
        ),
    )
    monkeypatch.setattr(
        init_module,
        "_run_init_tui",
        lambda *, language_choices, focus_choices: ("typescript", "backend-agent"),
    )

    result = CliRunner().invoke(init_command, [str(tmp_path)])
    project_root = tmp_path / "hello-agent"

    assert result.exit_code == 0
    assert f"New project: {project_root.resolve()}" in result.output
    assert (project_root / "package.json").is_file()
    assert (project_root / "src" / "server.ts").is_file()
    assert (project_root / "Dockerfile").is_file()
    assert not (tmp_path / "Dockerfile").exists()
    assert "@meshagent/meshagent" in (project_root / "package.json").read_text(
        encoding="utf-8"
    )


def test_init_existing_code_interactive_rejects_existing_subfolder(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "server.py").write_text("print('already here')\n", encoding="utf-8")
    (tmp_path / "hello-agent").mkdir()

    monkeypatch.setattr(init_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        init_module,
        "_run_existing_project_tui",
        lambda: init_module.ExistingProjectSelection(
            action="create-subfolder",
            subfolder_name="hello-agent",
        ),
    )

    result = CliRunner().invoke(init_command, [str(tmp_path)])

    assert result.exit_code == 1
    assert "Subfolder already exists" in result.output
    assert not (tmp_path / "hello-agent" / "Dockerfile").exists()


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
