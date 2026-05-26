from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
import json
import posixpath
from pathlib import Path
import re
import shlex
import shutil
import textwrap
import tomllib
from typing import Annotated, Iterable
import xml.etree.ElementTree as ET

import typer
from rich.console import Console
from rich.markup import escape

from meshagent.cli import async_typer
from meshagent.cli.meshagent_images import meshagent_image_prefix
from meshagent.cli.version import __version__ as MESHAGENT_CLIENT_VERSION


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
MESHAGENT_RUNTIME_LABEL = "meshagent.runtime"
PYTHON_REQUIRED_VERSION = "3.13"
PYTHON_REQUIRED_MAJOR_MINOR = (3, 13)
PYTHON_SDK_PACKAGE_NAME = "meshagent-api"
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
DOCTOR_TEMPLATE_PACKAGE = "meshagent.cli.doctor_templates"
PYTHON_PYPROJECT_TEMPLATE = "python-pyproject.toml"


@dataclass(frozen=True)
class ProjectDiagnosis:
    root: Path
    language: str
    javascript_flavor: str | None
    sdk: str | None
    has_deployment_artifact: bool
    deployment_artifacts: tuple[str, ...]
    has_dockerfile: bool
    dockerfile_is_meshagent_optimized: bool
    has_health_route: bool
    has_http_port_hint: bool
    is_headless_backend_agent: bool
    package_scripts: tuple[tuple[str, str], ...]
    sdk_versions: tuple[tuple[str, str], ...]
    python_has_pyproject: bool
    python_source_uses_sdk: bool
    python_sdk_versions: tuple[tuple[str, str], ...]
    python_runtime_findings: tuple[str, ...]
    python_virtualenv_versions: tuple[tuple[str, str], ...]
    liveness_path: str
    start_command: str


@dataclass(frozen=True)
class DoctorAutoFix:
    description: str
    path: Path
    contents: str
    overwrite: bool = False


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


def _python_dependency_entry_matches(value: object, package_name: str) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if normalized == "" or normalized.startswith("#"):
        return False
    return (
        re.match(rf"{re.escape(package_name.lower())}(?:\s|$|[<>=!~;\[])", normalized)
        is not None
    )


def _python_pyproject_dependency_entries(pyproject: dict[str, object]) -> list[str]:
    entries: list[str] = []

    project = pyproject.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            entries.extend(value for value in dependencies if isinstance(value, str))
        optional_dependencies = project.get("optional-dependencies")
        if isinstance(optional_dependencies, dict):
            for values in optional_dependencies.values():
                if isinstance(values, list):
                    entries.extend(value for value in values if isinstance(value, str))

    dependency_groups = pyproject.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for values in dependency_groups.values():
            if isinstance(values, list):
                entries.extend(value for value in values if isinstance(value, str))

    tool = pyproject.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    dependencies = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(dependencies, dict):
        entries.extend(str(name) for name in dependencies if str(name) != "python")

    return entries


def _python_dependency_exact_version(value: object, package_name: str) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized == "" or normalized.startswith("#"):
        return None
    normalized = normalized.split("#", 1)[0].strip()
    match = re.match(
        rf"{re.escape(package_name)}(?:\[[^\]]+\])?\s*==\s*"
        r"([A-Za-z0-9][A-Za-z0-9.!+_-]*)",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return match.group(1)


def _python_poetry_dependency_exact_version(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized == "" or normalized[0] in "<>=!~^":
            return None
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*$", normalized):
            return normalized
        return None

    if isinstance(value, dict):
        version = value.get("version")
        return _python_poetry_dependency_exact_version(version)

    return None


def _python_declared_dependency_versions(
    root: Path,
    package_name: str,
) -> list[tuple[str, str]]:
    versions: list[tuple[str, str]] = []

    for line in _read_text(root / "requirements.txt").splitlines():
        version = _python_dependency_exact_version(line, package_name)
        if version is not None:
            versions.append(("requirements.txt", version))

    pyproject = _read_toml(root / "pyproject.toml")
    for entry in _python_pyproject_dependency_entries(pyproject):
        version = _python_dependency_exact_version(entry, package_name)
        if version is not None:
            versions.append(("pyproject.toml", version))

    tool = pyproject.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    dependencies = poetry.get("dependencies") if isinstance(poetry, dict) else None
    if isinstance(dependencies, dict):
        for name, value in dependencies.items():
            if str(name).lower() != package_name:
                continue
            version = _python_poetry_dependency_exact_version(value)
            if version is not None:
                versions.append(("pyproject.toml", version))

    return versions


def _python_project_declares_dependency(root: Path, package_name: str) -> bool:
    requirements = _read_text(root / "requirements.txt").splitlines()
    if any(
        _python_dependency_entry_matches(line, package_name) for line in requirements
    ):
        return True

    pyproject = _read_toml(root / "pyproject.toml")
    return any(
        _python_dependency_entry_matches(entry, package_name)
        for entry in _python_pyproject_dependency_entries(pyproject)
    )


def _python_site_package_dirs(env_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    lib_dir = env_dir / "lib"
    try:
        python_dirs = sorted(lib_dir.glob("python*/site-packages"))
    except OSError:
        python_dirs = []
    candidates.extend(path for path in python_dirs if path.is_dir())

    windows_site_packages = env_dir / "Lib" / "site-packages"
    if windows_site_packages.is_dir():
        candidates.append(windows_site_packages)
    return candidates


def _python_installed_distribution_versions(
    root: Path,
    package_name: str,
) -> list[tuple[str, str]]:
    versions: list[tuple[str, str]] = []
    normalized_package_name = package_name.replace("-", "_").lower()

    for env_dir in _python_virtualenv_dirs(root):
        try:
            relative_env_dir = str(env_dir.relative_to(root))
        except ValueError:
            relative_env_dir = str(env_dir)

        for site_packages in _python_site_package_dirs(env_dir):
            try:
                metadata_paths = sorted(site_packages.glob("*.dist-info/METADATA"))
            except OSError:
                continue
            for metadata_path in metadata_paths:
                dist_info_name = metadata_path.parent.name.lower()
                if not dist_info_name.startswith(f"{normalized_package_name}-"):
                    continue

                metadata = _read_text(metadata_path)
                name: str | None = None
                version: str | None = None
                for line in metadata.splitlines():
                    key, separator, value = line.partition(":")
                    if not separator:
                        continue
                    if key.lower() == "name":
                        name = value.strip()
                    elif key.lower() == "version":
                        version = value.strip()
                if name is None or version is None:
                    continue
                if name.lower() != package_name:
                    continue
                versions.append((f"{relative_env_dir} installed package", version))

    return versions


def _python_sdk_versions(root: Path, language: str) -> tuple[tuple[str, str], ...]:
    if language != "Python":
        return ()

    versions = [
        *_python_declared_dependency_versions(root, PYTHON_SDK_PACKAGE_NAME),
        *_python_installed_distribution_versions(root, PYTHON_SDK_PACKAGE_NAME),
    ]
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, version in versions:
        item = (source, version)
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return tuple(deduped)


def _declared_dependency_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(
        r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.]+)?)",
        value.strip(),
    )
    if match is None:
        return None
    return match.group(1)


