from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
import shlex
import sys
from typing import Annotated, Literal, Mapping, Sequence

import typer
from typer import _click as typer_click

from meshagent.cli import async_typer
from meshagent.cli.meshagent_images import render_meshagent_image_prefix_template
from meshagent.cli.version import __version__ as MESHAGENT_CLIENT_VERSION


WEB_FOCUS = "webserver"
AGENT_FOCUS = "backend-agent"
CHATBOT_FOCUS = "chatbot"
ANTHROPIC_CHATBOT_FOCUS = "chatbot-anthropic"
CHATBOT_UI_FOCUS = "chatbot-ui"
ROOM_CHAT_FOCUS = "room-chat"
ROOM_WORKSPACE_FOCUS = "room-workspace"
CONTACT_FORM_FOCUS = "contact-form"
TASK_QUEUE_DASHBOARD_FOCUS = "task-queue-dashboard"
TELEGRAM_CHANNEL_FOCUS = "telegram-channel"
SLACK_CHANNEL_FOCUS = "slack-channel"
TWILIO_CHANNEL_FOCUS = "twilio-channel"
WHATSAPP_CHANNEL_FOCUS = "whatsapp-channel"
DEFAULT_LANGUAGE = "python"
DEFAULT_FOCUS = AGENT_FOCUS
CREATE_TEMPLATE_PACKAGE = "meshagent.cli.create_project_templates"
CREATE_TEMPLATE_VERSION_PLACEHOLDER = "__MESHAGENT_CLIENT_VERSION__"
CREATE_TEMPLATE_TELEGRAM_VERSION_PLACEHOLDER = "__MESHAGENT_TELEGRAM_VERSION__"
CREATE_TEMPLATE_SLACK_CHANNEL_VERSION_PLACEHOLDER = (
    "__MESHAGENT_SLACK_CHANNEL_VERSION__"
)
CREATE_TEMPLATE_TWILIO_VERSION_PLACEHOLDER = "__MESHAGENT_TWILIO_VERSION__"
CREATE_TEMPLATE_WHATSAPP_VERSION_PLACEHOLDER = "__MESHAGENT_WHATSAPP_VERSION__"
MESHAGENT_TELEGRAM_VERSION = "0.46.3"
MESHAGENT_SLACK_CHANNEL_VERSION = "0.46.3"
MESHAGENT_TWILIO_VERSION = "0.46.3"
MESHAGENT_WHATSAPP_VERSION = "0.46.3"


@dataclass(frozen=True, slots=True)
class CreateTemplate:
    language_id: str
    focus_id: str
    label: str
    description: str
    template_dir: str
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
    action: Literal["create-subfolder"]
    subfolder_name: str | None = None


