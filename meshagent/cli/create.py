from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import sys
from typing import Literal, Mapping, Sequence

import click

from meshagent.cli.meshagent_images import render_meshagent_image_prefix_template
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
PROJECT_MARKER_NAMES = {
    "Containerfile",
    "Dockerfile",
    "Gemfile",
    "go.mod",
    "meshagent.yaml",
    "meshagent.yml",
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".gitkeep",
}

WEB_FOCUS = "webserver"
AGENT_FOCUS = "backend-agent"
CHATBOT_FOCUS = "chatbot"
CHATBOT_UI_FOCUS = "chatbot-ui"
DEFAULT_LANGUAGE = "python"
DEFAULT_FOCUS = AGENT_FOCUS
CREATE_TEMPLATE_PACKAGE = "meshagent.cli.create_project_templates"
CREATE_TEMPLATE_VERSION_PLACEHOLDER = "__MESHAGENT_CLIENT_VERSION__"


@dataclass(frozen=True, slots=True)
class CreateTemplate:
    language_id: str
    focus_id: str
    label: str
    description: str
    files: Mapping[str, str]
    next_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateLanguage:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class CreateFocus:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class ExistingProjectSelection:
    action: Literal["run-doctor", "create-subfolder"]
    subfolder_name: str | None = None


WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
STATIC_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
AGENT_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
NPM_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)
NPM_STATIC_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)
NPM_CHATBOT_UI_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)
NPM_AGENT_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)

LANGUAGES: Mapping[str, CreateLanguage] = {
    "python": CreateLanguage(
        id="python",
        label="Python",
        description="Python 3.13.",
    ),
    "javascript": CreateLanguage(
        id="javascript",
        label="JavaScript",
        description="Node.js/CommonJS.",
    ),
    "typescript": CreateLanguage(
        id="typescript",
        label="TypeScript",
        description="Node.js/TypeScript.",
    ),
    "react": CreateLanguage(
        id="react",
        label="React",
        description="React/Vite.",
    ),
    "dotnet": CreateLanguage(
        id="dotnet",
        label=".NET",
        description=".NET.",
    ),
    "dart-flutter": CreateLanguage(
        id="dart-flutter",
        label="Dart/Flutter",
        description="Dart or Flutter.",
    ),
}

FOCUSES: Mapping[str, CreateFocus] = {
    WEB_FOCUS: CreateFocus(
        id=WEB_FOCUS,
        label="Web server",
        description="HTTP app with a health endpoint and public route.",
    ),
    AGENT_FOCUS: CreateFocus(
        id=AGENT_FOCUS,
        label="Backend agent",
        description="Headless RoomClient SDK service without a public port.",
    ),
    CHATBOT_FOCUS: CreateFocus(
        id=CHATBOT_FOCUS,
        label="Chatbot",
        description="TypeScript RoomClient chatbot with one chat tool.",
    ),
    CHATBOT_UI_FOCUS: CreateFocus(
        id=CHATBOT_UI_FOCUS,
        label="Chatbot UI",
        description="TypeScript/Next.js UI that chats with a MeshAgent assistant.",
    ),
}


def _template_files(
    language_id: str,
    focus_id: str,
    file_names: Sequence[str],
) -> Mapping[str, str]:
    return {
        file_name: f"{language_id}/{focus_id}/{file_name}" for file_name in file_names
    }


