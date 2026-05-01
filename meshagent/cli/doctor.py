from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import textwrap
import tomllib
from typing import Iterable

import click


SOURCE_SUFFIXES = {
    ".cs",
    ".dart",
    ".go",
    ".js",
    ".jsx",
    ".py",
    ".rb",
    ".ts",
    ".tsx",
}
PYTHON_REQUIRED_VERSION = "3.13"
PYTHON_REQUIRED_MAJOR_MINOR = (3, 13)
PYTHON_VIRTUAL_ENV_DIR_NAMES = {
    ".env",
    ".venv",
    ".virtualenv",
    "env",
    "venv",
    "virtualenv",
}
PYTHON_VIRTUAL_ENV_SCAN_IGNORES = {
    ".git",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
}
DEPLOY_ENV_SANITIZER = (
    "env -u MESHAGENT_TOKEN -u MESHAGENT_PARTICIPANT_ID "
    "-u MESHAGENT_PARTICIPANT_NAME -u MESHAGENT_ROOM_URL "
    "-u MESHAGENT_SESSION_ID"
)


@dataclass(frozen=True)
class ProjectDiagnosis:
    root: Path
    language: str
    javascript_flavor: str | None
    sdk: str | None
    has_deployment_artifact: bool
    deployment_artifacts: tuple[str, ...]
    has_health_route: bool
    has_port_8080_hint: bool
    package_scripts: tuple[tuple[str, str], ...]
    python_runtime_findings: tuple[str, ...]
    python_virtualenv_versions: tuple[tuple[str, str], ...]
    liveness_path: str
    start_command: str
    dockerfile: str


def _iter_files(root: Path) -> Iterable[Path]:
    ignored_dirs = {
        ".git",
        ".venv",
        "__pycache__",
        "bin",
        "build",
        "dist",
        "node_modules",
        "obj",
        "target",
    }
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in ignored_dirs for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_toml(path: Path) -> dict[str, object]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _python_major_minor(value: str) -> tuple[int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)", value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _python_runtime_is_older(value: str, *, constraint: bool = False) -> bool:
    normalized = value.strip().lower()
    if normalized == "":
        return False
    if constraint:
        if re.search(r"<\s*3\.13(?:\D|$)", normalized):
            return True
        if re.search(r"<=\s*3\.12(?:\D|$)", normalized):
            return True
        if re.search(r"==\s*3\.(?:[0-9]|1[0-2])(?:\D|$)", normalized):
            return True
        return False
    parsed = _python_major_minor(normalized)
    return parsed is not None and parsed < PYTHON_REQUIRED_MAJOR_MINOR


def _python_version_is_required(value: str) -> bool:
    parsed = _python_major_minor(value)
    return parsed == PYTHON_REQUIRED_MAJOR_MINOR


def _is_python_virtualenv_dir(path: Path) -> bool:
    return path.is_dir() and (
        path.name in PYTHON_VIRTUAL_ENV_DIR_NAMES or (path / "pyvenv.cfg").is_file()
    )


def _python_virtualenv_dirs(root: Path) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved not in seen and _is_python_virtualenv_dir(path):
            candidates.append(path)
            seen.add(resolved)

    add_candidate(root)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return candidates

    parents = [root]
    parents.extend(
        path
        for path in children
        if path.is_dir() and path.name not in PYTHON_VIRTUAL_ENV_SCAN_IGNORES
    )
    for parent in parents:
        try:
            env_candidates = sorted(parent.iterdir())
        except OSError:
            continue
        for path in env_candidates:
            add_candidate(path)
    return candidates


def _python_virtualenv_version(env_dir: Path) -> str | None:
    pyvenv_cfg = _read_text(env_dir / "pyvenv.cfg")
    for line in pyvenv_cfg.splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip().lower() == "version":
            version = value.strip()
            if version:
                return version

    for scripts_dir in ("bin", "Scripts"):
        python_dir = env_dir / scripts_dir
        try:
            names = {path.name.lower() for path in python_dir.iterdir()}
        except OSError:
            continue
        if any(name.startswith("python3.13") for name in names):
            return PYTHON_REQUIRED_VERSION
    return None