def _version_key(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or "0"),
    )


def _version_is_behind(value: str, reference: str) -> bool:
    value_key = _version_key(value)
    reference_key = _version_key(reference)
    return (
        value_key is not None
        and reference_key is not None
        and value_key < reference_key
    )


def _javascript_sdk_versions(
    root: Path, package_name: str
) -> tuple[tuple[str, str], ...]:
    version = _declared_dependency_version(
        _package_json_dependencies(root).get(package_name)
    )
    if version is None:
        return ()
    return (("package.json", version),)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _dotnet_sdk_versions(root: Path, package_name: str) -> tuple[tuple[str, str], ...]:
    versions: list[tuple[str, str]] = []
    for csproj_path in sorted(root.glob("*.csproj")):
        try:
            project = ET.fromstring(_read_text(csproj_path))
        except ET.ParseError:
            continue
        for element in project.iter():
            if _xml_local_name(element.tag) != "PackageReference":
                continue
            include = element.attrib.get("Include") or element.attrib.get("Update")
            if include is None or include.lower() != package_name.lower():
                continue
            version = _declared_dependency_version(element.attrib.get("Version"))
            if version is None:
                for child in element:
                    if _xml_local_name(child.tag) == "Version":
                        version = _declared_dependency_version(child.text)
                        break
            if version is not None:
                versions.append((csproj_path.name, version))
    return tuple(versions)


def _dart_sdk_versions(root: Path, package_name: str) -> tuple[tuple[str, str], ...]:
    dependency_indent: int | None = None
    for raw_line in _read_text(root / "pubspec.yaml").splitlines():
        stripped = raw_line.split("#", 1)[0].rstrip()
        if stripped.strip() == "":
            continue
        indent = len(stripped) - len(stripped.lstrip())
        if stripped.strip() == "dependencies:":
            dependency_indent = indent
            continue
        if dependency_indent is None:
            continue
        if indent <= dependency_indent:
            break
        match = re.match(rf"\s*{re.escape(package_name)}:\s*(\S+)?", stripped)
        if match is None:
            continue
        version = _declared_dependency_version(match.group(1))
        if version is None:
            return ()
        return (("pubspec.yaml", version),)
    return ()


def _sdk_versions(root: Path, language: str) -> tuple[tuple[str, str], ...]:
    if language == "Python":
        return _python_sdk_versions(root, language)
    if language in {"JavaScript", "TypeScript"}:
        return _javascript_sdk_versions(root, "@meshagent/meshagent")
    if language == ".NET":
        return _dotnet_sdk_versions(root, "Meshagent.Api")
    if language == "Dart":
        return _dart_sdk_versions(root, "meshagent")
    return ()


def _python_source_uses_sdk(source_files: list[Path]) -> bool:
    for path in source_files:
        for line in _read_text(path).splitlines():
            normalized = line.strip().lower()
            if normalized.startswith("from meshagent"):
                return True
            if normalized.startswith("import meshagent"):
                return True
            if re.search(r"\broomclient\b", normalized) is not None:
                return True
            if re.search(r"\bwebsocketclientprotocol\b", normalized) is not None:
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
        if _python_project_declares_dependency(root, "meshagent-api"):
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
    if (root / ".meshagent" / "deploy.yaml").exists():
        artifacts.append(".meshagent/deploy.yaml")
    return tuple(artifacts)


def _dockerfile_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / name
        for name in ("Dockerfile", "Containerfile")
        if (root / name).is_file()
    )


def _dockerfile_text(root: Path) -> str:
    return "\n".join(_read_text(path) for path in _dockerfile_paths(root))


def _dockerfile_from_image(args: str) -> str | None:
    for token in args.split():
        if token.startswith("--"):
            continue
        return token
    return None


def _dockerfile_final_stage_lines(dockerfile_text: str) -> tuple[str, ...]:
    final_stage_lines: list[str] = []
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#"):
            continue
        instruction, _, args = line.partition(" ")
        if instruction.lower() == "from":
            final_stage_lines = [line]
            continue
        if final_stage_lines:
            final_stage_lines.append(line)
    return tuple(final_stage_lines)


def _dockerfile_final_stage_base(dockerfile_text: str) -> str | None:
    final_stage_lines = _dockerfile_final_stage_lines(dockerfile_text)
    if len(final_stage_lines) == 0:
        return None
    instruction, _, args = final_stage_lines[0].partition(" ")
    if instruction.lower() != "from":
        return None
    return _dockerfile_from_image(args)


def _dockerfile_final_stage_runtime_label(dockerfile_text: str) -> str | None:
    for line in _dockerfile_final_stage_lines(dockerfile_text):
        instruction, _, args = line.partition(" ")
        if instruction.lower() != "label":
            continue
        match = re.search(
            rf"(?:^|\s){re.escape(MESHAGENT_RUNTIME_LABEL)}\s*=\s*\"?([^\"\s]+)",
            args,
            flags=re.IGNORECASE,
        )
        if match is not None:
            return match.group(1).strip().lower()
    return None


def _dockerfile_is_meshagent_optimized(root: Path) -> bool:
    dockerfile_text = _dockerfile_text(root)
    final_stage_base = _dockerfile_final_stage_base(dockerfile_text)
    runtime_label = _dockerfile_final_stage_runtime_label(dockerfile_text)
    return (
        final_stage_base is not None
        and final_stage_base.lower() == "scratch"
        and runtime_label in {"node", "python"}
    )


def _has_http_port_hint(root: Path, source_files: list[Path]) -> bool:
    dockerfile_text = _dockerfile_text(root).lower()
    if re.search(r"(?m)^\s*expose\s+\d+", dockerfile_text) is not None:
        return True

    return _contains_any(
        source_files,
        (
            "process.env.port",
            'os.environ.get("port"',
            "os.environ.get('port'",
            'environment.getenvironmentvariable("port")',
            "platform.environment['port']",
            'platform.environment["port"]',
            "0.0.0.0",
            "internetaddress.anyipv4",
            "listenandserve",
            ".listen(",
            "tcplistener",
            "tcpserver.new",
        ),
    )