TEMPLATES: Mapping[tuple[str, str], CreateTemplate] = {
    ("python", WEB_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=WEB_FOCUS,
        label="Python web server",
        description="Async Python HTTP service on a declared container port.",
        files=_template_files(
            "python",
            WEB_FOCUS,
            (
                "pyproject.toml",
                "server.py",
                "dev-content.json",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("python", AGENT_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=AGENT_FOCUS,
        label="Python backend agent",
        description="Headless Python RoomClient service.",
        files=_template_files(
            "python",
            AGENT_FOCUS,
            (
                "pyproject.toml",
                "server.py",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("javascript", WEB_FOCUS): CreateTemplate(
        language_id="javascript",
        focus_id=WEB_FOCUS,
        label="JavaScript web server",
        description="Node.js HTTP service on a declared container port.",
        files=_template_files(
            "javascript",
            WEB_FOCUS,
            (
                "package.json",
                ".npmrc",
                "server.js",
                "dev-content.json",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("javascript", AGENT_FOCUS): CreateTemplate(
        language_id="javascript",
        focus_id=AGENT_FOCUS,
        label="JavaScript backend agent",
        description="Headless Node.js RoomClient service.",
        files=_template_files(
            "javascript",
            AGENT_FOCUS,
            (
                "package.json",
                ".npmrc",
                "server.js",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("typescript", WEB_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=WEB_FOCUS,
        label="TypeScript web server",
        description="Node.js TypeScript HTTP service on a declared container port.",
        files=_template_files(
            "typescript",
            WEB_FOCUS,
            (
                "package.json",
                ".npmrc",
                "tsconfig.json",
                "src/server.ts",
                "src/dev-content.json",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", AGENT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=AGENT_FOCUS,
        label="TypeScript backend agent",
        description="Headless TypeScript RoomClient service.",
        files=_template_files(
            "typescript",
            AGENT_FOCUS,
            (
                "package.json",
                ".npmrc",
                "tsconfig.json",
                "src/server.ts",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("typescript", CHATBOT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=CHATBOT_FOCUS,
        label="TypeScript chatbot",
        description="Headless TypeScript RoomClient chatbot.",
        files={
            "package.json": "typescript/chatbot/package.json",
            ".npmrc": "typescript/backend-agent/.npmrc",
            "tsconfig.json": "typescript/backend-agent/tsconfig.json",
            "src/server.ts": "typescript/chatbot/src/server.ts",
            "Dockerfile": "typescript/backend-agent/Dockerfile",
            ".dockerignore": "typescript/backend-agent/.dockerignore",
        },
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("react", WEB_FOCUS): CreateTemplate(
        language_id="react",
        focus_id=WEB_FOCUS,
        label="React web server",
        description="React/Vite web app served by nginx on a declared container port.",
        files=_template_files(
            "react",
            WEB_FOCUS,
            (
                "package.json",
                ".npmrc",
                "tsconfig.json",
                "vite.config.ts",
                "index.html",
                "scripts/dev-content-toolkit.js",
                "src/dev-content.json",
                "src/main.tsx",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", CHATBOT_UI_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=CHATBOT_UI_FOCUS,
        label="TypeScript chatbot UI",
        description="TypeScript/Next.js UI that chats with a MeshAgent assistant.",
        files=_template_files(
            "typescript",
            CHATBOT_UI_FOCUS,
            (
                "package.json",
                ".npmrc",
                "tsconfig.json",
                "next.config.ts",
                "next-env.d.ts",
                "app/layout.tsx",
                "app/page.tsx",
                "app/globals.css",
                "app/health/route.ts",
                "Dockerfile",
                ".dockerignore",
            ),
        ),
        next_steps=NPM_CHATBOT_UI_NEXT_STEPS,
    ),
    ("dotnet", WEB_FOCUS): CreateTemplate(
        language_id="dotnet",
        focus_id=WEB_FOCUS,
        label=".NET web server",
        description="ASP.NET Core HTTP service on a declared container port.",
        files=_template_files(
            "dotnet",
            WEB_FOCUS,
            (
                "MeshAgentHello.csproj",
                "Program.cs",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", AGENT_FOCUS): CreateTemplate(
        language_id="dotnet",
        focus_id=AGENT_FOCUS,
        label=".NET backend agent",
        description="Headless .NET RoomClient service.",
        files=_template_files(
            "dotnet",
            AGENT_FOCUS,
            (
                "MeshAgentHello.csproj",
                "Program.cs",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("dart-flutter", WEB_FOCUS): CreateTemplate(
        language_id="dart-flutter",
        focus_id=WEB_FOCUS,
        label="Flutter web server",
        description="Flutter web app served by nginx on a declared container port.",
        files=_template_files(
            "dart-flutter",
            WEB_FOCUS,
            (
                "pubspec.yaml",
                "lib/main.dart",
                "tool/dev_room_proof.dart",
                "web/index.html",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dart-flutter", AGENT_FOCUS): CreateTemplate(
        language_id="dart-flutter",
        focus_id=AGENT_FOCUS,
        label="Dart backend agent",
        description="Headless Dart RoomClient service.",
        files=_template_files(
            "dart-flutter",
            AGENT_FOCUS,
            (
                "pubspec.yaml",
                "bin/server.dart",
                "Dockerfile",
                ".dockerignore",
                "scripts/install.sh",
                "scripts/dev.sh",
                "scripts/deploy.sh",
            ),
        ),
        next_steps=AGENT_NEXT_STEPS,
    ),
}

LANGUAGE_ALIASES = {
    ".net": "dotnet",
    "c#": "dotnet",
    "csharp": "dotnet",
    "dart": "dart-flutter",
    "dart/flutter": "dart-flutter",
    "dart-flutter": "dart-flutter",
    "dotnet": "dotnet",
    "flutter": "dart-flutter",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "node.js": "javascript",
    "nodejs": "javascript",
    "python": "python",
    "py": "python",
    "react": "react",
    "react.js": "react",
    "reactjs": "react",
    "react-vite": "react",
    "ts": "typescript",
    "typescript": "typescript",
    "node-ts": "typescript",
    "node.ts": "typescript",
    "vite-react": "react",
}
FOCUS_ALIASES = {
    "agent": AGENT_FOCUS,
    "backend": AGENT_FOCUS,
    "backend-agent": AGENT_FOCUS,
    "backend_agent": AGENT_FOCUS,
    "chat": CHATBOT_FOCUS,
    "chatbot": CHATBOT_FOCUS,
    "chat-ui": CHATBOT_UI_FOCUS,
    "chat_ui": CHATBOT_UI_FOCUS,
    "chatbot-ui": CHATBOT_UI_FOCUS,
    "chatbot_ui": CHATBOT_UI_FOCUS,
    "roomclient": AGENT_FOCUS,
    "room-client": AGENT_FOCUS,
    "web": WEB_FOCUS,
    "webserver": WEB_FOCUS,
    "web-server": WEB_FOCUS,
    "web_server": WEB_FOCUS,
    "webservering": WEB_FOCUS,
    "webserving": WEB_FOCUS,
}


def _has_existing_project_content(root: Path) -> bool:
    for path in sorted(root.rglob("*")):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if path.is_dir():
            continue
        if path.name in IGNORED_FILE_NAMES:
            continue
        if path.name in PROJECT_MARKER_NAMES:
            return True
        if path.suffix.lower() in SOURCE_SUFFIXES:
            return True
    return False


def _read_create_template(template_name: str) -> str:
    resource = resources.files(CREATE_TEMPLATE_PACKAGE)
    for part in template_name.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


def _render_create_template(template_name: str) -> str:
    return render_meshagent_image_prefix_template(
        _read_create_template(template_name).replace(
            CREATE_TEMPLATE_VERSION_PLACEHOLDER,
            MESHAGENT_CLIENT_VERSION,
        )
    )


def _write_file(path: Path, template_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_create_template(template_name), encoding="utf-8")
    if path.suffix == ".sh":
        path.chmod(0o755)


def _supported_language_text() -> str:
    return ", ".join(language.id for language in LANGUAGES.values())


def _supported_focus_text() -> str:
    return ", ".join(focus.id for focus in FOCUSES.values())


def _resolve_language_id(language: str | None) -> str:
    if language is None or language.strip() == "":
        return DEFAULT_LANGUAGE

    normalized = language.strip().lower()
    language_id = LANGUAGE_ALIASES.get(normalized)
    if language_id is None:
        expected = _supported_language_text()
        raise click.ClickException(
            f"Unsupported language: {language}. Expected one of: {expected}."
        )
    return language_id


def _resolve_focus_id(focus: str | None) -> str:
    if focus is None or focus.strip() == "":
        return DEFAULT_FOCUS

    normalized = focus.strip().lower()
    focus_id = FOCUS_ALIASES.get(normalized)
    if focus_id is None:
        expected = _supported_focus_text()
        raise click.ClickException(
            f"Unsupported focus: {focus}. Expected one of: {expected}."
        )
    return focus_id


def _resolve_template(language_id: str, focus_id: str) -> CreateTemplate:
    template = TEMPLATES.get((language_id, focus_id))
    if template is not None:
        return template

    language = LANGUAGES[language_id]
    supported = [
        focus
        for template_language_id, focus in TEMPLATES
        if template_language_id == language_id
    ]
    supported_text = ", ".join(supported) if supported else "none"
    raise click.ClickException(
        f"Unsupported template combination: {language.label} does not support "
        f"{focus_id}. Supported focus: {supported_text}."
    )


def _stdio_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _should_launch_tui(
    *,
    language: str | None,
    focus: str | None,
    interactive: bool | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    if interactive is False:
        return False
    if interactive is True:
        return stdin_is_tty and stdout_is_tty
    return (language is None or focus is None) and stdin_is_tty and stdout_is_tty


def _supported_focus_ids_for_language(language_id: str) -> tuple[str, ...]:
    return tuple(
        focus_id
        for template_language_id, focus_id in TEMPLATES
        if template_language_id == language_id
    )


def _language_choices() -> Sequence[tuple[str, str, str, tuple[str, ...]]]:
    return tuple(
        (
            language.id,
            language.label,
            language.description,
            _supported_focus_ids_for_language(language.id),
        )
        for language in LANGUAGES.values()
    )


def _focus_choices() -> Sequence[tuple[str, str, str]]:
    return tuple(
        (focus.id, focus.label, focus.description) for focus in FOCUSES.values()
    )


def _run_create_tui(
    *,
    language_choices: Sequence[tuple[str, str, str, tuple[str, ...]]],
    focus_choices: Sequence[tuple[str, str, str]],
) -> tuple[str, str] | None:
    from meshagent.cli.tui.create import (
        CreateFocusChoice,
        CreateLanguageChoice,
        run_create_wizard_tui,
    )

    languages = [
        CreateLanguageChoice(
            id=language_id,
            label=label,
            description=description,
            focus_ids=focus_ids,
        )
        for language_id, label, description, focus_ids in language_choices
    ]
    focuses = [
        CreateFocusChoice(id=focus_id, label=label, description=description)
        for focus_id, label, description in focus_choices
    ]
    result = asyncio.run(run_create_wizard_tui(languages=languages, focuses=focuses))
    if result.status != "completed":
        return None
    if result.selected_language_id is None or result.selected_focus_id is None:
        return None
    return result.selected_language_id, result.selected_focus_id


def _run_existing_project_tui() -> ExistingProjectSelection | None:
    from meshagent.cli.tui.create import run_existing_project_create_tui

    result = asyncio.run(run_existing_project_create_tui())
    if result.status != "completed" or result.action is None:
        return None
    return ExistingProjectSelection(
        action=result.action,
        subfolder_name=result.subfolder_name,
    )


def _validate_subfolder_name(folder_name: str | None) -> str:
    if folder_name is None:
        raise click.ClickException("Folder name cannot be empty.")

    resolved_folder_name = folder_name.strip()
    if resolved_folder_name == "":
        raise click.ClickException("Folder name cannot be empty.")
    if (
        resolved_folder_name in {".", ".."}
        or "/" in resolved_folder_name
        or "\\" in resolved_folder_name
    ):
        raise click.ClickException("Folder name must be a single new subfolder name.")
    return resolved_folder_name


def _new_project_subfolder(root: Path, folder_name: str | None) -> Path:
    resolved_folder_name = _validate_subfolder_name(folder_name)
    target = root / resolved_folder_name
    if target.exists():
        raise click.ClickException(f"Subfolder already exists: {target}")
    return target


def _run_doctor(root: Path) -> None:
    from meshagent.cli.doctor import _print_report, diagnose_project

    _print_report(diagnose_project(root))


def _write_template(root: Path, template: CreateTemplate) -> None:
    for name, template_name in template.files.items():
        _write_file(root / name, template_name)


def _print_created_report(*, template: CreateTemplate) -> None:
    click.echo("")
    click.echo(f"Created a minimal deployable {template.label} project:")
    for name in template.files:
        click.echo(f"  {name}")
    click.echo("")
    click.echo("Next steps:")
    for step in template.next_steps:
        click.echo(f"  {step}")


@click.command(
    "create",
    help="Create a minimal deployable project.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=None,
    help=(
        "Template language for non-interactive use. "
        "Supported: python, javascript, typescript, react, dotnet, dart/flutter."
    ),
)
@click.option(
    "--focus",
    type=str,
    default=None,
    help=(
        "Project focus for non-interactive use. Supported: webserver, backend-agent."
        " TypeScript also supports chatbot and chatbot-ui."
    ),
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help=(
        "Run or bypass the interactive template picker. Defaults to interactive "
        "when attached to a TTY and language or focus is missing."
    ),
)
@click.argument(
    "path",
    required=False,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
def create_command(
    path: Path | None = None,
    language: str | None = None,
    focus: str | None = None,
    interactive: bool | None = None,
) -> None:
    """Create a minimal project that can be deployed on MeshAgent."""

    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    click.echo("meshagent create")
    click.echo(f"Project: {root}")

    is_interactive_stdio = _stdio_is_interactive()
    if interactive is True and not is_interactive_stdio:
        raise click.ClickException(
            "Interactive mode requires a TTY. Pass --language, --focus, and "
            "--no-interactive when running from a script."
        )

    if _has_existing_project_content(root):
        if interactive is not False and is_interactive_stdio:
            existing_project_selection = _run_existing_project_tui()
            if existing_project_selection is None:
                click.echo("Create canceled.")
                return
            if existing_project_selection.action == "run-doctor":
                click.echo("")
                _run_doctor(root)
                return

            root = _new_project_subfolder(
                root,
                existing_project_selection.subfolder_name,
            )
            root.mkdir(parents=True, exist_ok=False)
            click.echo(f"New project: {root}")
        else:
            click.echo("")
            click.echo("Existing application code or deployment metadata was detected.")
            click.echo("No files were written.")
            click.echo("")
            click.echo("Recommended next step for existing projects:")
            click.echo("  meshagent doctor")
            return

    if _should_launch_tui(
        language=language,
        focus=focus,
        interactive=interactive,
        stdin_is_tty=is_interactive_stdio,
        stdout_is_tty=is_interactive_stdio,
    ):
        selection = _run_create_tui(
            language_choices=_language_choices(),
            focus_choices=_focus_choices(),
        )
        if selection is None:
            click.echo("Create canceled.")
            return
        language_id, focus_id = selection
    else:
        language_id = _resolve_language_id(language)
        focus_id = _resolve_focus_id(focus)

    template = _resolve_template(language_id, focus_id)
    _write_template(root, template)
    _print_created_report(template=template)