def _python_virtualenv_versions(
    root: Path, language: str
) -> tuple[tuple[str, str], ...]:
    if language != "Python":
        return ()

    versions: list[tuple[str, str]] = []
    for env_dir in _python_virtualenv_dirs(root):
        version = _python_virtualenv_version(env_dir)
        if version is None:
            continue
        try:
            relative_path = env_dir.relative_to(root)
        except ValueError:
            relative_path = env_dir
        versions.append((str(relative_path), version))
    return tuple(versions)


def _python_runtime_findings(root: Path, language: str) -> tuple[str, ...]:
    if language != "Python":
        return ()

    findings: list[str] = []
    for file_name in (".python-version", "runtime.txt"):
        value = _read_text(root / file_name).strip().splitlines()
        if value and _python_runtime_is_older(value[0]):
            findings.append(
                f"{file_name} declares `{value[0]}`; MeshAgent Python apps "
                f"must target Python {PYTHON_REQUIRED_VERSION}."
            )

    pyproject = _read_toml(root / "pyproject.toml")
    project = pyproject.get("project")
    if isinstance(project, dict):
        requires_python = project.get("requires-python")
        if isinstance(requires_python, str) and _python_runtime_is_older(
            requires_python, constraint=True
        ):
            findings.append(
                "`pyproject.toml` project.requires-python is "
                f"`{requires_python}`; update it so Python "
                f"{PYTHON_REQUIRED_VERSION} is allowed."
            )

    tool = pyproject.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    dependencies = poetry.get("dependencies") if isinstance(poetry, dict) else None
    python_constraint = (
        dependencies.get("python") if isinstance(dependencies, dict) else None
    )
    if isinstance(python_constraint, str) and _python_runtime_is_older(
        python_constraint, constraint=True
    ):
        findings.append(
            "`pyproject.toml` tool.poetry.dependencies.python is "
            f"`{python_constraint}`; update it so Python "
            f"{PYTHON_REQUIRED_VERSION} is allowed."
        )

    return tuple(findings)


def _contains_any(paths: Iterable[Path], needles: tuple[str, ...]) -> bool:
    for path in paths:
        haystack = _read_text(path).lower()
        if any(needle in haystack for needle in needles):
            return True
    return False


def _source_files(root: Path) -> list[Path]:
    return [
        path for path in _iter_files(root) if path.suffix.lower() in SOURCE_SUFFIXES
    ]


def _package_json_dependencies(root: Path) -> dict[str, object]:
    package_json = _read_json(root / "package.json")
    dependencies: dict[str, object] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = package_json.get(key)
        if isinstance(value, dict):
            dependencies.update(value)
    return dependencies


def _package_json_scripts(root: Path) -> dict[str, str]:
    scripts = _read_json(root / "package.json").get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(command) for name, command in scripts.items()}


def _javascript_flavor(
    root: Path, language: str, source_files: list[Path]
) -> str | None:
    if language not in {"JavaScript", "TypeScript"}:
        return None

    dependencies = _package_json_dependencies(root)
    scripts = _package_json_scripts(root)
    script_text = " ".join(scripts.values()).lower()
    suffixes = {path.suffix.lower() for path in source_files}

    if "next" in dependencies or "next " in f"{script_text} ":
        return "Next.js"
    if "vite" in dependencies or "vite" in script_text:
        if "react" in dependencies or suffixes & {".jsx", ".tsx"}:
            return "React/Vite"
        return "Vite"
    if "react-scripts" in dependencies or "react-scripts" in script_text:
        return "React"
    if "react" in dependencies or suffixes & {".jsx", ".tsx"}:
        return "React"
    if language == "TypeScript":
        return "Node.js/TypeScript"
    return "Node.js"


def _is_static_javascript_flavor(javascript_flavor: str | None) -> bool:
    return javascript_flavor in {"React", "React/Vite", "Vite"}


def _is_javascript_project(language: str) -> bool:
    return language in {"JavaScript", "TypeScript"}