def _has_web_service_hint(
    root: Path,
    *,
    language: str,
    javascript_flavor: str | None,
    source_files: list[Path],
    has_health_route: bool,
    has_http_port_hint: bool,
) -> bool:
    if has_health_route or has_http_port_hint:
        return True
    if javascript_flavor in {"React", "React/Vite", "Vite", "Next.js"}:
        return True
    if language == "Python":
        return _contains_any(
            source_files,
            (
                "aiohttp",
                "fastapi",
                "flask",
                "http.server",
                "threadinghttpserver",
                "uvicorn",
            ),
        )
    if language in {"JavaScript", "TypeScript"}:
        return _contains_any(
            source_files,
            (
                "createserver",
                "express",
                "fastify",
                "hono",
                "listen(",
            ),
        )
    if language == ".NET":
        csproj_text = "\n".join(_read_text(path) for path in root.glob("*.csproj"))
        return "microsoft.net.sdk.web" in csproj_text.lower() or _contains_any(
            source_files,
            (
                "webapplication",
                "mapget",
                "app.run",
            ),
        )
    if language == "Dart":
        return _contains_any(source_files, ("httpserver", "shelf", "shelf_router"))
    return False


def _start_command(language: str, javascript_flavor: str | None) -> str:
    if javascript_flavor in {"React", "React/Vite", "Vite"}:
        return "nginx -g 'daemon off;'"
    if javascript_flavor == "Next.js":
        return "npm start -- -H 0.0.0.0 -p $PORT"
    return {
        "Python": "python server.py",
        "TypeScript": "npm start",
        "JavaScript": "npm start",
        ".NET": "dotnet DoctorDotnetRoomClient.dll",
        "Dart": "/app/server",
        "Go": "./server",
        "Ruby": "ruby server.rb",
    }.get(language, "<start command>")


def _read_doctor_template(template_name: str) -> str:
    return (
        resources.files(DOCTOR_TEMPLATE_PACKAGE)
        .joinpath(template_name)
        .read_text(encoding="utf-8")
    )


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
    has_http_port_hint = _has_http_port_hint(resolved_root, source_files)
    sdk = _detect_sdk(resolved_root, language, source_files)
    python_has_pyproject = (
        language == "Python" and (resolved_root / "pyproject.toml").is_file()
    )
    python_source_uses_sdk = language == "Python" and _python_source_uses_sdk(
        source_files
    )
    sdk_versions = _sdk_versions(resolved_root, language)
    is_roomclient_project = sdk is not None or (
        language == "Python" and python_source_uses_sdk
    )
    has_web_service_hint = _has_web_service_hint(
        resolved_root,
        language=language,
        javascript_flavor=javascript_flavor,
        source_files=source_files,
        has_health_route=has_health_route,
        has_http_port_hint=has_http_port_hint,
    )
    is_headless_backend_agent = is_roomclient_project and not has_web_service_hint
    liveness_path = _liveness_path_for(language, javascript_flavor, has_health_route)
    return ProjectDiagnosis(
        root=resolved_root,
        language=language,
        javascript_flavor=javascript_flavor,
        sdk=sdk,
        has_deployment_artifact=bool(artifacts),
        deployment_artifacts=artifacts,
        has_dockerfile=bool(_dockerfile_paths(resolved_root)),
        dockerfile_is_meshagent_optimized=_dockerfile_is_meshagent_optimized(
            resolved_root
        ),
        has_health_route=has_health_route,
        has_http_port_hint=has_http_port_hint,
        is_headless_backend_agent=is_headless_backend_agent,
        package_scripts=tuple(sorted(_package_json_scripts(resolved_root).items())),
        sdk_versions=sdk_versions,
        python_has_pyproject=python_has_pyproject,
        python_source_uses_sdk=python_source_uses_sdk,
        python_sdk_versions=sdk_versions if language == "Python" else (),
        python_runtime_findings=python_runtime_findings,
        python_virtualenv_versions=python_virtualenv_versions,
        liveness_path=liveness_path,
        start_command=_start_command(language, javascript_flavor),
    )


def _meshagent_deploy_command(diagnosis: ProjectDiagnosis) -> str:
    parts = [
        "meshagent deploy .",
        '--room "$MESHAGENT_ROOM"',
        f"--tag {_suggested_image_tag(diagnosis.root)}",
    ]
    if not diagnosis.is_headless_backend_agent:
        parts.extend(
            [
                "--public",
                "--domain <domain>",
                f"--liveness {diagnosis.liveness_path}",
            ]
        )
    parts.append("--wait")
    if _is_static_javascript_flavor(diagnosis.javascript_flavor):
        parts.insert(-1, "--room-mount /:/data:rw")
    for mount_arg in _deploy_volume_mount_args(diagnosis):
        if mount_arg not in parts:
            parts.insert(-1, mount_arg)
    if _needs_roomclient_runtime(diagnosis):
        parts.append("--meshagent-token agentDefault")
    return " ".join(parts)


def _deploy_command(diagnosis: ProjectDiagnosis) -> str:
    if _is_javascript_project(diagnosis.language):
        scripts = dict(diagnosis.package_scripts)
        if isinstance(scripts.get("deploy"), str):
            return "npm run deploy"
    return _meshagent_deploy_command(diagnosis)


def _dockerfile_volume_paths(diagnosis: ProjectDiagnosis) -> tuple[str, ...]:
    dockerfile = diagnosis.root / "Dockerfile"
    if not dockerfile.is_file():
        return ()

    from meshagent.cli.image import _parse_packed_dockerfile_metadata

    metadata = _parse_packed_dockerfile_metadata(local_packed_dockerfile=dockerfile)
    if metadata is None:
        return ()
    return metadata.volumes


def _deploy_mount_arg_for_volume_path(volume_path: str) -> str:
    if volume_path == "/data":
        return "--room-mount /:/data:rw"
    return f"--empty-dir-mount {volume_path}"


def _deploy_volume_mount_args(diagnosis: ProjectDiagnosis) -> tuple[str, ...]:
    mount_args: list[str] = []
    for volume_path in _dockerfile_volume_paths(diagnosis):
        mount_arg = _deploy_mount_arg_for_volume_path(volume_path)
        if mount_arg not in mount_args:
            mount_args.append(mount_arg)
    return tuple(mount_args)


def _split_option_value(token: str, option: str) -> str | None:
    if token.startswith(f"{option}="):
        return token.removeprefix(f"{option}=")
    return None


def _deploy_mount_value_path(*, option: str, value: str) -> str:
    if option == "--image-mount":
        _, _, mount_spec = value.partition("=")
        value = mount_spec
    if option in {"--room-mount", "--project-mount"}:
        parts = value.split(":")
        if len(parts) < 2:
            return ""
        return posixpath.normpath(parts[1].strip())
    mount_path = value.split(":", 1)[0]
    return posixpath.normpath(mount_path.strip())