WEBSERVER_NEXT_STEPS = (
    "./scripts/install.sh",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
STATIC_WEBSERVER_NEXT_STEPS = (
    "./scripts/install.sh",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
AGENT_NEXT_STEPS = (
    "./scripts/install.sh",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
PYTHON_WEBSERVER_NEXT_STEPS = (
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
PYTHON_AGENT_NEXT_STEPS = (
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
NPM_WEBSERVER_NEXT_STEPS = (
    "npm run dev",
    "npm run deploy",
)
NPM_STATIC_WEBSERVER_NEXT_STEPS = (
    "npm run dev",
    "npm run deploy",
)
NPM_CHATBOT_UI_NEXT_STEPS = (
    "npm run dev",
    "npm run deploy",
)
CONTACT_FORM_NEXT_STEPS = (
    "./scripts/dev.sh",
    "CONTACT_FORM_TO=you@example.com ./scripts/deploy.sh",
)
TASK_QUEUE_DASHBOARD_NEXT_STEPS = (
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
TELEGRAM_CHANNEL_NEXT_STEPS = (
    "./scripts/configure-telegram.sh",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
SLACK_CHANNEL_NEXT_STEPS = (
    "./scripts/configure-slack.sh",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
TWILIO_CHANNEL_NEXT_STEPS = (
    "cp .env.example .env",
    "${EDITOR:-nano} .env",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
WHATSAPP_CHANNEL_NEXT_STEPS = (
    "cp .env.example .env",
    "${EDITOR:-nano} .env",
    "./scripts/dev.sh",
    "./scripts/deploy.sh",
)
NPM_AGENT_NEXT_STEPS = (
    "npm run dev",
    "npm run deploy",
)
AGENT_TOOLKIT_NAMES = {
    "python": "meshagent.create.python-agent",
    "javascript": "meshagent.create.javascript-agent",
    "typescript": "meshagent.create.typescript-agent",
    "dotnet": "meshagent.create.dotnet-agent",
    "dart-flutter": "meshagent.create.dart-agent",
}
AGENT_PROCESS_NAMES = {
    "python": "meshagent-create-python-agent",
    "javascript": "meshagent-create-javascript-agent",
    "typescript": "meshagent-create-typescript-agent",
    "dotnet": "meshagent-create-dotnet-agent",
    "dart-flutter": "meshagent-create-dart-agent",
}
TEMPLATE_IGNORED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".npm-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
TEMPLATE_IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".gitkeep",
    "package-lock.json",
}
TEMPLATE_IGNORED_SUFFIXES = {
    ".pyc",
    ".pyo",
}

LANGUAGES: Mapping[str, CreateLanguage] = {
    "python": CreateLanguage(
        id="python",
        label="Python",
        description="Python 3.13 services and agents.",
    ),
    "javascript": CreateLanguage(
        id="javascript",
        label="JavaScript",
        description="Node.js CommonJS services and agents.",
    ),
    "typescript": CreateLanguage(
        id="typescript",
        label="TypeScript",
        description="Node.js TypeScript services, agents, and chat apps.",
    ),
    "react": CreateLanguage(
        id="react",
        label="React",
        description="React/Vite browser app.",
    ),
    "dotnet": CreateLanguage(
        id="dotnet",
        label=".NET",
        description=".NET service or agent.",
    ),
    "dart-flutter": CreateLanguage(
        id="dart-flutter",
        label="Dart/Flutter",
        description="Flutter Web App or Dart Agent Toolkit.",
    ),
}

FOCUSES: Mapping[str, CreateFocus] = {
    WEB_FOCUS: CreateFocus(
        id=WEB_FOCUS,
        label="Web App",
        description="Public HTTP service with a health endpoint.",
    ),
    AGENT_FOCUS: CreateFocus(
        id=AGENT_FOCUS,
        label="Agent Toolkit",
        description="Expose custom functionality to agents in the room.",
    ),
    CHATBOT_FOCUS: CreateFocus(
        id=CHATBOT_FOCUS,
        label="OpenAI Chatbot",
        description="Browser chat app backed by the room OpenAI proxy.",
    ),
    ANTHROPIC_CHATBOT_FOCUS: CreateFocus(
        id=ANTHROPIC_CHATBOT_FOCUS,
        label="Anthropic Chatbot",
        description="Browser chat app backed by the room Anthropic proxy.",
    ),
    CHATBOT_UI_FOCUS: CreateFocus(
        id=CHATBOT_UI_FOCUS,
        label="Agent UI",
        description="Browser chat interface for a deployed MeshAgent agent.",
    ),
    ROOM_CHAT_FOCUS: CreateFocus(
        id=ROOM_CHAT_FOCUS,
        label="Room Chat",
        description="Browser multi-user chat backed by the room messaging API.",
    ),
    ROOM_WORKSPACE_FOCUS: CreateFocus(
        id=ROOM_WORKSPACE_FOCUS,
        label="Room Workspace",
        description="Browser room app with chat, meetings, and files.",
    ),
    CONTACT_FORM_FOCUS: CreateFocus(
        id=CONTACT_FORM_FOCUS,
        label="Contact Form",
        description="Public HTML contact form that sends email through a room mailbox.",
    ),
    TASK_QUEUE_DASHBOARD_FOCUS: CreateFocus(
        id=TASK_QUEUE_DASHBOARD_FOCUS,
        label="Task Queue Dashboard",
        description="Public dashboard backed by a scheduled queue worker.",
    ),
    TELEGRAM_CHANNEL_FOCUS: CreateFocus(
        id=TELEGRAM_CHANNEL_FOCUS,
        label="Telegram Channel",
        description="Telegram account channel for a process-backed room agent.",
    ),
    SLACK_CHANNEL_FOCUS: CreateFocus(
        id=SLACK_CHANNEL_FOCUS,
        label="Slack Channel",
        description="Slack Events API channel for a process-backed room agent.",
    ),
    TWILIO_CHANNEL_FOCUS: CreateFocus(
        id=TWILIO_CHANNEL_FOCUS,
        label="Twilio Channel",
        description="Twilio SMS/MMS channel for a process-backed room agent.",
    ),
    WHATSAPP_CHANNEL_FOCUS: CreateFocus(
        id=WHATSAPP_CHANNEL_FOCUS,
        label="WhatsApp Channel",
        description="WhatsApp Cloud API channel for a process-backed room agent.",
    ),
}


def _template_dir(language_id: str, focus_id: str) -> str:
    return f"{language_id}/{focus_id}"


TEMPLATES: Mapping[tuple[str, str], CreateTemplate] = {
    ("python", WEB_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=WEB_FOCUS,
        label="Python Web App",
        description="Async Python public HTTP service with a health route.",
        template_dir=_template_dir("python", WEB_FOCUS),
        next_steps=PYTHON_WEBSERVER_NEXT_STEPS,
    ),
    ("python", AGENT_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=AGENT_FOCUS,
        label="Python Agent Toolkit",
        description="Headless Python service that exposes custom tools to agents.",
        template_dir=_template_dir("python", AGENT_FOCUS),
        next_steps=PYTHON_AGENT_NEXT_STEPS,
    ),
    ("python", CONTACT_FORM_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=CONTACT_FORM_FOCUS,
        label="Python Contact Form",
        description="Public aiohttp contact form that sends email through room SMTP.",
        template_dir=_template_dir("python", CONTACT_FORM_FOCUS),
        next_steps=CONTACT_FORM_NEXT_STEPS,
    ),
    ("python", TASK_QUEUE_DASHBOARD_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=TASK_QUEUE_DASHBOARD_FOCUS,
        label="Python Task Queue Dashboard",
        description="Python dashboard that schedules text work onto a room queue.",
        template_dir=_template_dir("python", TASK_QUEUE_DASHBOARD_FOCUS),
        next_steps=TASK_QUEUE_DASHBOARD_NEXT_STEPS,
    ),
    ("python", TELEGRAM_CHANNEL_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=TELEGRAM_CHANNEL_FOCUS,
        label="Python Telegram Channel",
        description="Runs a Telegram-backed command channel for a MeshAgent process agent. Incoming Telegram messages become trusted user turns, and completed agent responses are sent back to the same Telegram chat.",
        template_dir=_template_dir("python", TELEGRAM_CHANNEL_FOCUS),
        next_steps=TELEGRAM_CHANNEL_NEXT_STEPS,
    ),
    ("python", SLACK_CHANNEL_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=SLACK_CHANNEL_FOCUS,
        label="Python Slack Channel",
        description="Runs a Slack-backed command channel for a MeshAgent process agent. Validated Slack Events API requests become trusted user turns, and completed agent responses are sent back through Slack chat.postMessage.",
        template_dir=_template_dir("python", SLACK_CHANNEL_FOCUS),
        next_steps=SLACK_CHANNEL_NEXT_STEPS,
    ),
    ("python", TWILIO_CHANNEL_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=TWILIO_CHANNEL_FOCUS,
        label="Python Twilio Channel",
        description="Runs a Twilio-backed SMS/MMS command channel for a MeshAgent process agent. Validated Twilio webhooks become trusted user turns, and completed agent responses are sent back through the Twilio Messages API.",
        template_dir=_template_dir("python", TWILIO_CHANNEL_FOCUS),
        next_steps=TWILIO_CHANNEL_NEXT_STEPS,
    ),
    ("python", WHATSAPP_CHANNEL_FOCUS): CreateTemplate(
        language_id="python",
        focus_id=WHATSAPP_CHANNEL_FOCUS,
        label="Python WhatsApp Channel",
        description="Runs a WhatsApp Cloud API command channel for a MeshAgent process agent. Validated Meta webhooks become trusted user turns, and completed agent responses are sent back through the WhatsApp Cloud API.",
        template_dir=_template_dir("python", WHATSAPP_CHANNEL_FOCUS),
        next_steps=WHATSAPP_CHANNEL_NEXT_STEPS,
    ),
    ("javascript", WEB_FOCUS): CreateTemplate(
        language_id="javascript",
        focus_id=WEB_FOCUS,
        label="JavaScript Web App",
        description="Node.js public HTTP service with a health route.",
        template_dir=_template_dir("javascript", WEB_FOCUS),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("javascript", AGENT_FOCUS): CreateTemplate(
        language_id="javascript",
        focus_id=AGENT_FOCUS,
        label="JavaScript Agent Toolkit",
        description="Headless Node.js service that exposes custom tools to agents.",
        template_dir=_template_dir("javascript", AGENT_FOCUS),
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("typescript", WEB_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=WEB_FOCUS,
        label="TypeScript Web App",
        description="TypeScript public HTTP service with a health route.",
        template_dir=_template_dir("typescript", WEB_FOCUS),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", AGENT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=AGENT_FOCUS,
        label="TypeScript Agent Toolkit",
        description="Headless TypeScript service that exposes custom tools to agents.",
        template_dir=_template_dir("typescript", AGENT_FOCUS),
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("typescript", CHATBOT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=CHATBOT_FOCUS,
        label="TypeScript OpenAI Chatbot",
        description="Browser chatbot backed by the room OpenAI proxy.",
        template_dir=_template_dir("typescript", CHATBOT_FOCUS),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", ANTHROPIC_CHATBOT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=ANTHROPIC_CHATBOT_FOCUS,
        label="TypeScript Anthropic Chatbot",
        description="Browser chatbot backed by the room Anthropic proxy.",
        template_dir=_template_dir("typescript", ANTHROPIC_CHATBOT_FOCUS),
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("react", WEB_FOCUS): CreateTemplate(
        language_id="react",
        focus_id=WEB_FOCUS,
        label="React/Vite Web App",
        description="React/Vite browser app served as a public route.",
        template_dir=_template_dir("react", WEB_FOCUS),
        next_steps=NPM_STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", CHATBOT_UI_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=CHATBOT_UI_FOCUS,
        label="TypeScript Agent UI",
        description="Browser chat interface for a deployed MeshAgent agent.",
        template_dir=_template_dir("typescript", CHATBOT_UI_FOCUS),
        next_steps=NPM_CHATBOT_UI_NEXT_STEPS,
    ),
    ("typescript", ROOM_CHAT_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=ROOM_CHAT_FOCUS,
        label="TypeScript Room Chat",
        description="Browser multi-user chat backed by the room messaging API.",
        template_dir=_template_dir("typescript", ROOM_CHAT_FOCUS),
        next_steps=NPM_CHATBOT_UI_NEXT_STEPS,
    ),
    ("typescript", ROOM_WORKSPACE_FOCUS): CreateTemplate(
        language_id="typescript",
        focus_id=ROOM_WORKSPACE_FOCUS,
        label="TypeScript Room Workspace",
        description="Browser room app with chat, meetings, and files.",
        template_dir="typescript/room-workspace",
        next_steps=NPM_CHATBOT_UI_NEXT_STEPS,
    ),
    ("dotnet", WEB_FOCUS): CreateTemplate(
        language_id="dotnet",
        focus_id=WEB_FOCUS,
        label=".NET Web App",
        description="ASP.NET Core public HTTP service with a health route.",
        template_dir=_template_dir("dotnet", WEB_FOCUS),
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", AGENT_FOCUS): CreateTemplate(
        language_id="dotnet",
        focus_id=AGENT_FOCUS,
        label=".NET Agent Toolkit",
        description="Headless .NET service that exposes custom tools to agents.",
        template_dir=_template_dir("dotnet", AGENT_FOCUS),
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("dart-flutter", WEB_FOCUS): CreateTemplate(
        language_id="dart-flutter",
        focus_id=WEB_FOCUS,
        label="Flutter Web App",
        description="Flutter browser app served as a public route.",
        template_dir=_template_dir("dart-flutter", WEB_FOCUS),
        next_steps=STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dart-flutter", AGENT_FOCUS): CreateTemplate(
        language_id="dart-flutter",
        focus_id=AGENT_FOCUS,
        label="Dart Agent Toolkit",
        description="Headless Dart service that exposes custom tools to agents.",
        template_dir=_template_dir("dart-flutter", AGENT_FOCUS),
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
    "agent-toolkit": AGENT_FOCUS,
    "agent_toolkit": AGENT_FOCUS,
    "room-agent": AGENT_FOCUS,
    "room_agent": AGENT_FOCUS,
    "anthropic-chat": ANTHROPIC_CHATBOT_FOCUS,
    "anthropic-chatbot": ANTHROPIC_CHATBOT_FOCUS,
    "chat": CHATBOT_FOCUS,
    "chatbot": CHATBOT_FOCUS,
    "openai-chat": CHATBOT_FOCUS,
    "openai-chatbot": CHATBOT_FOCUS,
    "chatbot-anthropic": ANTHROPIC_CHATBOT_FOCUS,
    "chatbot_anthropic": ANTHROPIC_CHATBOT_FOCUS,
    "agent-ui": CHATBOT_UI_FOCUS,
    "agent_ui": CHATBOT_UI_FOCUS,
    "chat-ui": CHATBOT_UI_FOCUS,
    "chat_ui": CHATBOT_UI_FOCUS,
    "chatbot-ui": CHATBOT_UI_FOCUS,
    "chatbot_ui": CHATBOT_UI_FOCUS,
    "multi-user-chat": ROOM_CHAT_FOCUS,
    "multi_user_chat": ROOM_CHAT_FOCUS,
    "room-chat": ROOM_CHAT_FOCUS,
    "room_chat": ROOM_CHAT_FOCUS,
    "room-ui": ROOM_CHAT_FOCUS,
    "room_ui": ROOM_CHAT_FOCUS,
    "room_workspace": ROOM_WORKSPACE_FOCUS,
    "room-workspace": ROOM_WORKSPACE_FOCUS,
    "powerboards": ROOM_WORKSPACE_FOCUS,
    "powerboards-react": ROOM_WORKSPACE_FOCUS,
    "contact": CONTACT_FORM_FOCUS,
    "contact-form": CONTACT_FORM_FOCUS,
    "contact_form": CONTACT_FORM_FOCUS,
    "contact-email": CONTACT_FORM_FOCUS,
    "contact_email": CONTACT_FORM_FOCUS,
    "email": CONTACT_FORM_FOCUS,
    "email-form": CONTACT_FORM_FOCUS,
    "email_form": CONTACT_FORM_FOCUS,
    "queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "queue-dashboard": TASK_QUEUE_DASHBOARD_FOCUS,
    "queue_dashboard": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled-queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled_queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled-task": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled_task": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled-task-queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "scheduled_task_queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "task-queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "task_queue": TASK_QUEUE_DASHBOARD_FOCUS,
    "task-queue-dashboard": TASK_QUEUE_DASHBOARD_FOCUS,
    "task_queue_dashboard": TASK_QUEUE_DASHBOARD_FOCUS,
    "telegram": TELEGRAM_CHANNEL_FOCUS,
    "telegram-account": TELEGRAM_CHANNEL_FOCUS,
    "telegram_account": TELEGRAM_CHANNEL_FOCUS,
    "telegram-agent": TELEGRAM_CHANNEL_FOCUS,
    "telegram_agent": TELEGRAM_CHANNEL_FOCUS,
    "telegram-bot": TELEGRAM_CHANNEL_FOCUS,
    "telegram_bot": TELEGRAM_CHANNEL_FOCUS,
    "telegram-channel": TELEGRAM_CHANNEL_FOCUS,
    "telegram_channel": TELEGRAM_CHANNEL_FOCUS,
    "slack": SLACK_CHANNEL_FOCUS,
    "slack-agent": SLACK_CHANNEL_FOCUS,
    "slack_agent": SLACK_CHANNEL_FOCUS,
    "slack-bot": SLACK_CHANNEL_FOCUS,
    "slack_bot": SLACK_CHANNEL_FOCUS,
    "slack-channel": SLACK_CHANNEL_FOCUS,
    "slack_channel": SLACK_CHANNEL_FOCUS,
    "twilio": TWILIO_CHANNEL_FOCUS,
    "twilio-agent": TWILIO_CHANNEL_FOCUS,
    "twilio_agent": TWILIO_CHANNEL_FOCUS,
    "twilio-bot": TWILIO_CHANNEL_FOCUS,
    "twilio_bot": TWILIO_CHANNEL_FOCUS,
    "twilio-channel": TWILIO_CHANNEL_FOCUS,
    "twilio_channel": TWILIO_CHANNEL_FOCUS,
    "twilio-sms": TWILIO_CHANNEL_FOCUS,
    "twilio_sms": TWILIO_CHANNEL_FOCUS,
    "whatsapp": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp-agent": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp_agent": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp-bot": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp_bot": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp-channel": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp_channel": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp-cloud": WHATSAPP_CHANNEL_FOCUS,
    "whatsapp_cloud": WHATSAPP_CHANNEL_FOCUS,
    "roomclient": AGENT_FOCUS,
    "room-client": AGENT_FOCUS,
    "web": WEB_FOCUS,
    "web-app": WEB_FOCUS,
    "web_app": WEB_FOCUS,
    "webserver": WEB_FOCUS,
    "web-server": WEB_FOCUS,
    "web_server": WEB_FOCUS,
    "webservering": WEB_FOCUS,
    "webserving": WEB_FOCUS,
}


def _target_directory_is_nonempty(root: Path) -> bool:
    return next(root.iterdir(), None) is not None


def _read_create_template(template_name: str) -> str:
    resource = _create_template_resource(template_name)
    return resource.read_text(encoding="utf-8")


def _create_template_resource(template_name: str) -> Traversable:
    resource = resources.files(CREATE_TEMPLATE_PACKAGE)
    for part in template_name.split("/"):
        resource = resource.joinpath(part)
    return resource


def _render_create_template(template_name: str) -> str:
    return render_meshagent_image_prefix_template(
        _read_create_template(template_name)
        .replace(
            CREATE_TEMPLATE_VERSION_PLACEHOLDER,
            MESHAGENT_CLIENT_VERSION,
        )
        .replace(
            CREATE_TEMPLATE_TELEGRAM_VERSION_PLACEHOLDER,
            MESHAGENT_TELEGRAM_VERSION,
        )
        .replace(
            CREATE_TEMPLATE_SLACK_CHANNEL_VERSION_PLACEHOLDER,
            MESHAGENT_SLACK_CHANNEL_VERSION,
        )
        .replace(
            CREATE_TEMPLATE_TWILIO_VERSION_PLACEHOLDER,
            MESHAGENT_TWILIO_VERSION,
        )
        .replace(
            CREATE_TEMPLATE_WHATSAPP_VERSION_PLACEHOLDER,
            MESHAGENT_WHATSAPP_VERSION,
        )
    )


def _write_file(path: Path, template_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_create_template(template_name), encoding="utf-8")
    if path.suffix == ".sh":
        path.chmod(0o755)


def _is_ignored_template_path(path: Path) -> bool:
    if any(part in TEMPLATE_IGNORED_DIR_NAMES for part in path.parts[:-1]):
        return True
    if path.name in TEMPLATE_IGNORED_FILE_NAMES:
        return True
    return path.suffix in TEMPLATE_IGNORED_SUFFIXES


def _walk_template_files(template_dir: str) -> tuple[str, ...]:
    root = _create_template_resource(template_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Create template directory not found: {template_dir}")

    files: list[str] = []

    def walk(resource: Traversable, relative_dir: Path) -> None:
        children = sorted(resource.iterdir(), key=lambda child: child.name)
        for child in children:
            relative_path = relative_dir / child.name
            if child.is_dir():
                if child.name not in TEMPLATE_IGNORED_DIR_NAMES:
                    walk(child, relative_path)
                continue
            if _is_ignored_template_path(relative_path):
                continue
            files.append(relative_path.as_posix())

    walk(root, Path())
    return tuple(files)


def _template_output_files(template: CreateTemplate) -> tuple[str, ...]:
    return _walk_template_files(template.template_dir)


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
        raise typer_click.exceptions.ClickException(
            f"Unsupported language: {language}.\nExpected one of: {expected}."
        )
    return language_id


def _resolve_focus_id(focus: str | None) -> str:
    if focus is None or focus.strip() == "":
        return DEFAULT_FOCUS

    normalized = focus.strip().lower()
    focus_id = FOCUS_ALIASES.get(normalized)
    if focus_id is None:
        expected = _supported_focus_text()
        raise typer_click.exceptions.ClickException(
            f"Unsupported focus: {focus}.\nExpected one of: {expected}."
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
    raise typer_click.exceptions.ClickException(
        "Unsupported template combination.\n"
        f"{language.label} does not support {focus_id}. "
        f"Supported focus: {supported_text}."
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


def _first_explanatory_readme_paragraph(markdown: str) -> str | None:
    paragraph_lines: list[str] = []
    in_fenced_block = False

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block:
            continue
        if line == "":
            if paragraph_lines:
                break
            continue
        if line.startswith("#"):
            if paragraph_lines:
                break
            continue

        paragraph_lines.append(line)

    if not paragraph_lines:
        return None
    return " ".join(" ".join(paragraph_lines).split())


def _template_choice_description(template: CreateTemplate) -> str:
    try:
        readme = _read_create_template(f"{template.template_dir}/README.md")
    except FileNotFoundError:
        return template.description

    return _first_explanatory_readme_paragraph(readme) or template.description


def _focus_choices() -> Sequence[tuple[str, str, str, tuple[tuple[str, str], ...]]]:
    return tuple(
        (
            focus.id,
            focus.label,
            focus.description,
            tuple(
                (template.language_id, _template_choice_description(template))
                for template in TEMPLATES.values()
                if template.focus_id == focus.id
            ),
        )
        for focus in FOCUSES.values()
    )


def _run_create_tui(
    *,
    language_choices: Sequence[tuple[str, str, str, tuple[str, ...]]],
    focus_choices: Sequence[
        tuple[str, str, str, Sequence[tuple[str, str]]] | tuple[str, str, str]
    ],
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
        CreateFocusChoice(
            id=focus_id,
            label=label,
            description=description,
            descriptions_by_language=tuple(descriptions_by_language),
        )
        for focus_id, label, description, descriptions_by_language in (
            (*choice, ()) if len(choice) == 3 else choice for choice in focus_choices
        )
    ]
    result = asyncio.run(run_create_wizard_tui(languages=languages, focuses=focuses))
    if result.status != "completed":
        return None
    if result.selected_language_id is None or result.selected_focus_id is None:
        return None
    return result.selected_language_id, result.selected_focus_id


def _run_existing_project_tui(*, root: Path) -> ExistingProjectSelection | None:
    from meshagent.cli.tui.create import run_existing_project_create_tui

    result = asyncio.run(run_existing_project_create_tui(root=root))
    if result.status != "completed" or result.action is None:
        return None
    return ExistingProjectSelection(
        action=result.action,
        subfolder_name=result.subfolder_name,
    )


def _validate_subfolder_name(folder_name: str | None) -> str:
    if folder_name is None:
        raise typer_click.exceptions.ClickException("Folder name cannot be empty.")

    resolved_folder_name = folder_name.strip()
    if resolved_folder_name == "":
        raise typer_click.exceptions.ClickException("Folder name cannot be empty.")
    if (
        resolved_folder_name in {".", ".."}
        or "/" in resolved_folder_name
        or "\\" in resolved_folder_name
    ):
        raise typer_click.exceptions.ClickException(
            "Folder name must be a single new subfolder name."
        )
    return resolved_folder_name


def _new_project_subfolder(root: Path, folder_name: str | None) -> Path:
    resolved_folder_name = _validate_subfolder_name(folder_name)
    target = root / resolved_folder_name
    if target.exists():
        raise typer_click.exceptions.ClickException(
            f"Subfolder already exists: {target}"
        )
    return target


def _write_template(root: Path, template: CreateTemplate) -> None:
    for name in _walk_template_files(template.template_dir):
        _write_file(root / name, f"{template.template_dir}/{name}")


def _next_step_sections(
    steps: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    section_specs = (
        ("Install dependencies", ("install",)),
        (
            "Configure environment",
            (".env", "configure-telegram", "configure-slack", "create-bot-token"),
        ),
        ("Create Telegram session", ("create-session",)),
        ("Create room", ("rooms create",)),
        ("Run locally", ("dev",)),
        ("Deploy", ("deploy",)),
    )
    sections: list[tuple[str, tuple[str, ...]]] = []
    matched_steps: set[str] = set()
    for title, keywords in section_specs:
        section_steps = tuple(
            step for step in steps if any(keyword in step for keyword in keywords)
        )
        if section_steps:
            sections.append((title, section_steps))
            matched_steps.update(section_steps)

    other_steps = tuple(step for step in steps if step not in matched_steps)
    if other_steps:
        sections.append(("Other", other_steps))
    return tuple(sections)


def _print_next_steps(
    steps: tuple[str, ...],
    *,
    enter_project_root: Path | None = None,
) -> None:
    typer.secho("Next steps:", fg="cyan", bold=True)
    if enter_project_root is not None:
        typer.secho(f"  cd {shlex.quote(str(enter_project_root))}", fg="green")
    for index, (title, section_steps) in enumerate(_next_step_sections(steps), start=1):
        if index > 1:
            typer.echo("")
        typer.secho(f"  {index}. {title}", fg="blue", bold=True)
        for step in section_steps:
            typer.secho(f"     {step}", fg="green")


def _paired_agent_deploy_command(template: CreateTemplate) -> str | None:
    if template.focus_id != AGENT_FOCUS:
        return None
    toolkit_name = AGENT_TOOLKIT_NAMES.get(template.language_id)
    agent_name = AGENT_PROCESS_NAMES.get(template.language_id)
    if toolkit_name is None or agent_name is None:
        return None
    rule = f"Use the {toolkit_name} toolkit to answer ping, status, and echo requests."
    return (
        "meshagent process deploy "
        "--room <room> "
        f"--agent-name {agent_name} "
        f"--require-toolkit {toolkit_name} "
        f"--rule {shlex.quote(rule)}"
    )


def _print_agent_toolkit_guidance(template: CreateTemplate) -> None:
    command = _paired_agent_deploy_command(template)
    if command is None:
        return
    typer.echo("")
    typer.secho(
        "To install an agent in your room that uses this tool run:",
        fg="cyan",
        bold=True,
    )
    typer.secho(f"  {command}", fg="green")


def _print_contact_form_email_guidance(template: CreateTemplate) -> None:
    if template.focus_id != CONTACT_FORM_FOCUS:
        return
    typer.echo("")
    typer.secho(
        "Email setup is handled by the deploy template:",
        fg="cyan",
        bold=True,
    )
    typer.echo(
        "  .meshagent/deploy.yaml injects CONTACT_FORM_FROM and CONTACT_FORM_TO into the service."
    )
    typer.echo(
        "  meshagent deploy creates or updates the public sender mailbox from CONTACT_FORM_FROM."
    )
    typer.echo(
        "  meshagent deploy prompts for the sender mailbox when CONTACT_FORM_FROM is not set."
    )
    typer.echo(
        "Set CONTACT_FORM_TO to the address that should receive submissions. "
        "Set CONTACT_FORM_FROM only when you want a specific sender mailbox on "
        "the MeshAgent mail domain."
    )
    typer.echo(
        "If deploy reports that the sender mailbox already routes to a different room, choose another room-specific local part."
    )


def _print_created_report(
    *,
    template: CreateTemplate,
    enter_project_root: Path | None = None,
) -> None:
    typer.echo("")
    typer.echo(f"Created a minimal deployable {template.label} project:")
    for name in _template_output_files(template):
        typer.echo(f"  {name}")
    typer.echo("")
    _print_next_steps(template.next_steps, enter_project_root=enter_project_root)
    _print_agent_toolkit_guidance(template)
    _print_contact_form_email_guidance(template)


app = async_typer.AsyncTyper(add_completion=False)


@app.command(
    "create",
    help="Create a minimal deployable project.",
)
def _create_command(
    path: Annotated[
        Path | None,
        typer.Argument(file_okay=False, dir_okay=True),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option(
            "--language",
            "-l",
            help=(
                "Template language for non-interactive use. "
                "Supported: python, javascript, typescript, react, dotnet, dart/flutter."
            ),
        ),
    ] = None,
    focus: Annotated[
        str | None,
        typer.Option(
            "--focus",
            help=(
                "Project focus for non-interactive use. Use stable IDs: webserver "
                "(Web App), backend-agent (Agent Toolkit), chatbot (OpenAI Chatbot), "
                "chatbot-anthropic (Anthropic Chatbot), chatbot-ui (Agent UI), "
                "room-chat (Room Chat), room-workspace (Room Workspace), or "
                "contact-form (Contact Form), task-queue-dashboard (Task Queue "
                "Dashboard), telegram-channel (Telegram Channel), "
                "slack-channel (Slack Channel), twilio-channel (Twilio Channel), "
                "or whatsapp-channel (WhatsApp Channel)."
            ),
        ),
    ] = None,
    interactive: Annotated[
        bool | None,
        typer.Option(
            "--interactive/--no-interactive",
            help=(
                "Run or bypass the interactive template picker. Defaults to interactive "
                "when attached to a TTY and language or focus is missing."
            ),
        ),
    ] = None,
) -> None:
    """Create a minimal project that can be deployed on MeshAgent."""

    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    typer.echo("meshagent create")
    typer.echo(f"Project: {root}")
    enter_project_root: Path | None = None

    is_interactive_stdio = _stdio_is_interactive()
    if interactive is True and not is_interactive_stdio:
        raise typer_click.exceptions.ClickException(
            "Interactive mode requires a TTY. Pass --language, --focus, and "
            "--no-interactive when running from a script."
        )

    if _target_directory_is_nonempty(root):
        if interactive is not False and is_interactive_stdio:
            existing_project_selection = _run_existing_project_tui(root=root)
            if existing_project_selection is None:
                typer.echo("Create canceled.")
                return

            root = _new_project_subfolder(
                root,
                existing_project_selection.subfolder_name,
            )
            root.mkdir(parents=True, exist_ok=False)
            enter_project_root = root
            typer.echo(f"New project: {root}")
        else:
            typer.echo("")
            typer.echo(
                "The target directory is not empty; treating it as an existing project."
            )
            typer.echo("No files were written.")
            typer.echo("")
            typer.echo("Recommended next step for existing projects:")
            typer.echo("  meshagent doctor")
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
            typer.echo("Create canceled.")
            return
        language_id, focus_id = selection
    else:
        language_id = _resolve_language_id(language)
        focus_id = _resolve_focus_id(focus)

    template = _resolve_template(language_id, focus_id)
    _write_template(root, template)
    _print_created_report(template=template, enter_project_root=enter_project_root)


create_command = async_typer.get_command(app)