def _detect_language(root: Path) -> str:
    package_dependencies = _package_json_dependencies(root)
    if (root / "tsconfig.json").is_file() or "typescript" in package_dependencies:
        return "TypeScript"
    if (root / "package.json").is_file():
        return "JavaScript"
    if any(root.glob("*.csproj")):
        return ".NET"
    if (root / "pubspec.yaml").is_file():
        return "Dart"
    if (root / "requirements.txt").is_file() or (root / "pyproject.toml").is_file():
        return "Python"
    if any(root.glob("*.go")) or (root / "go.mod").is_file():
        return "Go"
    if any(root.glob("*.rb")) or (root / "Gemfile").is_file():
        return "Ruby"

    suffixes = {path.suffix.lower() for path in _source_files(root)}
    if ".py" in suffixes:
        return "Python"
    if ".ts" in suffixes or ".tsx" in suffixes:
        return "TypeScript"
    if ".js" in suffixes or ".jsx" in suffixes:
        return "JavaScript"
    if ".dart" in suffixes:
        return "Dart"
    if ".cs" in suffixes:
        return ".NET"
    if ".go" in suffixes:
        return "Go"
    if ".rb" in suffixes:
        return "Ruby"
    return "Unknown"


def _detect_sdk(root: Path, language: str, source_files: list[Path]) -> str | None:
    if language in {"JavaScript", "TypeScript"}:
        if "@meshagent/meshagent" in _package_json_dependencies(root):
            return "@meshagent/meshagent"
    if language == "Python":
        requirements = _read_text(root / "requirements.txt").lower()
        pyproject = _read_toml(root / "pyproject.toml")
        pyproject_text = str(pyproject).lower()
        if "meshagent-api" in requirements or "meshagent-api" in pyproject_text:
            return "meshagent-api"
        if _contains_any(source_files, ("from meshagent", "import meshagent")):
            return "meshagent-api"
    if language == ".NET":
        if _contains_any(_iter_files(root), ("meshagent.api", "meshagent.api")):
            return "Meshagent.Api"
    if language == "Dart":
        if "meshagent:" in _read_text(root / "pubspec.yaml").lower():
            return "meshagent"
        if _contains_any(source_files, ("package:meshagent",)):
            return "meshagent"
    return None


def _deployment_artifacts(root: Path) -> tuple[str, ...]:
    artifacts = []
    for name in ("Dockerfile", "Containerfile", "meshagent.yaml", "meshagent.yml"):
        if (root / name).exists():
            artifacts.append(name)
    return tuple(artifacts)


def _start_command(language: str, javascript_flavor: str | None) -> str:
    if javascript_flavor in {"React", "React/Vite", "Vite"}:
        return "nginx -g 'daemon off;'"
    if javascript_flavor == "Next.js":
        return "npm start -- -H 0.0.0.0 -p 8080"
    return {
        "Python": "python server.py",
        "TypeScript": "npm start",
        "JavaScript": "npm start",
        ".NET": "dotnet DoctorDotnetRoomClient.dll",
        "Dart": "/app/server",
        "Go": "./server",
        "Ruby": "ruby server.rb",
    }.get(language, "<start command>")