def _deploy_script_mounts_volume_path(deploy_script: str, volume_path: str) -> bool:
    try:
        tokens = shlex.split(deploy_script, comments=False, posix=True)
    except ValueError:
        return False

    expected_path = posixpath.normpath(volume_path)
    mount_options = {
        "--empty-dir-mount",
        "--room-mount",
        "--project-mount",
        "--image-mount",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        for option in mount_options:
            option_value = _split_option_value(token, option)
            if option_value is not None and (
                _deploy_mount_value_path(option=option, value=option_value)
                == expected_path
            ):
                return True
        if token in mount_options and index + 1 < len(tokens):
            if (
                _deploy_mount_value_path(option=token, value=tokens[index + 1])
                == expected_path
            ):
                return True
            index += 2
            continue
        index += 1
    return False


def _suggested_image_tag(root: Path) -> str:
    repository = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip(".-_")
    if not repository:
        repository = "meshagent-app"
    return f"{repository}:latest"


def _needs_roomclient_runtime(diagnosis: ProjectDiagnosis) -> bool:
    return diagnosis.sdk is not None or (
        diagnosis.language == "Python" and diagnosis.python_source_uses_sdk
    )


def _javascript_roomclient_deploy_token_finding(
    diagnosis: ProjectDiagnosis,
) -> tuple[str, str] | None:
    if not _needs_roomclient_runtime(diagnosis) or not _is_javascript_project(
        diagnosis.language
    ):
        return None

    scripts = dict(diagnosis.package_scripts)
    deploy_script = scripts.get("deploy")
    if deploy_script is None:
        return (
            "warning",
            "RoomClient deploy-token check: add a package.json deploy script "
            "that runs `meshagent deploy` with `--tag` and "
            "`--meshagent-token agentDefault`.",
        )
    missing_args: list[str] = []
    if re.search(r"(?:^|\s)--tag(?:=|\s)", deploy_script) is None:
        missing_args.append("`--tag`")
    for volume_path in _dockerfile_volume_paths(diagnosis):
        if not _deploy_script_mounts_volume_path(deploy_script, volume_path):
            missing_args.append(f"a mount for Dockerfile volume `{volume_path}`")
    if "--meshagent-token agentDefault" not in deploy_script:
        missing_args.append("`--meshagent-token agentDefault`")
    if missing_args:
        return (
            "warning",
            "RoomClient deploy-token check: package.json deploy script should "
            f"include {' and '.join(missing_args)}.",
        )
    return (
        "ok",
        "RoomClient deploy-token check: package.json deploy script injects "
        "`--meshagent-token agentDefault`.",
    )


def _javascript_roomclient_deploy_script_is_fixable(
    diagnosis: ProjectDiagnosis,
) -> bool:
    deploy_token_finding = _javascript_roomclient_deploy_token_finding(diagnosis)
    return deploy_token_finding is not None and deploy_token_finding[0] == "warning"


def _supports_meshagent_optimized_dockerfile(diagnosis: ProjectDiagnosis) -> bool:
    return diagnosis.language == "Python" or _is_javascript_project(diagnosis.language)


def _relative_source_file_names(diagnosis: ProjectDiagnosis) -> tuple[str, ...]:
    return tuple(
        str(path.relative_to(diagnosis.root)) for path in _source_files(diagnosis.root)
    )


def _is_single_file_python_server(diagnosis: ProjectDiagnosis) -> bool:
    return _relative_source_file_names(diagnosis) == ("server.py",)


def _has_package_dependency(diagnosis: ProjectDiagnosis, package_name: str) -> bool:
    return package_name in _package_json_dependencies(diagnosis.root)


def _can_autofix_python_project_metadata(diagnosis: ProjectDiagnosis) -> bool:
    return diagnosis.language == "Python" and _is_single_file_python_server(diagnosis)


def _can_use_node_dockerfile_recommendation(diagnosis: ProjectDiagnosis) -> bool:
    scripts = dict(diagnosis.package_scripts)
    if diagnosis.language == "JavaScript":
        return (
            _relative_source_file_names(diagnosis) == ("server.js",)
            and scripts.get("build") == "ncc build server.js -o dist"
            and scripts.get("start") == "node dist/index.js"
            and _has_package_dependency(diagnosis, "@vercel/ncc")
        )
    if diagnosis.language == "TypeScript":
        return (
            _relative_source_file_names(diagnosis) == ("src/server.ts",)
            and (diagnosis.root / "tsconfig.json").is_file()
            and scripts.get("build") == "ncc build src/server.ts -o dist"
            and scripts.get("start") == "node dist/index.js"
            and _has_package_dependency(diagnosis, "@vercel/ncc")
        )
    return False


def _python_requirements_dependency_entries(root: Path) -> tuple[str, ...]:
    dependencies: list[str] = []
    for raw_line in _read_text(root / "requirements.txt").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line == "":
            continue
        if line.startswith(("-", "git+", "http://", "https://")):
            continue
        dependencies.append(line)
    return tuple(dependencies)


def _python_project_name(root: Path) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    if normalized == "":
        return "meshagent-project"
    if not normalized[0].isalpha():
        return f"meshagent-{normalized}"
    return normalized


def _python_fix_dependencies(diagnosis: ProjectDiagnosis) -> tuple[str, ...]:
    dependencies: list[str] = []
    dependencies.extend(
        dependency
        for dependency in _python_requirements_dependency_entries(diagnosis.root)
        if not _python_dependency_entry_matches(dependency, PYTHON_SDK_PACKAGE_NAME)
    )
    source_files = list(_source_files(diagnosis.root))
    if diagnosis.python_source_uses_sdk or diagnosis.sdk == PYTHON_SDK_PACKAGE_NAME:
        dependencies.append(f"meshagent-api=={MESHAGENT_CLIENT_VERSION}")
    if _contains_any(source_files, ("aiohttp",)) and not any(
        _python_dependency_entry_matches(dependency, "aiohttp")
        for dependency in dependencies
    ):
        dependencies.append("aiohttp[speedups]~=3.13.0")
    return tuple(dict.fromkeys(dependencies))


def _python_pyproject_for(diagnosis: ProjectDiagnosis) -> str:
    dependency_lines = "".join(
        f'  "{dependency}",\n' for dependency in _python_fix_dependencies(diagnosis)
    )
    setuptools_section = ""
    if (diagnosis.root / "server.py").is_file():
        setuptools_section = '\n\n[tool.setuptools]\npy-modules = ["server"]'
    return (
        _read_doctor_template(PYTHON_PYPROJECT_TEMPLATE)
        .replace("__PYTHON_PROJECT_NAME__", _python_project_name(diagnosis.root))
        .replace("__PYTHON_REQUIRED_VERSION__", PYTHON_REQUIRED_VERSION)
        .replace("__PYTHON_DEPENDENCY_LINES__", dependency_lines)
        .replace("__PYTHON_SETUPTOOLS_SECTION__", setuptools_section)
        .rstrip()
        + "\n"
    )


def _package_json_with_autofixes(diagnosis: ProjectDiagnosis) -> str | None:
    package_json = _read_json(diagnosis.root / "package.json")
    if not package_json:
        return None
    changed = False
    for key in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        dependencies = package_json.get(key)
        if not isinstance(dependencies, dict):
            continue
        declared_version = _declared_dependency_version(
            dependencies.get("@meshagent/meshagent")
        )
        if declared_version is None or not _version_is_behind(
            declared_version, MESHAGENT_CLIENT_VERSION
        ):
            continue
        dependencies["@meshagent/meshagent"] = MESHAGENT_CLIENT_VERSION
        changed = True
        break
    if _javascript_roomclient_deploy_script_is_fixable(diagnosis):
        scripts = package_json.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
            package_json["scripts"] = scripts
        deploy_script = scripts.get("deploy")
        if not isinstance(deploy_script, str) or deploy_script.strip() == "":
            scripts["deploy"] = _meshagent_deploy_command(diagnosis)
            changed = True
        else:
            fixed_deploy_script = deploy_script
            if re.search(r"(?:^|\s)--tag(?:=|\s)", fixed_deploy_script) is None:
                fixed_deploy_script = (
                    f"{fixed_deploy_script.rstrip()} "
                    f"--tag {_suggested_image_tag(diagnosis.root)}"
                )
            for volume_path in _dockerfile_volume_paths(diagnosis):
                if not _deploy_script_mounts_volume_path(
                    fixed_deploy_script, volume_path
                ):
                    fixed_deploy_script = (
                        f"{fixed_deploy_script.rstrip()} "
                        f"{_deploy_mount_arg_for_volume_path(volume_path)}"
                    )
            if "--meshagent-token agentDefault" not in fixed_deploy_script:
                token_fixed_deploy_script = re.sub(
                    r"--meshagent-token(?:=|\s+)\S+",
                    "--meshagent-token agentDefault",
                    fixed_deploy_script,
                )
                if token_fixed_deploy_script == fixed_deploy_script:
                    fixed_deploy_script = (
                        f"{fixed_deploy_script.rstrip()} --meshagent-token agentDefault"
                    )
                else:
                    fixed_deploy_script = token_fixed_deploy_script
            if fixed_deploy_script != deploy_script:
                scripts["deploy"] = fixed_deploy_script
                changed = True
    if not changed:
        return None
    return f"{json.dumps(package_json, indent=2)}\n"


def _autofix_plan(diagnosis: ProjectDiagnosis) -> tuple[DoctorAutoFix, ...]:
    fixes: list[DoctorAutoFix] = []
    if (
        diagnosis.language == "Python"
        and not diagnosis.python_has_pyproject
        and _can_autofix_python_project_metadata(diagnosis)
    ):
        fixes.append(
            DoctorAutoFix(
                description="Create pyproject.toml with Python runtime metadata",
                path=diagnosis.root / "pyproject.toml",
                contents=_python_pyproject_for(diagnosis),
            )
        )
    package_json_with_autofixes = _package_json_with_autofixes(diagnosis)
    if package_json_with_autofixes is not None:
        fixes.append(
            DoctorAutoFix(
                description=(
                    "Update package.json MeshAgent SDK version and deploy script "
                    "where needed"
                ),
                path=diagnosis.root / "package.json",
                contents=package_json_with_autofixes,
                overwrite=True,
            )
        )
    return tuple(fixes)


def _apply_auto_fixes(diagnosis: ProjectDiagnosis) -> tuple[DoctorAutoFix, ...]:
    fixes = _autofix_plan(diagnosis)
    applied: list[DoctorAutoFix] = []
    for fix in fixes:
        if fix.path.exists() and not fix.overwrite:
            continue
        fix.path.parent.mkdir(parents=True, exist_ok=True)
        fix.path.write_text(fix.contents, encoding="utf-8")
        applied.append(fix)
    return tuple(applied)


def _sdk_checks(diagnosis: ProjectDiagnosis) -> list[str]:
    if diagnosis.sdk == "@meshagent/meshagent":
        checks = [
            "The Node RoomClient SDK currently resolves reliably through its "
            'CommonJS entrypoint; use `require("@meshagent/meshagent")` or '
            "compile TypeScript to CommonJS before deploying RoomClient routes."
        ]
        if diagnosis.javascript_flavor == "Node.js/TypeScript":
            checks.append(
                "For TypeScript RoomClient servers, set `compilerOptions.module` "
                'to `"CommonJS"` and `moduleResolution` to `"Node"`, remove '
                '`"type": "module"` from `package.json` or set it to `"commonjs"`, '
                "and make `npm start` run the built CommonJS entrypoint, for "
                "example `node dist/server.js`."
            )
        if _is_static_javascript_flavor(diagnosis.javascript_flavor):
            checks.append(
                "Static React/Vite browser bundles cannot hold a room token safely; "
                "keep RoomClient calls in a Node server or API route and have the "
                "frontend call that route."
            )
        return checks
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
            "dotnet publish -c Release -o /tmp/meshagent-doctor-publish --disable-build-servers /p:UseSharedCompilation=false",
        ),
        "Dart": (
            "dart",
            "dart pub get && dart compile exe bin/server.dart -o /tmp/meshagent-doctor-server",
        ),
        "Go": ("go", "go build -o /tmp/meshagent-doctor-server server.go"),
        "Ruby": ("ruby", "ruby -c server.rb"),
    }.get(diagnosis.language)


