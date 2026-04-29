from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
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


@dataclass(frozen=True)
class ProjectDiagnosis:
    root: Path
    language: str
    sdk: str | None
    has_deployment_artifact: bool
    deployment_artifacts: tuple[str, ...]
    has_health_route: bool
    has_port_8080_hint: bool
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


def _start_command(language: str) -> str:
    return {
        "Python": "python server.py",
        "TypeScript": "npm start",
        "JavaScript": "npm start",
        ".NET": "dotnet DoctorDotnetRoomClient.dll",
        "Dart": "/app/server",
        "Go": "./server",
        "Ruby": "ruby server.rb",
    }.get(language, "<start command>")


def _dockerfile_for(language: str) -> str:
    snippets = {
        "Python": """
            FROM python:3.12-alpine
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
            COPY package.json tsconfig.json ./
            COPY src ./src
            RUN npm install && npm run build
            EXPOSE 8080
            CMD ["npm", "start"]
        """,
        "JavaScript": """
            FROM node:22-alpine
            WORKDIR /app
            COPY package.json server.js ./
            RUN npm install --omit=dev
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
    return textwrap.dedent(snippets.get(language, "")).strip()


def diagnose_project(root: Path) -> ProjectDiagnosis:
    resolved_root = root.resolve()
    source_files = _source_files(resolved_root)
    language = _detect_language(resolved_root)
    artifacts = _deployment_artifacts(resolved_root)
    return ProjectDiagnosis(
        root=resolved_root,
        language=language,
        sdk=_detect_sdk(resolved_root, language, source_files),
        has_deployment_artifact=bool(artifacts),
        deployment_artifacts=artifacts,
        has_health_route=_contains_any(
            source_files, ('"/health"', "'/health'", "/health")
        ),
        has_port_8080_hint=_contains_any(source_files, ("8080",)),
        start_command=_start_command(language),
        dockerfile=_dockerfile_for(language),
    )


def _deploy_command(diagnosis: ProjectDiagnosis) -> str:
    parts = [
        "meshagent deploy .",
        '--room "$MESHAGENT_ROOM"',
        "--tag <tag>",
        "--public",
        "--domain <domain>",
        "--liveness /health",
        "--no-optimize",
        "--no-wait",
    ]
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
        return [
            "The Node RoomClient SDK currently resolves reliably through its "
            'CommonJS entrypoint; use `require("@meshagent/meshagent")` or '
            "compile TypeScript to CommonJS before deploying RoomClient routes."
        ]
    if diagnosis.language == ".NET" and diagnosis.sdk == "Meshagent.Api":
        return [
            "RoomClient lives in the Meshagent.Api.Room namespace; add "
            "`using Meshagent.Api.Room;` before deploying if the app references "
            "RoomClient."
        ]
    return []


def _local_build_check(diagnosis: ProjectDiagnosis) -> str | None:
    return {
        "Python": "python -m py_compile server.py",
        "TypeScript": "npm install && npm run build",
        "JavaScript": "npm install --omit=dev",
        ".NET": "dotnet publish -c Release -o /tmp/meshagent-doctor-publish",
        "Dart": "dart pub get && dart compile exe bin/server.dart -o /tmp/meshagent-doctor-server",
        "Go": "go build -o /tmp/meshagent-doctor-server server.go",
        "Ruby": "ruby -c server.rb",
    }.get(diagnosis.language)


def _codex_diagnostics(diagnosis: ProjectDiagnosis) -> list[str]:
    diagnostics = [
        "If deployment fails, fix the first compiler, build, or container-start "
        "error in the deploy output before retrying with a fresh tag/domain.",
        "Keep `/health` returning 200 ok on port 8080; add task-specific routes "
        "such as `/status`, `/api/ping`, or `/room` before deploying.",
    ]
    build_check = _local_build_check(diagnosis)
    if build_check is not None:
        diagnostics.append(f"Fast local build/syntax check: `{build_check}`.")
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
    else:
        click.echo("  [check] Add an HTTP /health route that returns 200 ok")
    if diagnosis.has_port_8080_hint:
        click.echo("  [ok] App appears to listen on port 8080")
    else:
        click.echo("  [check] Ensure the service listens on 0.0.0.0:8080")
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