def _dockerfile_for(language: str, javascript_flavor: str | None) -> str:
    snippets = {
        "Python": """
            FROM python:3.13-slim
            WORKDIR /app
            COPY requirements.txt .
            RUN pip install --no-cache-dir -r requirements.txt
            COPY server.py .
            EXPOSE 8080
            CMD ["python", "server.py"]
        """,
        "TypeScript": """
            FROM node:22-alpine
            WORKDIR /app
            COPY package*.json tsconfig.json ./
            RUN npm install
            COPY . .
            RUN npm run build && npm prune --omit=dev
            EXPOSE 8080
            CMD ["npm", "start"]
        """,
        "JavaScript": """
            FROM node:22-alpine
            WORKDIR /app
            COPY package*.json ./
            RUN npm install --omit=dev
            COPY . .
            EXPOSE 8080
            CMD ["npm", "start"]
        """,
        ".NET": """
            FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
            WORKDIR /src
            COPY . .
            RUN dotnet publish -c Release -o /app/publish

            FROM mcr.microsoft.com/dotnet/aspnet:9.0
            WORKDIR /app
            COPY --from=build /app/publish .
            EXPOSE 8080
            ENTRYPOINT ["dotnet", "DoctorDotnetRoomClient.dll"]
        """,
        "Dart": """
            FROM dart:stable
            WORKDIR /app
            COPY pubspec.yaml ./
            RUN dart pub get
            COPY bin ./bin
            RUN dart compile exe bin/server.dart -o /app/server
            EXPOSE 8080
            CMD ["/app/server"]
        """,
        "Go": """
            FROM golang:1.24-alpine
            WORKDIR /app
            COPY server.go .
            RUN go build -o server server.go
            EXPOSE 8080
            CMD ["./server"]
        """,
        "Ruby": """
            FROM ruby:3.4-alpine
            WORKDIR /app
            COPY server.rb .
            EXPOSE 8080
            CMD ["ruby", "server.rb"]
        """,
    }

    if javascript_flavor in {"React/Vite", "Vite"}:
        return textwrap.dedent(
            """
            FROM node:22-alpine AS build
            WORKDIR /app
            COPY package*.json ./
            RUN npm install
            COPY . .
            RUN npm run build

            FROM nginx:1.27-alpine
            COPY --from=build /app/dist /usr/share/nginx/html
            RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
              'pid /data/nginx/nginx.pid;' \\
              'events {}' \\
              'http {' \\
              '  include /etc/nginx/mime.types;' \\
              '  client_body_temp_path /data/nginx/client_temp;' \\
              '  proxy_temp_path /data/nginx/proxy_temp;' \\
              '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
              '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
              '  scgi_temp_path /data/nginx/scgi_temp;' \\
              '  server { listen 8080; location = /health { return 200 "ok\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
              '}' > /etc/nginx/nginx.conf
            EXPOSE 8080
            CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
            """
        ).strip()

    if javascript_flavor == "React":
        return textwrap.dedent(
            """
            FROM node:22-alpine AS build
            WORKDIR /app
            COPY package*.json ./
            RUN npm install
            COPY . .
            RUN npm run build

            FROM nginx:1.27-alpine
            COPY --from=build /app/build /usr/share/nginx/html
            RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
              'pid /data/nginx/nginx.pid;' \\
              'events {}' \\
              'http {' \\
              '  include /etc/nginx/mime.types;' \\
              '  client_body_temp_path /data/nginx/client_temp;' \\
              '  proxy_temp_path /data/nginx/proxy_temp;' \\
              '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
              '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
              '  scgi_temp_path /data/nginx/scgi_temp;' \\
              '  server { listen 8080; location = /health { return 200 "ok\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
              '}' > /etc/nginx/nginx.conf
            EXPOSE 8080
            CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
            """
        ).strip()

    if javascript_flavor == "Next.js":
        return textwrap.dedent(
            """
            FROM node:22-alpine
            WORKDIR /app
            ENV HOSTNAME=0.0.0.0
            ENV PORT=8080
            COPY package*.json ./
            RUN npm install
            COPY . .
            RUN npm run build
            EXPOSE 8080
            CMD ["npm", "start", "--", "-H", "0.0.0.0", "-p", "8080"]
            """
        ).strip()

    return textwrap.dedent(snippets.get(language, "")).strip()


def _liveness_path_for(
    language: str, javascript_flavor: str | None, has_health_route: bool
) -> str:
    if has_health_route or javascript_flavor in {"React", "React/Vite", "Vite"}:
        return "/health"
    if _is_javascript_project(language) and javascript_flavor == "Next.js":
        return "/"
    return "/health"


def diagnose_project(root: Path) -> ProjectDiagnosis:
    resolved_root = root.resolve()
    source_files = _source_files(resolved_root)
    language = _detect_language(resolved_root)
    javascript_flavor = _javascript_flavor(resolved_root, language, source_files)
    python_runtime_findings = _python_runtime_findings(resolved_root, language)
    python_virtualenv_versions = _python_virtualenv_versions(resolved_root, language)
    artifacts = _deployment_artifacts(resolved_root)
    has_health_route = _contains_any(
        source_files, ('"/health"', "'/health'", "/health")
    )
    liveness_path = _liveness_path_for(language, javascript_flavor, has_health_route)
    return ProjectDiagnosis(
        root=resolved_root,
        language=language,
        javascript_flavor=javascript_flavor,
        sdk=_detect_sdk(resolved_root, language, source_files),
        has_deployment_artifact=bool(artifacts),
        deployment_artifacts=artifacts,
        has_health_route=has_health_route,
        has_port_8080_hint=_contains_any(source_files, ("8080",)),
        package_scripts=tuple(sorted(_package_json_scripts(resolved_root).items())),
        python_runtime_findings=python_runtime_findings,
        python_virtualenv_versions=python_virtualenv_versions,
        liveness_path=liveness_path,
        start_command=_start_command(language, javascript_flavor),
        dockerfile=_dockerfile_for(language, javascript_flavor),
    )