def _deployment_checks(diagnosis: ProjectDiagnosis) -> list[str]:
    checks: list[str] = []
    if diagnosis.is_headless_backend_agent:
        checks.append(
            "Headless backend-agent rule: RoomClient-only services can omit "
            "`EXPOSE`, `--public`, `--domain`, and `--liveness`; add HTTP ports "
            "only when the process serves HTTP."
        )
    else:
        checks.append(
            "HTTP service rule: keep `/health` returning 200 ok on the published "
            "HTTP container port; add task-specific routes such as `/status`, "
            "`/api/ping`, or `/room` before deploying."
        )
    build_check = _local_build_check(diagnosis)
    if build_check is not None:
        executable, command = build_check
        if shutil.which(executable) is None:
            checks.append(
                "Local build check: unavailable here because "
                f"`{executable}` is not on PATH; use the first Docker or "
                "MeshAgent deploy build error instead of installing a local "
                "toolchain."
            )
        else:
            checks.append(f"Local build check: `{command}`.")
    if diagnosis.language == "Python":
        checks.append(
            "Python runtime rule: MeshAgent Python deployments must target "
            "Python 3.13; use the MeshAgent-optimized Dockerfile shape with "
            "`python-sdk-slim` in the build stage and `LABEL meshagent.runtime=python` "
            "in the scratch final stage."
        )
        if not diagnosis.python_has_pyproject:
            checks.append(
                "Python project metadata check: add a `pyproject.toml` with "
                '`requires-python = ">=3.13"` and the runtime dependencies '
                "needed by the app before deploying."
            )
        if diagnosis.python_source_uses_sdk and diagnosis.sdk is None:
            checks.append(
                "Python SDK dependency check: source imports MeshAgent SDK "
                "symbols, but this project does not declare `meshagent-api`; "
                "add it to `pyproject.toml` or `requirements.txt` so the "
                "deployed container installs the SDK."
            )
        if diagnosis.python_sdk_versions:
            for source, version in diagnosis.python_sdk_versions:
                if version == MESHAGENT_CLIENT_VERSION:
                    continue
                checks.append(
                    "Python SDK version check: "
                    f"`{source}` uses `meshagent-api=={version}`, but this "
                    f"`meshagent` client is `{MESHAGENT_CLIENT_VERSION}`; "
                    "update the project dependency or reinstall the local "
                    "virtualenv so the SDK and CLI versions match."
                )
        elif diagnosis.sdk == PYTHON_SDK_PACKAGE_NAME:
            checks.append(
                "Python SDK version check: "
                f"pin `meshagent-api=={MESHAGENT_CLIENT_VERSION}` in project "
                "metadata so the deployed SDK version matches this `meshagent` client."
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
                checks.append(
                    "Python virtualenv check: a local Python 3.13 virtual "
                    "environment was detected "
                    f"({virtualenv_list}); still deploy with a Python 3.13 "
                    "Docker base image."
                )
            else:
                checks.append(
                    "Python virtualenv check: local virtual environments were "
                    "found but none report "
                    f"Python {PYTHON_REQUIRED_VERSION} ({virtualenv_list}); "
                    "recreate the local venv with `python3.13 -m venv .venv` "
                    "before relying on local build checks."
                )
        else:
            checks.append(
                "Python virtualenv check: no local Python virtual environment "
                "metadata was detected; if you create one for troubleshooting, use "
                "`python3.13 -m venv .venv`."
            )
        if diagnosis.python_runtime_findings:
            checks.append(
                "Python runtime metadata check: update `.python-version`, "
                "`runtime.txt`, `pyproject.toml` `requires-python`, and any "
                f"Dockerfile base image to allow Python {PYTHON_REQUIRED_VERSION}."
            )
    if _is_javascript_project(diagnosis.language):
        scripts = dict(diagnosis.package_scripts)
        if "start" not in scripts and not _is_static_javascript_flavor(
            diagnosis.javascript_flavor
        ):
            if diagnosis.is_headless_backend_agent:
                checks.append(
                    "Node start-script check: add a `package.json` start script "
                    "for the long-running RoomClient backend agent process."
                )
            else:
                checks.append(
                    "Node start-script check: add a `package.json` start script "
                    "that binds the production server to `0.0.0.0` on the "
                    "service's declared HTTP container port."
                )
        if diagnosis.javascript_flavor in {"React", "React/Vite", "Vite"}:
            if "build" not in scripts:
                checks.append(
                    "Static frontend build check: add a `package.json` build "
                    "script, usually `vite build` or `react-scripts build`, "
                    "before deploying."
                )
            checks.append(
                "Static frontend serving check: build assets with `npm run build`, "
                "serve the generated `dist` or `build` directory with nginx on a "
                "declared HTTP container port, include a `/health` location that "
                "returns 200, and write nginx pid/temp files under a writable "
                "`/data` room mount."
            )
            checks.append(
                "Static nginx storage check: deploy with `--room-mount /:/data:rw`; "
                "MeshAgent service filesystems can be read-only, so nginx must not "
                "write pid, cache, or temp files under `/var`."
            )
            checks.append(
                "Static app liveness check: after deploy, verify the public app "
                "URL itself returns 200 with "
                '`curl -fsS "$PUBLIC_URL/"`.'
            )
        elif diagnosis.javascript_flavor == "Next.js":
            checks.append(
                "Next.js binding check: ensure the production server binds to "
                "`0.0.0.0` and reads `PORT` or uses the same fallback port "
                "declared in Dockerfile `EXPOSE`."
            )
            checks.append(
                "Next.js Docker context check: add a `.dockerignore` so "
                "`node_modules`, `.next`, `dist`, `build`, and npm debug logs "
                "are not streamed to MeshAgent after local build checks."
            )
            checks.append(
                "Next.js liveness check: if the app has no dedicated `/health` "
                "route, deploy with `--liveness /` and verify the public root URL "
                "returns HTTP 200."
            )
        elif diagnosis.is_headless_backend_agent:
            checks.append(
                "Node backend-agent check: keep a `package.json` start script for "
                "the long-running worker process; do not add a public HTTP port "
                "unless the agent also serves HTTP."
            )
        else:
            checks.append(
                "Node HTTP binding check: read `process.env.PORT` with an "
                "app-specific fallback matching Dockerfile `EXPOSE`, bind to "
                "`0.0.0.0`, and make `/health` return 200 before deploying."
            )
            checks.append(
                "Node Docker context check: add a `.dockerignore` so "
                "`node_modules`, `dist`, `build`, and npm debug logs are not "
                "streamed to MeshAgent after local build checks."
            )
            checks.append(
                "Node app liveness check: after deploy, verify the public root "
                "URL returns HTTP 200 with "
                '`curl -fsS "$PUBLIC_URL/"`; the app URL itself should be '
                "reachable, not just `/health`."
            )
        if (
            diagnosis.javascript_flavor in {"Node.js", "Node.js/TypeScript"}
            and not diagnosis.has_dockerfile
            and not _can_use_node_dockerfile_recommendation(diagnosis)
        ):
            if diagnosis.language == "TypeScript":
                checks.append(
                    "Node ncc optimization check: to use the MeshAgent-optimized "
                    "Dockerfile snippet, add `@vercel/ncc`, set "
                    '`"build": "ncc build src/server.ts -o dist"`, and set '
                    '`"start": "node dist/index.js"`; otherwise adapt the '
                    "Dockerfile to the project's actual build output."
                )
            else:
                checks.append(
                    "Node ncc optimization check: to use the MeshAgent-optimized "
                    "Dockerfile snippet, add `@vercel/ncc`, set "
                    '`"build": "ncc build server.js -o dist"`, and set '
                    '`"start": "node dist/index.js"`; otherwise adapt the '
                    "Dockerfile to the project's actual build output."
                )
    if _needs_roomclient_runtime(diagnosis):
        checks.extend(
            [
                "RoomClient room check: pass `--room` or set `MESHAGENT_ROOM` "
                "for the target room.",
            ]
        )
        deploy_token_finding = _javascript_roomclient_deploy_token_finding(diagnosis)
        if deploy_token_finding is not None and deploy_token_finding[0] == "warning":
            checks.append(deploy_token_finding[1])
    if diagnosis.sdk == "@meshagent/meshagent":
        checks.append(
            "Node RoomClient module check: if Node reports `ERR_MODULE_NOT_FOUND` "
            "under `@meshagent/meshagent/dist/esm`, switch the app to the SDK's "
            'CommonJS path with `require("@meshagent/meshagent")` or compile '
            'TypeScript with `module: "CommonJS"`.'
        )
        if diagnosis.javascript_flavor == "Node.js/TypeScript":
            checks.append(
                "TypeScript RoomClient module check: if the project uses "
                "`type: module` or tsconfig `module: NodeNext`, convert the "
                "RoomClient server to CommonJS before deploy so the SDK resolves "
                "through its stable CommonJS entrypoint."
            )
    if diagnosis.sdk == "meshagent-api":
        checks.extend(
            [
                "Python RoomClient constructor check: build the RoomClient with "
                "`RoomClient(protocol_factory=WebSocketClientProtocol("
                'url=websocket_room_url(room_name=os.environ["MESHAGENT_ROOM"]), '
                'token=os.environ["MESHAGENT_TOKEN"]).create_factory())`.',
                "Python RoomClient URL check: `MESHAGENT_ROOM_URL` is the in-room "
                "HTTP endpoint; do not pass it directly to "
                "`WebSocketClientProtocol` or the SDK may fail with "
                "`WSServerHandshakeError: 200`.",
                "Python RoomClient signature check: if Python reports "
                "`RoomClient.__init__()` got an unexpected keyword, inspect the "
                "installed SDK signature and use the explicit "
                "`protocol_factory=WebSocketClientProtocol(...).create_factory()` "
                "constructor above.",
            ]
        )
    if diagnosis.language == ".NET" and diagnosis.sdk == "Meshagent.Api":
        checks.extend(
            [
                ".NET RoomClient namespace check: if publish cannot find "
                "`RoomClient`, add `using Meshagent.Api.Room;` and rebuild before "
                "deploying.",
                ".NET Docker publish check: run publish with "
                "`--disable-build-servers /p:UseSharedCompilation=false` so "
                "compiler/build-server processes do not survive the RUN step "
                "and trigger BuildKit cgroup cleanup failures.",
            ]
        )
    if diagnosis.language == "Dart" and diagnosis.sdk == "meshagent":
        checks.append(
            "Dart compile check: if deploy times out during `dart compile`, run "
            "`dart run bin/server.dart` to isolate SDK/runtime errors from "
            "ahead-of-time compilation."
        )
    return checks


FINDING_COLORS = {
    "ok": "green",
    "warning": "yellow",
    "error": "red",
}
_DOCTOR_CONSOLE = Console(soft_wrap=True)


def _format_finding_label(severity: str) -> str:
    return f"[{FINDING_COLORS[severity]}]{escape(f'[{severity}]')}[/]"


def _echo_finding(severity: str, message: str) -> None:
    _DOCTOR_CONSOLE.print(f"  {_format_finding_label(severity)} {escape(message)}")


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _has_deploy_spec(root: Path) -> bool:
    return (root / ".meshagent" / "deploy.yaml").is_file()


def _print_report(diagnosis: ProjectDiagnosis) -> None:
    auto_fixes = _autofix_plan(diagnosis)

    typer.echo("MeshAgent doctor")
    typer.echo(f"Project: {diagnosis.root}")
    typer.echo(f"Detected project: {diagnosis.language}")
    if diagnosis.javascript_flavor is not None:
        typer.echo(f"JavaScript flavor: {diagnosis.javascript_flavor}")
    if diagnosis.sdk is None:
        typer.echo("Official RoomClient SDK: not detected")
    else:
        typer.echo(f"Official RoomClient SDK: detected ({diagnosis.sdk})")
    typer.echo("")

    if diagnosis.language == "Unknown" and not diagnosis.has_deployment_artifact:
        typer.echo("Findings:")
        _echo_finding("error", "No identifiable deployable project was detected")
        typer.echo("")
        typer.echo("Recommended next steps:")
        typer.echo("1. Create a minimal deployable Python backend agent project:")
        typer.echo("   meshagent create")
        typer.echo("2. Re-run doctor and address remaining findings:")
        typer.echo("   meshagent doctor")
        typer.echo("3. Deployment checks:")
        typer.echo(
            "   - Project detection check: no recognizable application code or "
            "deployment metadata was found in this directory. Run `meshagent create` "
            "to create a minimal Python backend agent project, then deploy the "
            "generated project."
        )
        return

    typer.echo("Findings:")
    if diagnosis.has_deployment_artifact:
        _echo_finding(
            "ok",
            "Deployment artifact found: " + ", ".join(diagnosis.deployment_artifacts),
        )
    else:
        _echo_finding(
            "warning",
            "Dockerfile missing: add Dockerfile or meshagent.yaml",
        )
    if _has_deploy_spec(diagnosis.root):
        _echo_finding("ok", "Deploy spec found: .meshagent/deploy.yaml")
    else:
        _echo_finding(
            "warning",
            "Deploy spec missing: add .meshagent/deploy.yaml for template values; "
            "run `meshagent create` to see deploy spec examples",
        )
    if diagnosis.has_dockerfile and _supports_meshagent_optimized_dockerfile(diagnosis):
        if diagnosis.dockerfile_is_meshagent_optimized:
            _echo_finding(
                "ok",
                "MeshAgent-optimized Dockerfile uses a scratch final stage "
                "with a meshagent.runtime label",
            )
        else:
            _echo_finding(
                "warning",
                "Dockerfile is not MeshAgent-optimized; use a final FROM scratch "
                "stage with LABEL meshagent.runtime=python or node so deploy can "
                "reuse cached runtime images",
            )
    if diagnosis.is_headless_backend_agent:
        _echo_finding(
            "ok",
            "Backend agent does not require an HTTP /health route unless it serves HTTP",
        )
    elif diagnosis.has_health_route:
        _echo_finding("ok", "HTTP liveness route appears to exist: /health")
    elif _is_static_javascript_flavor(diagnosis.javascript_flavor):
        _echo_finding(
            "ok",
            "Static web app Dockerfile check includes an nginx /health "
            "route returning 200",
        )
    elif diagnosis.javascript_flavor == "Next.js":
        _echo_finding(
            "warning",
            "Add a /health route or deploy Next.js with --liveness / "
            "and verify / returns 200",
        )
    else:
        _echo_finding("warning", "Add an HTTP /health route that returns 200 ok")
    if diagnosis.is_headless_backend_agent:
        _echo_finding(
            "ok",
            "Backend agent does not require exposed or published HTTP ports",
        )
    elif diagnosis.has_http_port_hint:
        _echo_finding("ok", "App declares or binds an HTTP container port")
    elif _is_static_javascript_flavor(diagnosis.javascript_flavor):
        _echo_finding(
            "ok",
            "Static web Dockerfile check serves nginx on a declared container port",
        )
    elif diagnosis.javascript_flavor == "Next.js":
        _echo_finding(
            "warning",
            "Ensure Next.js binds to 0.0.0.0 on the declared container port",
        )
    else:
        _echo_finding(
            "warning",
            "Ensure the service listens on 0.0.0.0 and declares an HTTP container port",
        )
    if diagnosis.language == "Python":
        if diagnosis.python_has_pyproject:
            _echo_finding("ok", "Python project metadata found: pyproject.toml")
        else:
            _echo_finding(
                "error",
                "Python project metadata: add pyproject.toml with "
                "requires-python and dependencies",
            )
        if diagnosis.python_source_uses_sdk and diagnosis.sdk is None:
            _echo_finding(
                "error",
                "Python RoomClient SDK dependency: add meshagent-api "
                "to project metadata",
            )
        if diagnosis.python_sdk_versions:
            for source, version in diagnosis.python_sdk_versions:
                if version == MESHAGENT_CLIENT_VERSION:
                    _echo_finding(
                        "ok",
                        "Python meshagent-api version matches meshagent "
                        f"client: {version} ({source})",
                    )
                    continue
                _echo_finding(
                    "warning",
                    f"Python meshagent-api version mismatch: {source} has "
                    f"{version}, meshagent client is {MESHAGENT_CLIENT_VERSION}",
                )
        elif diagnosis.sdk == PYTHON_SDK_PACKAGE_NAME:
            _echo_finding(
                "warning",
                "Python meshagent-api dependency is not pinned; pin "
                f"meshagent-api=={MESHAGENT_CLIENT_VERSION} to match this "
                "meshagent client",
            )
        if diagnosis.python_runtime_findings:
            _echo_finding(
                "warning",
                f"Upgrade Python runtime metadata to "
                f"{PYTHON_REQUIRED_VERSION} before deploying",
            )
            for finding in diagnosis.python_runtime_findings:
                typer.echo(f"    - {finding}")
        else:
            _echo_finding(
                "ok",
                f"Python Dockerfile check targets Python {PYTHON_REQUIRED_VERSION}",
            )
        if diagnosis.python_virtualenv_versions:
            if any(
                _python_version_is_required(version)
                for _, version in diagnosis.python_virtualenv_versions
            ):
                _echo_finding(
                    "ok",
                    f"Local Python {PYTHON_REQUIRED_VERSION} virtual "
                    "environment detected",
                )
            else:
                _echo_finding(
                    "warning",
                    f"No local Python {PYTHON_REQUIRED_VERSION} "
                    "virtual environment detected",
                )
            for path, version in diagnosis.python_virtualenv_versions:
                typer.echo(
                    f"    - Local virtual environment `{path}` uses Python `{version}`"
                )
        else:
            _echo_finding("warning", "No local Python virtual environment detected")
    if diagnosis.language != "Python" and diagnosis.sdk_versions:
        for source, version in diagnosis.sdk_versions:
            if _version_is_behind(version, MESHAGENT_CLIENT_VERSION):
                _echo_finding(
                    "warning",
                    f"{diagnosis.sdk} version is behind meshagent client: "
                    f"{source} has {version}, meshagent client is "
                    f"{MESHAGENT_CLIENT_VERSION}",
                )
            else:
                _echo_finding(
                    "ok",
                    f"{diagnosis.sdk} version is not behind meshagent client: "
                    f"{version} ({source})",
                )
    if _is_javascript_project(diagnosis.language):
        scripts = dict(diagnosis.package_scripts)
        if not _is_static_javascript_flavor(diagnosis.javascript_flavor):
            if "start" in scripts:
                _echo_finding("ok", f"package.json start script: {scripts['start']}")
            else:
                _echo_finding(
                    "warning", "Add a package.json start script for production"
                )
        if diagnosis.javascript_flavor in {"React", "React/Vite", "Vite", "Next.js"}:
            if "build" in scripts:
                _echo_finding("ok", f"package.json build script: {scripts['build']}")
            else:
                _echo_finding("warning", "Add a package.json build script")
    deploy_token_finding = _javascript_roomclient_deploy_token_finding(diagnosis)
    if deploy_token_finding is not None:
        level, message = deploy_token_finding
        _echo_finding(level, message)
    typer.echo("")

    typer.echo("Recommended next steps:")
    next_step_number = 1
    if auto_fixes:
        typer.echo(f"{next_step_number}. Auto-fix project files:")
        typer.echo("   meshagent doctor --fix")
        for fix in auto_fixes:
            typer.echo(
                f"   - {fix.description}: {_display_path(diagnosis.root, fix.path)}"
            )
        next_step_number += 1
    if not diagnosis.has_deployment_artifact and diagnosis.dockerfile != "":
        typer.echo(f"{next_step_number}. Add a Dockerfile like:")
        typer.echo("")
        typer.echo(textwrap.indent(diagnosis.dockerfile, "   "))
        typer.echo("")
        next_step_number += 1
    sdk_checks = _sdk_checks(diagnosis)
    if sdk_checks:
        typer.echo(f"{next_step_number}. SDK checks:")
        for item in sdk_checks:
            typer.echo(f"   - {item}")
        next_step_number += 1
    checks = _deployment_checks(diagnosis)
    if not _has_deploy_spec(diagnosis.root):
        checks.append(
            "Deploy spec check: add `.meshagent/deploy.yaml` so `meshagent deploy` "
            "can prompt for template variables and save `.meshagent/values.yaml`; "
            "run `meshagent create` to see deploy spec examples."
        )
    if checks:
        typer.echo(f"{next_step_number}. Deployment checks:")
        for item in checks:
            typer.echo(f"   - {item}")
        next_step_number += 1
    typer.echo(f"{next_step_number}. Re-run doctor after changes:")
    typer.echo("   meshagent doctor")
    typer.echo("   Address remaining findings before deploying.")
    next_step_number += 1
    typer.echo(f"{next_step_number}. Deploy from this directory:")
    typer.echo(f"   {_deploy_command(diagnosis)}")


def _print_fix_report(*, diagnosis: ProjectDiagnosis) -> None:
    typer.echo("MeshAgent doctor --fix")
    typer.echo(f"Project: {diagnosis.root}")
    typer.echo("")
    fixes = _apply_auto_fixes(diagnosis)
    if not fixes:
        typer.echo("No auto-fixable missing files were found.")
        typer.echo("")
        typer.echo("Next step:")
        typer.echo("  Run `meshagent doctor` and address remaining findings.")
        return

    typer.echo("Applied fixes:")
    for fix in fixes:
        typer.echo(f"  - {fix.description}: {_display_path(diagnosis.root, fix.path)}")
    typer.echo("")
    typer.echo("Next step:")
    typer.echo("  Run `meshagent doctor` and address remaining findings.")


app = async_typer.AsyncTyper(add_completion=False)


@app.command(
    "doctor", help="Inspect the current directory for MeshAgent deployment gaps."
)
def _doctor_command(
    path: Annotated[
        Path | None,
        typer.Argument(exists=True, file_okay=False, dir_okay=True),
    ] = None,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help=(
                "Create obvious missing project files such as Dockerfile or "
                "pyproject.toml."
            ),
        ),
    ] = False,
) -> None:
    """Inspect a project directory and print deploy readiness checks."""

    diagnosis = diagnose_project(path or Path.cwd())
    if fix:
        _print_fix_report(diagnosis=diagnosis)
        return

    _print_report(diagnosis)


doctor_command = async_typer.get_command(app)