def _deploy_command(diagnosis: ProjectDiagnosis) -> str:
    parts = [
        f"{DEPLOY_ENV_SANITIZER} meshagent deploy .",
        '--room "$MESHAGENT_ROOM"',
        "--tag <repository>:<tag>",
        "--public",
        "--domain <domain>",
        f"--liveness {diagnosis.liveness_path}",
        "--no-optimize",
        "--wait",
    ]
    if _is_static_javascript_flavor(diagnosis.javascript_flavor):
        parts.insert(-2, "--room-mount /:/data:rw")
    if diagnosis.sdk is not None:
        parts.extend(
            [
                "--meshagent-token full",
                '-e MESHAGENT_API_URL="$MESHAGENT_API_URL"',
                '-e MESHAGENT_ROOM="$MESHAGENT_ROOM"',
                '-e MESHAGENT_ROOM_URL="$MESHAGENT_ROOM_URL"',
            ]
        )
    return " ".join(parts)


def _sdk_guidance(diagnosis: ProjectDiagnosis) -> list[str]:
    if diagnosis.sdk == "@meshagent/meshagent":
        guidance = [
            "The Node RoomClient SDK currently resolves reliably through its "
            'CommonJS entrypoint; use `require("@meshagent/meshagent")` or '
            "compile TypeScript to CommonJS before deploying RoomClient routes."
        ]
        if diagnosis.javascript_flavor == "Node.js/TypeScript":
            guidance.append(
                "For TypeScript RoomClient servers, set `compilerOptions.module` "
                'to `"CommonJS"` and `moduleResolution` to `"Node"`, remove '
                '`"type": "module"` from `package.json` or set it to `"commonjs"`, '
                "and make `npm start` run the built CommonJS entrypoint, for "
                "example `node dist/server.js`."
            )
        if _is_static_javascript_flavor(diagnosis.javascript_flavor):
            guidance.append(
                "Static React/Vite browser bundles cannot hold a room token safely; "
                "keep RoomClient calls in a Node server or API route and have the "
                "frontend call that route."
            )
        return guidance
    if diagnosis.language == ".NET" and diagnosis.sdk == "Meshagent.Api":
        return [
            "RoomClient lives in the Meshagent.Api.Room namespace; add "
            "`using Meshagent.Api.Room;` before deploying if the app references "
            "RoomClient."
        ]
    return []


def _local_build_check(diagnosis: ProjectDiagnosis) -> tuple[str, str] | None:
    if diagnosis.javascript_flavor in {"React", "React/Vite", "Vite", "Next.js"}:
        return ("npm", "npm install && npm run build")
    if diagnosis.javascript_flavor == "Node.js/TypeScript":
        return ("npm", "npm install && npm run build")
    return {
        "Python": ("python3", "python3 -m py_compile server.py"),
        "TypeScript": ("npm", "npm install && npm run build"),
        "JavaScript": ("npm", "npm install --omit=dev"),
        ".NET": (
            "dotnet",
            "dotnet publish -c Release -o /tmp/meshagent-doctor-publish",
        ),
        "Dart": (
            "dart",
            "dart pub get && dart compile exe bin/server.dart -o /tmp/meshagent-doctor-server",
        ),
        "Go": ("go", "go build -o /tmp/meshagent-doctor-server server.go"),
        "Ruby": ("ruby", "ruby -c server.rb"),
    }.get(diagnosis.language)


def _codex_diagnostics(diagnosis: ProjectDiagnosis) -> list[str]:
    diagnostics = [
        "If deployment fails, fix the first compiler, build, or container-start "
        "error in the deploy output before retrying with a fresh tag/domain.",
        "Use an image tag in `<repository>:<tag>` form, for example "
        "`doctor-app:$(date +%s)`; a bare timestamp or label can build but fail "
        "during service creation.",
        "Run `meshagent deploy` with the sanitized environment prefix shown "
        "below so room runtime variables do not leak into the deploy CLI "
        "process.",
        "If deploy fails after image export with `service ids are generated by "
        "the server`, retry once with the same sanitized environment prefix and "
        "a fresh `<repository>:<tag>` plus domain before changing app code.",
        "Keep `/health` returning 200 ok on port 8080; add task-specific routes "
        "such as `/status`, `/api/ping`, or `/room` before deploying.",
    ]
    build_check = _local_build_check(diagnosis)
    if build_check is not None:
        executable, command = build_check
        if shutil.which(executable) is None:
            diagnostics.append(
                "Local build/syntax check unavailable here because "
                f"`{executable}` is not on PATH; use the first Docker or "
                "MeshAgent deploy build error instead of installing a local "
                "toolchain."
            )
        else:
            diagnostics.append(f"Fast local build/syntax check: `{command}`.")
    if diagnosis.language == "Python":
        diagnostics.append(
            "MeshAgent Python deployments must target Python 3.13; use a "
            "`python:3.13-slim` base image."
        )
        if diagnosis.python_virtualenv_versions:
            virtualenv_list = ", ".join(
                f"`{path}`={version}"
                for path, version in diagnosis.python_virtualenv_versions
            )
            if any(
                _python_version_is_required(version)
                for _, version in diagnosis.python_virtualenv_versions
            ):
                diagnostics.append(
                    "A local Python 3.13 virtual environment was detected "
                    f"({virtualenv_list}); still deploy with a Python 3.13 "
                    "Docker base image."
                )
            else:
                diagnostics.append(
                    "Local virtual environments were found but none report "
                    f"Python {PYTHON_REQUIRED_VERSION} ({virtualenv_list}); "
                    "recreate the local venv with `python3.13 -m venv .venv` "
                    "before relying on local build checks."
                )
        else:
            diagnostics.append(
                "No local Python virtual environment metadata was detected; if "
                "you create one for troubleshooting, use "
                "`python3.13 -m venv .venv`."
            )
        if diagnosis.python_runtime_findings:
            diagnostics.append(
                "Upgrade older Python runtime metadata before deploying: update "
                "`.python-version`, `runtime.txt`, `pyproject.toml` "
                "`requires-python`, and any Dockerfile base image to allow Python "
                f"{PYTHON_REQUIRED_VERSION}."
            )
    if _is_javascript_project(diagnosis.language):
        scripts = dict(diagnosis.package_scripts)
        if "start" not in scripts and not _is_static_javascript_flavor(
            diagnosis.javascript_flavor
        ):
            diagnostics.append(
                "Add a `package.json` start script that binds the production server "
                "to `0.0.0.0:8080` before deploying."
            )
        if diagnosis.javascript_flavor in {"React", "React/Vite", "Vite"}:
            if "build" not in scripts:
                diagnostics.append(
                    "Add a `package.json` build script, usually `vite build` or "
                    "`react-scripts build`, before deploying a static frontend."
                )
            diagnostics.append(
                "For static React/Vite apps, build assets with `npm run build`, "
                "serve the generated `dist` or `build` directory with nginx on "
                "port 8080, include a `/health` location that returns 200, and "
                "write nginx pid/temp files under a writable `/data` room mount."
            )
            diagnostics.append(
                "Deploy static nginx apps with `--room-mount /:/data:rw`; "
                "MeshAgent service filesystems can be read-only, so nginx must "
                "not write pid, cache, or temp files under `/var`."
            )
            diagnostics.append(
                "After deploy, verify the public app URL itself returns 200 with "
                '`curl -fsS "$PUBLIC_URL/"`.'
            )
        elif diagnosis.javascript_flavor == "Next.js":
            diagnostics.append(
                "For Next.js, ensure the production server binds to "
                "`0.0.0.0:8080`; `next start -H 0.0.0.0 -p 8080` is the expected "
                "shape when using npm scripts."
            )
            diagnostics.append(
                "Add a `.dockerignore` before deploying Next.js/Node projects so "
                "`node_modules`, `.next`, `dist`, `build`, and npm debug logs are "
                "not streamed to MeshAgent after local build checks."
            )
            diagnostics.append(
                "If the app has no dedicated `/health` route, deploy with "
                "`--liveness /` and verify the public root URL returns HTTP 200."
            )
        else:
            diagnostics.append(
                "For Node.js servers, read `process.env.PORT || 8080`, bind to "
                "`0.0.0.0`, and make `/health` return 200 before deploying."
            )
            diagnostics.append(
                "Add a `.dockerignore` before deploying Node.js projects so "
                "`node_modules`, `dist`, `build`, and npm debug logs are not "
                "streamed to MeshAgent after local build checks."
            )
            diagnostics.append(
                "After deploy, verify the public root URL returns HTTP 200 with "
                '`curl -fsS "$PUBLIC_URL/"`; the JavaScript-family evals require '
                "the app URL itself to be reachable, not just `/health`."
            )
    if diagnosis.sdk is not None:
        diagnostics.extend(
            [
                "Before testing a RoomClient route, confirm the runtime has "
                "`MESHAGENT_ROOM`, `MESHAGENT_TOKEN`, `MESHAGENT_API_URL`, and "
                "`MESHAGENT_ROOM_URL`.",
                "For RoomClient deploys, use `--meshagent-token full` plus the "
                "`MESHAGENT_API_URL`, `MESHAGENT_ROOM`, and `MESHAGENT_ROOM_URL` "
                "env passthrough shown below.",
            ]
        )
    if diagnosis.sdk == "@meshagent/meshagent":
        diagnostics.append(
            "If Node reports `ERR_MODULE_NOT_FOUND` under "
            "`@meshagent/meshagent/dist/esm`, switch the app to the SDK's "
            'CommonJS path with `require("@meshagent/meshagent")` or compile '
            'TypeScript with `module: "CommonJS"`.'
        )
        if diagnosis.javascript_flavor == "Node.js/TypeScript":
            diagnostics.append(
                "If the project uses `type: module` or tsconfig `module: NodeNext`, "
                "convert the RoomClient server to CommonJS before deploy so the "
                "SDK resolves through its stable CommonJS entrypoint."
            )
    if diagnosis.sdk == "meshagent-api":
        diagnostics.extend(
            [
                "For Python `meshagent-api`, build the RoomClient explicitly with "
                "`RoomClient(protocol=WebSocketClientProtocol(url=websocket_room_url("
                'room_name=os.environ["MESHAGENT_ROOM"]), token=os.environ["MESHAGENT_TOKEN"]))`.',
                "`MESHAGENT_ROOM_URL` is the in-room HTTP endpoint; do not pass it "
                "directly to `WebSocketClientProtocol` or the SDK may fail with "
                "`WSServerHandshakeError: 200`.",
                "If Python reports `RoomClient.__init__()` got an unexpected keyword, "
                "inspect the installed SDK signature and use the explicit "
                "`protocol=WebSocketClientProtocol(...)` constructor above.",
            ]
        )
    if diagnosis.language == ".NET" and diagnosis.sdk == "Meshagent.Api":
        diagnostics.append(
            "If .NET publish cannot find `RoomClient`, add "
            "`using Meshagent.Api.Room;` and rebuild before deploying."
        )
    if diagnosis.language == "Dart" and diagnosis.sdk == "meshagent":
        diagnostics.append(
            "If Dart deploy times out during `dart compile`, run "
            "`dart run bin/server.dart` to isolate SDK/runtime errors from "
            "ahead-of-time compilation."
        )
    return diagnostics


def _print_report(diagnosis: ProjectDiagnosis) -> None:
    click.echo("MeshAgent doctor")
    click.echo(f"Project: {diagnosis.root}")
    click.echo(f"Detected project: {diagnosis.language}")
    if diagnosis.javascript_flavor is not None:
        click.echo(f"JavaScript flavor: {diagnosis.javascript_flavor}")
    if diagnosis.sdk is None:
        click.echo("Official RoomClient SDK: not detected")
    else:
        click.echo(f"Official RoomClient SDK: detected ({diagnosis.sdk})")
    click.echo("")

    click.echo("Findings:")
    if diagnosis.has_deployment_artifact:
        click.echo(
            "  [ok] Deployment artifact found: "
            + ", ".join(diagnosis.deployment_artifacts)
        )
    else:
        click.echo("  [missing] Deployment artifact: add Dockerfile or meshagent.yaml")
    if diagnosis.has_health_route:
        click.echo("  [ok] HTTP liveness route appears to exist: /health")
    elif _is_static_javascript_flavor(diagnosis.javascript_flavor):
        click.echo(
            "  [ok] Static web app Dockerfile guidance includes an nginx /health "
            "route returning 200"
        )
    elif diagnosis.javascript_flavor == "Next.js":
        click.echo(
            "  [check] Add a /health route or deploy Next.js with --liveness / "
            "and verify / returns 200"
        )
    else:
        click.echo("  [check] Add an HTTP /health route that returns 200 ok")
    if diagnosis.has_port_8080_hint:
        click.echo("  [ok] App appears to listen on port 8080")
    elif _is_static_javascript_flavor(diagnosis.javascript_flavor):
        click.echo("  [ok] Static web Dockerfile guidance serves nginx on port 8080")
    elif diagnosis.javascript_flavor == "Next.js":
        click.echo("  [check] Ensure Next.js binds to 0.0.0.0:8080")
    else:
        click.echo("  [check] Ensure the service listens on 0.0.0.0:8080")
    if diagnosis.language == "Python":
        if diagnosis.python_runtime_findings:
            click.echo(
                f"  [check] Upgrade Python runtime metadata to "
                f"{PYTHON_REQUIRED_VERSION} before deploying"
            )
            for finding in diagnosis.python_runtime_findings:
                click.echo(f"    - {finding}")
        else:
            click.echo(
                f"  [ok] Python Dockerfile guidance targets "
                f"Python {PYTHON_REQUIRED_VERSION}"
            )
        if diagnosis.python_virtualenv_versions:
            if any(
                _python_version_is_required(version)
                for _, version in diagnosis.python_virtualenv_versions
            ):
                click.echo(
                    f"  [ok] Local Python {PYTHON_REQUIRED_VERSION} virtual "
                    "environment detected"
                )
            else:
                click.echo(
                    f"  [check] No local Python {PYTHON_REQUIRED_VERSION} "
                    "virtual environment detected"
                )
            for path, version in diagnosis.python_virtualenv_versions:
                click.echo(
                    f"    - Local virtual environment `{path}` uses Python `{version}`"
                )
        else:
            click.echo("  [check] No local Python virtual environment detected")
    if _is_javascript_project(diagnosis.language):
        scripts = dict(diagnosis.package_scripts)
        if not _is_static_javascript_flavor(diagnosis.javascript_flavor):
            if "start" in scripts:
                click.echo(f"  [ok] package.json start script: {scripts['start']}")
            else:
                click.echo("  [check] Add a package.json start script for production")
        if diagnosis.javascript_flavor in {"React", "React/Vite", "Vite", "Next.js"}:
            if "build" in scripts:
                click.echo(f"  [ok] package.json build script: {scripts['build']}")
            else:
                click.echo("  [check] Add a package.json build script")
    if diagnosis.sdk is not None:
        click.echo(
            "  [required] RoomClient deployment needs --meshagent-token full "
            "and MESHAGENT_API_URL/MESHAGENT_ROOM/MESHAGENT_ROOM_URL env passthrough"
        )
    click.echo("")

    click.echo("Recommended next steps:")
    if not diagnosis.has_deployment_artifact and diagnosis.dockerfile != "":
        click.echo("1. Add a Dockerfile like:")
        click.echo("")
        click.echo(textwrap.indent(diagnosis.dockerfile, "   "))
        click.echo("")
        next_step_number = 2
    else:
        next_step_number = 1
    guidance = _sdk_guidance(diagnosis)
    if guidance:
        click.echo(f"{next_step_number}. SDK runtime guidance:")
        for item in guidance:
            click.echo(f"   - {item}")
        next_step_number += 1
    diagnostics = _codex_diagnostics(diagnosis)
    if diagnostics:
        click.echo(f"{next_step_number}. Diagnostics for Codex:")
        for item in diagnostics:
            click.echo(f"   - {item}")
        next_step_number += 1
    click.echo(f"{next_step_number}. Deploy from this directory:")
    click.echo(f"   {_deploy_command(diagnosis)}")


@click.command(
    "doctor", help="Inspect the current directory for MeshAgent deployment gaps."
)
@click.argument(
    "path",
    required=False,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
def doctor_command(path: Path | None = None) -> None:
    """Inspect a project directory and print deploy readiness guidance."""

    _print_report(diagnose_project(path or Path.cwd()))
