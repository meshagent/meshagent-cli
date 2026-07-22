from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import io
import inspect
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.messages import (
    AGENT_EVENT_AUDIO_GENERATION_COMPLETED,
    AGENT_EVENT_AUDIO_GENERATION_DELTA,
    AGENT_EVENT_AUDIO_GENERATION_FAILED,
    AGENT_EVENT_AUDIO_GENERATION_STARTED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_COMPLETED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_FAILED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_STARTED,
    AGENT_EVENT_CONTEXT_COMPACTED,
    AGENT_EVENT_FILE_CONTENT_DELTA,
    AGENT_EVENT_FILE_CONTENT_ENDED,
    AGENT_EVENT_FILE_CONTENT_STARTED,
    AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
    AGENT_EVENT_IMAGE_GENERATION_FAILED,
    AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
    AGENT_EVENT_IMAGE_GENERATION_STARTED,
    AGENT_EVENT_REASONING_CONTENT_DELTA,
    AGENT_EVENT_REASONING_CONTENT_ENDED,
    AGENT_EVENT_REASONING_CONTENT_STARTED,
    AGENT_EVENT_THREAD_EVENT,
    AGENT_EVENT_THREAD_STATUS,
    AGENT_EVENT_TEXT_CONTENT_DELTA,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_TEXT_CONTENT_STARTED,
    AGENT_EVENT_TOOL_CALL_ARGUMENTS_DELTA,
    AGENT_EVENT_TOOL_CALL_APPROVAL_REQUESTED,
    AGENT_EVENT_TOOL_CALL_ENDED,
    AGENT_EVENT_TOOL_CALL_IN_PROGRESS,
    AGENT_EVENT_TOOL_CALL_LOG_DELTA,
    AGENT_EVENT_TOOL_CALL_PENDING,
    AGENT_EVENT_TOOL_CALL_STARTED,
    AGENT_EVENT_TURN_ENDED,
    AGENT_EVENT_TURN_START_ACCEPTED,
    AGENT_EVENT_TURN_START_REJECTED,
    AGENT_EVENT_TURN_STEER_ACCEPTED,
    AGENT_EVENT_TURN_STEER_REJECTED,
    AGENT_EVENT_TURN_STEERED,
    AGENT_EVENT_TURN_STARTED,
    AGENT_EVENT_USAGE_UPDATED,
    AGENT_MESSAGE_TURN_INTERRUPT,
    AGENT_MESSAGE_THREAD_START,
    AGENT_MESSAGE_TURN_START,
    AGENT_MESSAGE_TURN_STEER,
    AgentAudioGenerationCompleted,
    AgentAudioGenerationDelta,
    AgentAudioGenerationFailed,
    AgentAudioGenerationStarted,
    AgentAudioTranscriptionCompleted,
    AgentAudioTranscriptionDelta,
    AgentAudioTranscriptionFailed,
    AgentAudioTranscriptionStarted,
    AgentContextCompacted,
    AgentFileContent,
    AgentFileContentDelta,
    AgentFileContentEnded,
    AgentFileContentStarted,
    AgentImageGenerationCompleted,
    AgentImageGenerationFailed,
    AgentImageGenerationPartial,
    AgentImageGenerationStarted,
    AgentMessage,
    AgentModelChanged,
    AgentReasoningContentDelta,
    AgentReasoningContentEnded,
    AgentReasoningContentStarted,
    AgentTextContent,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentTextContentStarted,
    AgentThreadStatus,
    AgentThreadEvent,
    AgentToolCallArgumentsDelta,
    AgentToolCallApprovalRequested,
    AgentToolCallEnded,
    AgentToolCallInProgress,
    AgentToolCallLogDelta,
    AgentToolCallPending,
    AgentToolCallStarted,
    AgentUsageUpdated,
    StartThread,
    TurnEnded,
    TurnInterrupt,
    TurnStart,
    TurnStartAccepted,
    TurnStartRejected,
    TurnStarted,
    TurnSteer,
    TurnSteerAccepted,
    TurnSteerRejected,
    TurnSteered,
    parse_agent_message,
)
from meshagent.agents.chat_client import (
    ChatThreadSession,
    LocalChatClient,
    MessagingChatClient,
)
from meshagent.agents.images_dataset import (
    ImageDatasetClient,
    ImageDatasetRecord,
)
from meshagent.agents.tool_call_accumulator import tool_arguments_from_delta_text
from meshagent.agents.process import (
    AgentSupervisor,
    LLMAgentProcess,
    Message,
    TurnInstructionsProvider,
)
from meshagent.api import Participant, RoomException
from meshagent.cli import async_typer, auth_async
from meshagent.cli._filedrop import (
    dropped_file_paths_from_text,
)
from meshagent.cli.common_options import ProjectIdOption
from meshagent.cli.create import CREATE_TEMPLATE_PACKAGE
from meshagent.cli.helper import resolve_project_id
from meshagent.cli.preamble_rules import DEFAULT_PREAMBLE_RULE
from meshagent.cli.tool_call_summary import format_tool_call_summary
from meshagent.tools import Toolkit
from meshagent.tools.storage import StorageToolLocalMount, StorageToolkit

_MESHAGENT_PROJECT_ID_HEADER = "Meshagent-Project-Id"
_MESHAGENT_TOKEN_ENV = "MESHAGENT_TOKEN"
_DEFAULT_ASK_MODEL = "gpt-5.6-sol"
_ASK_ACTIVE_STATUS_STATES = {"queued", "in_progress", "running", "pending"}
_ASK_TERMINAL_STATUS_STATES = {
    "completed",
    "complete",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
}
_ASK_TOOL_LOG_RENDER_LIMIT = 4
_ASK_IMAGE_RENDER_COLUMNS = 72
_ASK_PASTE_DEBUG_ENV = "MESHAGENT_ASK_PASTE_DEBUG"
_ASK_PASS_THROUGH_AGENT_EVENT_TYPES = {
    AGENT_EVENT_AUDIO_GENERATION_COMPLETED,
    AGENT_EVENT_AUDIO_GENERATION_DELTA,
    AGENT_EVENT_AUDIO_GENERATION_FAILED,
    AGENT_EVENT_AUDIO_GENERATION_STARTED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_COMPLETED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_FAILED,
    AGENT_EVENT_AUDIO_TRANSCRIPTION_STARTED,
    AGENT_EVENT_CONTEXT_COMPACTED,
    AGENT_EVENT_FILE_CONTENT_DELTA,
    AGENT_EVENT_FILE_CONTENT_ENDED,
    AGENT_EVENT_FILE_CONTENT_STARTED,
    AGENT_EVENT_IMAGE_GENERATION_COMPLETED,
    AGENT_EVENT_IMAGE_GENERATION_FAILED,
    AGENT_EVENT_IMAGE_GENERATION_PARTIAL,
    AGENT_EVENT_IMAGE_GENERATION_STARTED,
    AGENT_EVENT_REASONING_CONTENT_DELTA,
    AGENT_EVENT_REASONING_CONTENT_ENDED,
    AGENT_EVENT_REASONING_CONTENT_STARTED,
    AGENT_EVENT_TEXT_CONTENT_ENDED,
    AGENT_EVENT_TEXT_CONTENT_STARTED,
    AGENT_EVENT_THREAD_EVENT,
    AGENT_EVENT_TOOL_CALL_ARGUMENTS_DELTA,
    AGENT_EVENT_TOOL_CALL_APPROVAL_REQUESTED,
    AGENT_EVENT_TOOL_CALL_ENDED,
    AGENT_EVENT_TOOL_CALL_IN_PROGRESS,
    AGENT_EVENT_TOOL_CALL_LOG_DELTA,
    AGENT_EVENT_TOOL_CALL_PENDING,
    AGENT_EVENT_TOOL_CALL_STARTED,
}


@dataclass(frozen=True, slots=True)
class _AskImagePreview:
    data: bytes
    width_px: int
    height_px: int
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class _AskPdfPreview:
    name: str
    pages: tuple[_AskImagePreview, ...] = ()
    text: str | None = None


_ASK_SLASH_COMMANDS = (
    ("/new", "Start a new thread"),
    ("/model", "List or change the active model"),
    ("/output", "Change text or audio outputs"),
    ("/threads on | off", "Show or hide the thread sidebar"),
)


@dataclass(frozen=True, slots=True)
class AskCommandOption:
    command: str
    label: str
    description: str | None = None
    active: bool = False


class _StreamingAudioPlayer:
    def __init__(self, *, sample_rate: int = 24000, channels: int = 1) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._stream: Any | None = None
        self._disabled_error: str | None = None

    async def play_delta(self, data: bytes) -> str | None:
        if self._disabled_error is not None:
            return None
        if len(data) == 0:
            return None
        try:
            await asyncio.to_thread(self._write, data)
        except Exception as exc:
            self._disabled_error = str(exc)
            await self.close()
            return (
                "Unable to play voice response audio. "
                "Install or configure the sounddevice package/audio device. "
                f"({exc})"
            )
        return None

    def _write(self, data: bytes) -> None:
        if self._stream is None:
            import sounddevice as sd

            self._stream = sd.RawOutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",
                blocksize=0,
            )
            self._stream.start()
        self._stream.write(data)

    async def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        await asyncio.to_thread(self._close_stream, stream)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()


def _ask_tool_raw_label(*, toolkit: str, tool: str) -> str:
    normalized_tool = tool.strip() or "tool"
    normalized_toolkit = toolkit.strip()
    if normalized_toolkit != "" and normalized_toolkit != normalized_tool:
        return f"{normalized_toolkit}: {normalized_tool}"
    return normalized_tool


def _ask_tool_log_lines(logs: list[str]) -> list[str]:
    lines: list[str] = []
    for log in logs:
        for line in log.splitlines():
            stripped = line.strip()
            if stripped != "":
                lines.append(stripped)
    return lines


def _ask_log_headline(line: str) -> str:
    normalized = line.strip()
    if _ask_looks_like_path_only_log_line(normalized):
        return f"Output: {normalized}"
    return normalized


def _ask_looks_like_path_only_log_line(line: str) -> bool:
    if line == "" or " " in line:
        return False
    return line.startswith(("/", "./", "../", "~/"))


def _ask_log_lines_look_like_traceback(lines: list[str]) -> bool:
    if any(line.startswith("Traceback (most recent call last):") for line in lines):
        return True
    has_frame = any(
        line.startswith('File "') or line.startswith("File ") for line in lines
    )
    has_exception = any(
        "Exception:" in line
        or line.endswith("Exception")
        or line.endswith("Error")
        or "Error:" in line
        for line in lines
    )
    return has_frame and has_exception


def _ask_tool_error_line(error_message: str | None) -> str | None:
    if error_message is None:
        return None
    lines = [line.strip() for line in error_message.splitlines() if line.strip()]
    if len(lines) == 0:
        return None
    last_line = lines[-1]
    if ": " in last_line:
        prefix, message = last_line.split(": ", 1)
        if "." in prefix or prefix.endswith("Error") or prefix.endswith("Exception"):
            return message.strip() or last_line
    return last_line


def _format_ask_tool_call_entry_text(
    *,
    toolkit: str,
    tool: str,
    arguments: dict[str, Any] | None,
    logs: list[str],
    error_message: str | None,
    completed: bool = True,
) -> str:
    failed = error_message is not None
    headline = format_tool_call_summary(
        toolkit=toolkit,
        tool=tool,
        arguments=arguments,
        failed=failed,
        completed=completed,
    )
    raw_headline = (
        f"{'Failed' if failed else 'Ran'} "
        f"{_ask_tool_raw_label(toolkit=toolkit, tool=tool)}"
    )
    log_lines = _ask_tool_log_lines(logs)
    if failed and _ask_log_lines_look_like_traceback(log_lines):
        log_lines = []
    log_limit = _ASK_TOOL_LOG_RENDER_LIMIT
    if headline == raw_headline and log_lines:
        headline = _ask_log_headline(log_lines.pop(0))
        log_limit -= 1
    elif (
        headline == raw_headline
        and not failed
        and completed
        and arguments is None
        and toolkit.strip().casefold() == "openai"
        and tool.strip().casefold() in {"shell", "local_shell", "code_interpreter"}
    ):
        headline = "Explored"

    detail_lines = [headline]
    detail_lines.extend(log_lines[:log_limit])
    error_line = _ask_tool_error_line(error_message)
    if error_line is not None:
        detail_lines.append(error_line)
    return "\n".join(detail_lines)


def _merge_ask_tool_call_arguments_delta(
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    delta_text: str,
) -> dict[str, Any] | None:
    return tool_arguments_from_delta_text(
        tool=tool,
        current=arguments,
        text=delta_text,
    )


def _friendly_ask_thread_event_text(value: str) -> str:
    normalized = value.strip()
    if normalized.casefold() == "ran openai: shell":
        return "Explored"
    return normalized


def _ask_feed_previous_participant_role(
    roles: Sequence[str], *, before_index: int
) -> str | None:
    for role in reversed(roles[:before_index]):
        if role == "event":
            continue
        return role
    return None


def _ask_text_needs_markdown(text: str) -> bool:
    stripped = text.strip()
    if "\n" in stripped:
        return True
    return any(marker in stripped for marker in ("`", "[", "]", "*", "_", "#", ">"))


app = async_typer.AsyncTyper(no_args_is_help=False)


@dataclass(frozen=True, slots=True)
class _AskConversationMessage:
    message_id: str
    role: str
    text: str
    kind: Literal["text", "image"] = "text"
    attachment_references: tuple["_AskAttachmentReference", ...] = ()

    @property
    def attachment_uris(self) -> tuple[str, ...]:
        return tuple(reference.uri for reference in self.attachment_references)


@dataclass(frozen=True, slots=True)
class _AskAttachmentReference:
    uri: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class _AskInputAttachment:
    placeholder: str
    uri: str
    path: str
    name_override: str | None = None

    @property
    def name(self) -> str:
        if self.name_override is not None and self.name_override.strip() != "":
            return self.name_override.strip()
        return Path(self.path).name or self.placeholder


@dataclass(frozen=True, slots=True)
class _ClipboardAttachmentData:
    name: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class _ClipboardAttachments:
    files: tuple[Path, ...] = ()
    data: tuple[_ClipboardAttachmentData, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingSteerCallback:
    message: TurnSteer
    prompt: str
    on_accepted: Callable[[], Awaitable[None] | None] | None
    on_applied: Callable[[], Awaitable[None] | None] | None
    on_rejected: Callable[[RoomException], Awaitable[None] | None] | None


@runtime_checkable
class _AskExternalThreadState(Protocol):
    @property
    def thread_status_text(self) -> str | None: ...

    @property
    def thread_status(self) -> AgentThreadStatus | None: ...

    @property
    def queued_message_labels(self) -> tuple[str, ...]: ...

    @property
    def messages(self) -> tuple[AgentMessage, ...]: ...


@runtime_checkable
class _AskSteerableSession(Protocol):
    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None: ...


@runtime_checkable
class _AskImageDatasetProvider(Protocol):
    @property
    def image_dataset_client(self) -> ImageDatasetClient | None: ...


@runtime_checkable
class _AskThreadGenerationState(Protocol):
    @property
    def thread_generation(self) -> int: ...


async def _send_chat_thread_prompt(
    *,
    session: ChatThreadSession,
    prompt: str,
    attachments: Sequence[_AskInputAttachment] = (),
) -> None:
    file_attachments = [
        AgentFileContent(type="file", url=attachment.uri, name=attachment.name)
        for attachment in attachments
    ]
    if session.has_thread_path:
        await session.send_text(text=prompt, attachments=file_attachments)
        return
    await session.start_thread(text=prompt, attachments=file_attachments)


async def _run_ask_chat_thread_prompt(
    *,
    session: ChatThreadSession,
    prompt: str,
    attachments: Sequence[_AskInputAttachment],
    on_message: Callable[[AgentMessage], Awaitable[None] | None],
) -> None:
    """Run a TUI turn while forwarding each event exactly as it arrives."""
    await session.ask(
        prompt=prompt,
        attachments=attachments,
        on_message=on_message,
    )


class _AgentMessageChannelClient(Protocol):
    @property
    def has_thread_path(self) -> bool: ...

    @property
    def thread_path(self) -> str: ...

    @property
    def thread_status_text(self) -> str | None: ...

    @property
    def thread_status(self) -> AgentThreadStatus | None: ...

    @property
    def queued_message_labels(self) -> tuple[str, ...]: ...

    @property
    def messages(self) -> tuple[AgentMessage, ...]: ...

    def add_agent_message(self, message: AgentMessage) -> None: ...

    def clear_applied_queued_agent_inputs(self) -> None: ...

    async def send(self, payload: Any) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _format_token_count(value: float | int) -> str:
    count = float(value)
    magnitude = abs(count)
    if magnitude >= 1_000_000:
        formatted = f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}M"
    if magnitude >= 1_000:
        formatted = f"{count / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}K"
    return str(int(count))


def _thread_status_text(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    normalized = status.strip()
    if normalized == "":
        return None
    return normalized


def _format_grouped_status_digits(value: int) -> str:
    text = str(value)
    parts: list[str] = []
    for index, char in enumerate(text):
        if index > 0 and (len(text) - index) % 3 == 0:
            parts.append(",")
        parts.append(char)
    return "".join(parts)


def _format_agent_thread_status_text(message: AgentThreadStatus) -> str | None:
    text = _thread_status_text(message.status)
    if text is None:
        return None
    if message.lines_added is not None or message.lines_removed is not None:
        parts = [text]
        if message.lines_added is not None:
            parts.append(f"+{_format_grouped_status_digits(message.lines_added)}")
        if message.lines_removed is not None:
            parts.append(f"-{_format_grouped_status_digits(message.lines_removed)}")
        return " ".join(parts)
    if message.total_bytes is not None and message.total_bytes > 100:
        return f"{text} {_format_grouped_status_digits(message.total_bytes)} bytes"
    return text


_ASK_INPUT_ATTACHMENT_PLACEHOLDER_RE = re.compile(
    r"\[(?:Image #\d+|[^\]\n]+\.[^\]\n]+)\]"
)
_ASK_MACOS_CLIPBOARD_SWIFT = r"""
import AppKit
import Foundation

let pb = NSPasteboard.general
let pdfType = NSPasteboard.PasteboardType("public.pdf")

if let data = pb.data(forType: .png) {
    print("data\tclipboard.png\timage/png\t" + data.base64EncodedString())
} else if let data = pb.data(forType: pdfType) {
    print("data\tclipboard.pdf\tapplication/pdf\t" + data.base64EncodedString())
} else if let data = pb.data(forType: .tiff),
          let image = NSImage(data: data),
          let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) {
    print("data\tclipboard.png\timage/png\t" + png.base64EncodedString())
} else if let urls = pb.readObjects(forClasses: [NSURL.self], options: nil) as? [URL],
          !urls.isEmpty {
    for url in urls {
        print("file\t" + url.path)
    }
} else {
    print("none")
}
"""


def _ask_present_input_attachments(
    prompt: str,
    attachments: Sequence[_AskInputAttachment],
) -> list[_AskInputAttachment]:
    return [
        attachment for attachment in attachments if attachment.placeholder in prompt
    ]


def _ask_prompt_without_attachment_placeholders(
    prompt: str,
    attachments: Sequence[_AskInputAttachment],
) -> str:
    normalized_prompt = prompt
    for attachment in attachments:
        normalized_prompt = normalized_prompt.replace(attachment.placeholder, " ")
    return re.sub(r"[ \t]+", " ", normalized_prompt).strip()


def _input_attachment_file_paths_from_text(
    text: str,
    *,
    current_working_directory: str,
) -> list[Path]:
    return [
        path
        for path in dropped_file_paths_from_text(
            text,
            current_working_directory=current_working_directory,
        )
        if _is_supported_input_attachment_path(path)
    ]


def _image_file_paths_from_text(
    text: str,
    *,
    current_working_directory: str,
) -> list[Path]:
    return [
        path
        for path in _input_attachment_file_paths_from_text(
            text,
            current_working_directory=current_working_directory,
        )
        if _mime_type_for_path(path).startswith("image/")
    ]


def _mime_type_for_path(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _is_supported_input_attachment_mime_type(mime_type: str) -> bool:
    return mime_type.startswith("image/") or mime_type == "application/pdf"


def _is_supported_input_attachment_path(path: Path) -> bool:
    return _is_supported_input_attachment_mime_type(_mime_type_for_path(path))


def _input_attachment_placeholder_for_file(path: Path, *, image_number: int) -> str:
    if _mime_type_for_path(path).startswith("image/"):
        return f"[Image #{image_number}]"
    return f"[{path.name or 'attachment'}]"


def _input_attachment_placeholder_for_data(
    attachment: _ClipboardAttachmentData, *, image_number: int
) -> str:
    if attachment.mime_type.startswith("image/"):
        return f"[Image #{image_number}]"
    return f"[{attachment.name or 'attachment'}]"


def _macos_clipboard_attachments() -> _ClipboardAttachments:
    if sys.platform != "darwin":
        return _ClipboardAttachments()
    try:
        completed = subprocess.run(
            ["swift", "-e", _ASK_MACOS_CLIPBOARD_SWIFT],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return _ClipboardAttachments()
    if completed.returncode != 0:
        return _ClipboardAttachments()
    files: list[Path] = []
    data_attachments: list[_ClipboardAttachmentData] = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 2 and parts[0] == "file":
            path = Path(parts[1]).expanduser()
            if path.is_file() and _is_supported_input_attachment_path(path):
                files.append(path)
            continue
        if len(parts) == 4 and parts[0] == "data":
            name = parts[1].strip() or "attachment"
            mime_type = parts[2].strip()
            if not _is_supported_input_attachment_mime_type(mime_type):
                continue
            try:
                data = base64.b64decode(parts[3], validate=True)
            except (binascii.Error, ValueError):
                continue
            data_attachments.append(
                _ClipboardAttachmentData(
                    name=name,
                    mime_type=mime_type,
                    data=data,
                )
            )
    return _ClipboardAttachments(files=tuple(files), data=tuple(data_attachments))


def _debug_ask_paste_event(
    *,
    source: str,
    text: str,
    current_working_directory: str,
    file_paths: Sequence[Path],
    image_paths: Sequence[Path],
    attachment_paths: Sequence[Path] = (),
) -> None:
    debug_path = os.environ.get(_ASK_PASTE_DEBUG_ENV, "").strip()
    if debug_path == "":
        return
    target = Path(debug_path)
    if target.is_dir():
        target = target / "meshagent-ask-paste.log"
    try:
        with target.expanduser().open("a", encoding="utf-8") as handle:
            handle.write(f"source={source}\n")
            handle.write(f"cwd={current_working_directory}\n")
            handle.write(f"text={text!r}\n")
            handle.write(f"file_paths={[str(path) for path in file_paths]!r}\n")
            handle.write(f"image_paths={[str(path) for path in image_paths]!r}\n")
            if len(attachment_paths) > 0:
                handle.write(
                    f"attachment_paths={[str(path) for path in attachment_paths]!r}\n"
                )
            handle.write("\n")
    except OSError:
        pass


def _debug_ask_attachment_event(
    *,
    event: str,
    placeholder: str,
    image_path: Path,
    input_text: str | None = None,
    error: BaseException | None = None,
) -> None:
    debug_path = os.environ.get(_ASK_PASTE_DEBUG_ENV, "").strip()
    if debug_path == "":
        return
    target = Path(debug_path)
    if target.is_dir():
        target = target / "meshagent-ask-paste.log"
    try:
        with target.expanduser().open("a", encoding="utf-8") as handle:
            handle.write(f"event={event}\n")
            handle.write(f"placeholder={placeholder}\n")
            handle.write(f"image_path={image_path}\n")
            if input_text is not None:
                handle.write(f"input_text={input_text!r}\n")
            if error is not None:
                handle.write(f"error={type(error).__name__}: {error}\n")
            handle.write("\n")
    except OSError:
        pass


async def _save_ask_input_image_attachment(
    *,
    image_path: Path,
    placeholder: str,
) -> _AskInputAttachment:
    return await _save_ask_input_file_attachment(
        path=image_path,
        placeholder=placeholder,
    )


async def _save_ask_input_file_attachment(
    *,
    path: Path,
    placeholder: str,
) -> _AskInputAttachment:
    return await asyncio.to_thread(
        _save_ask_input_file_attachment_sync,
        path=path,
        placeholder=placeholder,
    )


def _save_ask_input_file_attachment_sync(
    *,
    path: Path,
    placeholder: str,
) -> _AskInputAttachment:
    data = path.read_bytes()
    mime_type = _mime_type_for_path(path)
    return _ask_input_attachment_from_bytes(
        data=data,
        mime_type=mime_type,
        name=path.name,
        placeholder=placeholder,
        path=str(path),
    )


def _ask_input_attachment_from_bytes(
    *,
    data: bytes,
    mime_type: str,
    name: str,
    placeholder: str,
    path: str | None = None,
) -> _AskInputAttachment:
    encoded = base64.b64encode(data).decode("ascii")
    return _AskInputAttachment(
        placeholder=placeholder,
        uri=f"data:{mime_type};base64,{encoded}",
        path=path or name,
        name_override=name,
    )


def _ask_thread_status_feed_text(status: object) -> str | None:
    if isinstance(status, AgentThreadStatus):
        text = _format_agent_thread_status_text(status)
        if text is None:
            return None
        return f"• {text}"
    text = _thread_status_text(status)
    if text is None:
        return None
    return f"• {text}"


def _sync_status_timer_started_at(
    *,
    started_at: float | None,
    active: bool,
    pending: bool,
    now: float,
) -> float | None:
    if active and started_at is None:
        return now
    if not active and not pending:
        return None
    return started_at


def _agent_message_content_text(
    content: list[AgentTextContent | AgentFileContent],
) -> str:
    text_parts: list[str] = []
    for item in content:
        if isinstance(item, AgentTextContent) and item.text.strip() != "":
            text_parts.append(item.text)
            continue
        if isinstance(item, AgentFileContent) and item.url.strip() != "":
            if not _attachment_uri_may_render_as_image(item.url):
                text_parts.append(_attachment_display_text(item.url, name=item.name))
    return "\n\n".join(text_parts).strip()


def _agent_message_content_attachment_uris(
    content: list[AgentTextContent | AgentFileContent],
) -> tuple[str, ...]:
    return tuple(
        attachment.uri
        for attachment in _agent_message_content_attachment_references(content)
    )


def _agent_message_content_attachment_references(
    content: list[AgentTextContent | AgentFileContent],
) -> tuple[_AskAttachmentReference, ...]:
    return tuple(
        _AskAttachmentReference(uri=item.url.strip(), name=item.name)
        for item in content
        if isinstance(item, AgentFileContent) and item.url.strip() != ""
    )


def _attachment_display_text(uri: str | None, *, name: str | None = None) -> str:
    display_name = _attachment_display_name(uri, name=name)
    return f"[{display_name}]"


def _attachment_uri_may_render_as_image(uri: str | None) -> bool:
    if not isinstance(uri, str):
        return False
    normalized_uri = uri.strip()
    if normalized_uri == "":
        return False
    data_match = re.fullmatch(
        r"data:([^;,]*)(?:;[^,]*)?;base64,.*", normalized_uri, re.S
    )
    if data_match is not None:
        return data_match.group(1).strip().startswith("image/")
    return ImageDatasetClient.dataset_uri_reference(normalized_uri) is not None


def _attachment_uri_may_render_as_pdf(uri: str | None) -> bool:
    if not isinstance(uri, str):
        return False
    normalized_uri = uri.strip()
    if normalized_uri == "":
        return False
    data_match = re.fullmatch(
        r"data:([^;,]*)(?:;[^,]*)?;base64,.*", normalized_uri, re.S
    )
    if data_match is not None:
        return data_match.group(1).strip() == "application/pdf"
    path = Path(normalized_uri)
    return path.suffix.lower() == ".pdf"


def _attachment_uri_may_render_inline(uri: str | None) -> bool:
    return _attachment_uri_may_render_as_image(
        uri
    ) or _attachment_uri_may_render_as_pdf(uri)


def _attachment_display_name(uri: str | None, *, name: str | None = None) -> str:
    if isinstance(name, str) and name.strip() != "":
        return name.strip()
    if not isinstance(uri, str):
        return "attachment"
    normalized_uri = uri.strip()
    if normalized_uri == "":
        return "attachment"
    data_match = re.fullmatch(
        r"data:([^;,]*)(?:;[^,]*)?;base64,.*", normalized_uri, re.S
    )
    if data_match is not None:
        return _attachment_name_for_mime_type(data_match.group(1).strip())
    parsed = Path(normalized_uri)
    path_name = parsed.name
    if path_name != "" and path_name != normalized_uri:
        return path_name
    return "attachment"


def _attachment_name_for_mime_type(mime_type: str) -> str:
    normalized_mime_type = mime_type or "application/octet-stream"
    if normalized_mime_type == "application/pdf":
        return "document.pdf"
    if normalized_mime_type.startswith("image/"):
        extension = mimetypes.guess_extension(normalized_mime_type) or ".png"
        return f"image{extension}"
    extension = mimetypes.guess_extension(normalized_mime_type)
    return f"attachment{extension}" if extension is not None else "attachment"


def _image_bytes_from_data_uri(uri: str) -> bytes | None:
    record = _image_record_from_data_uri(uri)
    return None if record is None else record.data


def _image_record_from_data_uri(
    uri: str,
    *,
    fallback_mime_type: str | None = None,
) -> ImageDatasetRecord | None:
    record = _attachment_record_from_data_uri(
        uri,
        fallback_mime_type=fallback_mime_type,
    )
    if record is None or not record.mime_type.startswith("image/"):
        return None
    return record


def _attachment_record_from_data_uri(
    uri: str,
    *,
    fallback_mime_type: str | None = None,
) -> ImageDatasetRecord | None:
    match = re.fullmatch(r"data:([^;,]*)(?:;[^,]*)?;base64,(.*)", uri, re.S)
    if match is None:
        return None
    mime_type = match.group(1).strip() or (
        fallback_mime_type or "application/octet-stream"
    )
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None
    return ImageDatasetRecord(data=data, mime_type=mime_type)


async def _attachment_record_from_uri(
    uri: str | None,
    *,
    image_dataset_client: ImageDatasetClient | None = None,
    fallback_mime_type: str | None = None,
) -> ImageDatasetRecord | None:
    if not isinstance(uri, str):
        return None
    normalized_uri = uri.strip()
    record = _attachment_record_from_data_uri(
        normalized_uri,
        fallback_mime_type=fallback_mime_type,
    )
    if record is not None:
        return record
    if image_dataset_client is None:
        return None
    try:
        return await image_dataset_client.read_record_from_uri(
            normalized_uri,
            fallback_mime_type=fallback_mime_type,
        )
    except Exception:
        return None


async def _image_record_from_uri(
    uri: str | None,
    *,
    image_dataset_client: ImageDatasetClient | None = None,
    fallback_mime_type: str | None = None,
) -> ImageDatasetRecord | None:
    if not isinstance(uri, str):
        return None
    normalized_uri = uri.strip()
    record = _image_record_from_data_uri(
        normalized_uri,
        fallback_mime_type=fallback_mime_type,
    )
    if record is not None:
        return record
    if image_dataset_client is None:
        return None
    try:
        return await image_dataset_client.read_record_from_uri(
            normalized_uri,
            fallback_mime_type=fallback_mime_type,
        )
    except Exception:
        return None


def _ascii_image_from_record(
    record: ImageDatasetRecord,
    *,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
) -> str | None:
    if not record.mime_type.startswith("image/"):
        return None
    if len(record.data) == 0:
        return None
    try:
        import ascii_magic
        from PIL import Image

        with Image.open(io.BytesIO(record.data)) as image:
            art = ascii_magic.from_pillow_image(image)
            rendered = art.to_terminal(columns=columns, monochrome=False)
    except Exception:
        return None
    normalized = rendered.strip("\n")
    return normalized if normalized.strip() != "" else None


def _image_preview_from_record(
    record: ImageDatasetRecord,
    *,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
    max_rows: int | None = None,
) -> _AskImagePreview | None:
    if not record.mime_type.startswith("image/"):
        return None
    if len(record.data) == 0:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(record.data)) as image:
            width_px = max(image.width, 1)
            height_px = max(image.height, 1)
            png_buffer = io.BytesIO()
            image.save(png_buffer, format="PNG")
            png_data = png_buffer.getvalue()
    except Exception:
        return None

    normalized_columns = max(1, columns)
    rows = max(
        1,
        (height_px * normalized_columns + (width_px * 2) - 1) // (width_px * 2),
    )
    if max_rows is not None and max_rows > 0 and rows > max_rows:
        rows = max_rows
        normalized_columns = max(1, (width_px * 2 * rows) // height_px)
    return _AskImagePreview(
        data=png_data,
        width_px=width_px,
        height_px=height_px,
        columns=normalized_columns,
        rows=rows,
    )


def _pdf_text_from_record(record: ImageDatasetRecord) -> str | None:
    if record.mime_type != "application/pdf" or len(record.data) == 0:
        return None
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file:
            pdf_file.write(record.data)
            pdf_file.flush()
            result = subprocess.run(
                [pdftotext, "-layout", pdf_file.name, "-"],
                check=False,
                capture_output=True,
                timeout=10,
            )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return text if text != "" else None


def _pdf_page_previews_from_record(
    record: ImageDatasetRecord,
    *,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
    max_rows: int | None = None,
) -> tuple[_AskImagePreview, ...]:
    if record.mime_type != "application/pdf" or len(record.data) == 0:
        return ()
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        return ()
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "document.pdf"
            output_prefix = Path(tmp_dir) / "page"
            pdf_path.write_bytes(record.data)
            result = subprocess.run(
                [
                    pdftoppm,
                    "-png",
                    "-r",
                    "120",
                    str(pdf_path),
                    str(output_prefix),
                ],
                check=False,
                capture_output=True,
                timeout=15,
            )
            if result.returncode != 0:
                return ()
            previews: list[_AskImagePreview] = []
            for page_path in sorted(Path(tmp_dir).glob("page-*.png")):
                preview = _image_preview_from_record(
                    ImageDatasetRecord(
                        data=page_path.read_bytes(),
                        mime_type="image/png",
                    ),
                    columns=columns,
                    max_rows=max_rows,
                )
                if preview is not None:
                    previews.append(preview)
            return tuple(previews)
    except Exception:
        return ()


def _pdf_preview_from_record(
    record: ImageDatasetRecord,
    *,
    name: str,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
    max_rows: int | None = None,
) -> _AskPdfPreview | None:
    if record.mime_type != "application/pdf":
        return None
    pages = _pdf_page_previews_from_record(
        record,
        columns=columns,
        max_rows=max_rows,
    )
    text = _pdf_text_from_record(record)
    if len(pages) == 0 and text is None:
        return None
    return _AskPdfPreview(name=name, pages=pages, text=text)


def _ascii_image_from_uri(
    uri: str | None,
    *,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
) -> str | None:
    if not isinstance(uri, str):
        return None
    record = _image_record_from_data_uri(uri.strip())
    if record is None:
        return None
    return _ascii_image_from_record(record, columns=columns)


def _dataset_image_attachment_uri(text: str) -> str | None:
    match = re.fullmatch(r"\[attachment\]\s+(dataset://\S+)", text.strip())
    if match is None:
        return None
    uri = match.group(1).strip()
    if ImageDatasetClient.dataset_uri_reference(uri) is None:
        return None
    return uri


def _dataset_image_attachment_text(uri: str | None) -> str | None:
    if not isinstance(uri, str):
        return None
    normalized_uri = uri.strip()
    if ImageDatasetClient.dataset_uri_reference(normalized_uri) is None:
        return None
    return f"[attachment] {normalized_uri}"


def _ask_inline_pending_message_ids(
    session: _AskExternalThreadState,
    *,
    external_thread_active: bool,
) -> set[str]:
    if external_thread_active:
        return set()
    if not isinstance(session, ChatThreadSession):
        return set()

    message_ids: set[str] = set()
    for pending in session.pending_inputs:
        if not isinstance(pending.payload, (StartThread, TurnStart)):
            continue
        if _agent_message_content_text(pending.payload.content or []).strip() == "":
            continue
        message_ids.add(pending.message_id)
    return message_ids


def _ask_queued_message_labels(
    session: _AskExternalThreadState,
    *,
    external_thread_active: bool,
) -> list[str]:
    inline_message_ids = _ask_inline_pending_message_ids(
        session,
        external_thread_active=external_thread_active,
    )
    if len(inline_message_ids) == 0:
        return list(session.queued_message_labels)
    if not isinstance(session, ChatThreadSession):
        return list(session.queued_message_labels)
    return [
        pending.label
        for pending in session.pending_inputs
        if pending.message_id not in inline_message_ids
    ]


async def _ascii_image_from_uri_async(
    uri: str | None,
    *,
    image_dataset_client: ImageDatasetClient | None = None,
    fallback_mime_type: str | None = None,
    columns: int = _ASK_IMAGE_RENDER_COLUMNS,
) -> str | None:
    record = await _image_record_from_uri(
        uri,
        image_dataset_client=image_dataset_client,
        fallback_mime_type=fallback_mime_type,
    )
    if record is None:
        return None
    return _ascii_image_from_record(record, columns=columns)


def _image_dataset_client_from_agent_client(
    client: _AgentMessageChannelClient,
) -> ImageDatasetClient | None:
    if not isinstance(client, ChatThreadSession):
        return None
    chat_client = client.client
    if not isinstance(chat_client, MessagingChatClient):
        return None
    return ImageDatasetClient(chat_client.room.datasets)


def _role_for_sender(
    sender_name: object,
    *,
    local_participant_name: str | None,
    default: str,
) -> str:
    if not isinstance(sender_name, str):
        return default
    normalized_sender_name = sender_name.strip()
    if normalized_sender_name == "":
        return default
    if normalized_sender_name == local_participant_name:
        return "you"
    return normalized_sender_name


def _ask_conversation_message_from_agent_message(
    message: AgentMessage,
    *,
    local_participant_name: str | None,
) -> _AskConversationMessage | None:
    if isinstance(message, (StartThread, TurnStart, TurnSteer)):
        text = _agent_message_content_text(message.content)
        attachment_references = _agent_message_content_attachment_references(
            message.content
        )
        if text == "" and len(attachment_references) == 0:
            return None
        return _AskConversationMessage(
            message_id=message.message_id,
            role=_role_for_sender(
                message.sender_name,
                local_participant_name=local_participant_name,
                default="you",
            ),
            text=text,
            attachment_references=attachment_references,
        )
    if isinstance(message, (TurnStartAccepted, TurnSteerAccepted)):
        text = _agent_message_content_text(message.content)
        attachment_references = _agent_message_content_attachment_references(
            message.content
        )
        if text == "" and len(attachment_references) == 0:
            return None
        return _AskConversationMessage(
            message_id=message.source_message_id,
            role=_role_for_sender(
                message.sender_name,
                local_participant_name=local_participant_name,
                default="user",
            ),
            text=text,
            attachment_references=attachment_references,
        )
    if isinstance(message, TurnEnded):
        if message.error is None:
            return None
        return _AskConversationMessage(
            message_id=message.message_id,
            role="error",
            text=message.error.message,
        )
    if isinstance(message, AgentTextContentDelta):
        return _AskConversationMessage(
            message_id=message.item_id,
            role=_role_for_sender(
                message.sender_name,
                local_participant_name=local_participant_name,
                default="assistant",
            ),
            text=message.text,
        )
    if isinstance(message, AgentAudioTranscriptionDelta):
        return _AskConversationMessage(
            message_id=message.item_id,
            role=message.role
            or _role_for_sender(
                message.sender_name,
                local_participant_name=local_participant_name,
                default="assistant",
            ),
            text=message.text,
        )
    if isinstance(message, AgentFileContentDelta):
        can_render_inline = _attachment_uri_may_render_inline(message.url)
        return _AskConversationMessage(
            message_id=message.item_id,
            role=_role_for_sender(
                message.sender_name,
                local_participant_name=local_participant_name,
                default="assistant",
            ),
            text="" if can_render_inline else _attachment_display_text(message.url),
            kind="image" if can_render_inline else "text",
            attachment_references=(
                (_AskAttachmentReference(uri=message.url),) if can_render_inline else ()
            ),
        )
    if isinstance(message, AgentImageGenerationPartial):
        if message.image is None:
            return None
        if not _attachment_uri_may_render_as_image(message.image.uri):
            return None
        return _AskConversationMessage(
            message_id=message.item_id,
            role="assistant",
            text="",
            kind="image",
            attachment_references=(_AskAttachmentReference(uri=message.image.uri),),
        )
    if isinstance(message, AgentImageGenerationCompleted):
        image_uris: list[str] = []
        for image in message.images:
            if _attachment_uri_may_render_as_image(image.uri):
                image_uris.append(image.uri)
        if len(image_uris) == 0:
            return None
        return _AskConversationMessage(
            message_id=message.item_id,
            role="assistant",
            text="",
            kind="image",
            attachment_references=tuple(
                _AskAttachmentReference(uri=uri) for uri in image_uris
            ),
        )
    return None


def _ask_conversation_messages_from_agent_messages(
    messages: Sequence[AgentMessage],
    *,
    local_participant_name: str | None,
) -> tuple[_AskConversationMessage, ...]:
    conversation_messages: list[_AskConversationMessage] = []
    seen_message_ids: set[str] = set()
    for message in messages:
        conversation_message = _ask_conversation_message_from_agent_message(
            message,
            local_participant_name=local_participant_name,
        )
        if conversation_message is None:
            continue
        if conversation_message.message_id in seen_message_ids:
            continue
        seen_message_ids.add(conversation_message.message_id)
        conversation_messages.append(conversation_message)
    return tuple(conversation_messages)


def _ask_conversation_message_render_window(height: int) -> int:
    return max(24, min(160, max(1, height) * 4))


async def _maybe_await(callback_result: Any) -> None:
    if inspect.isawaitable(callback_result):
        await callback_result


async def _emit_agent_message(
    callback: Callable[[AgentMessage], Awaitable[None] | None] | None,
    message: AgentMessage,
) -> None:
    if callback is not None:
        await _maybe_await(callback(message))


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


class _AskSupervisor(AgentSupervisor):
    def __init__(self, *, process: LLMAgentProcess) -> None:
        super().__init__()
        self._process = process
        self.add_process(process)
        self.events: asyncio.Queue[Message] = asyncio.Queue()

    def create_thread_process(self, thread_id: str):
        if thread_id != self._process.thread_id:
            raise ValueError(f"unknown thread_id {thread_id}")
        return self._process

    def send(self, message: Message) -> None:
        if message.source is not None:
            self.events.put_nowait(message)
        super().send(message)


def _status_text_from_custom_event(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type not in ("agent.event", "codex.event"):
        return None

    raw_state = event.get("state")
    if not isinstance(raw_state, str):
        return None
    state = raw_state.strip().lower()
    if state == "":
        return None

    if state in _ASK_ACTIVE_STATUS_STATES:
        for candidate in (event.get("headline"), event.get("summary")):
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized != "":
                    return normalized
        return "Working"

    if state in _ASK_TERMINAL_STATUS_STATES:
        return "Working"

    return None


class _StatusAwareLLMAdapter(LLMAdapter[Any]):
    def __init__(self, *, delegate: LLMAdapter) -> None:
        self._delegate = delegate
        self._status_callback: Callable[[str], None] | None = None

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        self._status_callback = callback

    def default_model(self) -> str:
        return self._delegate.default_model()

    def create_session(self, *, usage_callback=None):
        return self._delegate.create_session(usage_callback=usage_callback)

    def get_additional_instructions(self) -> str | None:
        return self._delegate.get_additional_instructions()

    def on_turn_steer(self, *, context, interrupted: bool) -> None:
        self._delegate.on_turn_steer(context=context, interrupted=interrupted)

    def context_window_size(self, model: str) -> float:
        return self._delegate.context_window_size(model)

    def needs_compaction(self, *, context) -> bool:
        return self._delegate.needs_compaction(context=context)

    async def compact(self, *, context, model: str | None = None) -> None:
        await self._delegate.compact(context=context, model=model)

    async def get_input_tokens(
        self,
        *,
        context,
        model: str,
        toolkits: list | None = None,
        output_schema: dict | None = None,
    ) -> int:
        return await self._delegate.get_input_tokens(
            context=context,
            model=model,
            toolkits=toolkits,
            output_schema=output_schema,
        )

    async def check_for_termination(self, *, context):
        return await self._delegate.check_for_termination(context=context)

    def set_tool_call_approval_handler(self, handler) -> None:
        self._delegate.set_tool_call_approval_handler(handler)

    def make_agent_event_publisher(
        self,
        turn_id: str,
        thread_id: str,
        callback,
        custom_event_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        def _handle_custom_event(event: dict[str, Any]) -> None:
            if custom_event_callback is not None:
                custom_event_callback(event)

            if self._status_callback is None:
                return

            status_text = _status_text_from_custom_event(event)
            if status_text is not None:
                self._status_callback(status_text)

        return self._delegate.make_agent_event_publisher(
            turn_id=turn_id,
            thread_id=thread_id,
            callback=callback,
            custom_event_callback=_handle_custom_event,
        )

    async def create_response(
        self,
        *,
        context,
        caller: Participant,
        toolkits,
        output_schema=None,
        event_handler=None,
        steering_callback=None,
        model: str | None = None,
        on_behalf_of: Participant | None = None,
        tool_choice=None,
        options: dict | None = None,
    ) -> Any:
        return await self._delegate.create_response(
            context=context,
            caller=caller,
            toolkits=toolkits,
            output_schema=output_schema,
            event_handler=event_handler,
            steering_callback=steering_callback,
            model=model,
            on_behalf_of=on_behalf_of,
            tool_choice=tool_choice,
            options=options,
        )


class _AgentMessageSession:
    def __init__(
        self,
        *,
        client: _AgentMessageChannelClient,
        model: str | None,
        model_provider: Callable[[], AgentModelChanged | None] | None = None,
        start_thread_callback: Callable[
            [StartThread], Awaitable[_AgentMessageChannelClient]
        ]
        | None = None,
        current_working_directory: str | None = None,
        local_participant_name: str | None = None,
        image_dataset_client: ImageDatasetClient | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._model_provider = model_provider
        self._start_thread_callback = start_thread_callback
        self._image_dataset_client = image_dataset_client
        self._output_modalities: tuple[Literal["text", "audio"], ...] | None = None
        self._current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._local_participant_name = (
            local_participant_name.strip()
            if isinstance(local_participant_name, str)
            and local_participant_name.strip() != ""
            else None
        )
        self._active_turn_id: str | None = None
        self._thread_status_text: str | None = None
        self._pending_steer_callbacks: dict[str, _PendingSteerCallback] = {}

    @property
    def current_working_directory(self) -> str:
        return self._current_working_directory

    @property
    def thread_status_text(self) -> str | None:
        if self._thread_status_text is not None:
            return self._thread_status_text
        return self._client.thread_status_text

    @property
    def thread_status(self) -> AgentThreadStatus | None:
        return self._client.thread_status

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._client.queued_message_labels

    @property
    def image_dataset_client(self) -> ImageDatasetClient | None:
        if self._image_dataset_client is not None:
            return self._image_dataset_client
        return _image_dataset_client_from_agent_client(self._client)

    def set_output_modalities(
        self, output_modalities: tuple[Literal["text", "audio"], ...] | None
    ) -> None:
        self._output_modalities = output_modalities

    @property
    def messages(self) -> tuple[_AskConversationMessage, ...]:
        return _ask_conversation_messages_from_agent_messages(
            self._client.messages,
            local_participant_name=self._local_participant_name,
        )

    def add_agent_message(self, message: AgentMessage) -> None:
        self._client.add_agent_message(message)

    def replace_client(self, client: _AgentMessageChannelClient) -> None:
        self._client = client
        if self._image_dataset_client is None:
            self._image_dataset_client = _image_dataset_client_from_agent_client(client)

    async def close(self, *, close_client: bool = True) -> None:
        self._pending_steer_callbacks.clear()
        if close_client:
            await self._client.close()

    async def ask(
        self,
        *,
        prompt: str,
        attachments: Sequence[_AskInputAttachment] = (),
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        content: list[AgentTextContent | AgentFileContent] = []
        if prompt.strip() != "":
            content.append(
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            )
        content.extend(
            AgentFileContent(
                type="file",
                url=attachment.uri,
                name=attachment.name,
            )
            for attachment in attachments
        )
        current_model = (
            self._model_provider() if self._model_provider is not None else None
        )
        backend_name = current_model.backend if current_model is not None else None
        provider_name = current_model.provider if current_model is not None else None
        model_name = current_model.model if current_model is not None else self._model
        output_modalities = self._output_modalities
        if self._client.has_thread_path:
            turn_start_args: dict[str, Any] = {
                "type": AGENT_MESSAGE_TURN_START,
                "thread_id": self._client.thread_path,
                "content": content,
            }
            if backend_name is not None:
                turn_start_args["backend"] = backend_name
            if provider_name is not None:
                turn_start_args["provider"] = provider_name
            if model_name is not None:
                turn_start_args["model"] = model_name
            if output_modalities is not None:
                turn_start_args["output_modalities"] = list(output_modalities)
            input_message = TurnStart.model_validate(turn_start_args)
        else:
            start_thread_args: dict[str, Any] = {
                "type": AGENT_MESSAGE_THREAD_START,
                "content": content,
            }
            if backend_name is not None:
                start_thread_args["backend"] = backend_name
            if provider_name is not None:
                start_thread_args["provider"] = provider_name
            if model_name is not None:
                start_thread_args["model"] = model_name
            if output_modalities is not None:
                start_thread_args["output_modalities"] = list(output_modalities)
            input_message = StartThread.model_validate(start_thread_args)

        if isinstance(input_message, StartThread):
            if self._start_thread_callback is None:
                raise RoomException("chat client cannot start a new thread")
            self._client = await self._start_thread_callback(input_message)
            if self._image_dataset_client is None:
                self._image_dataset_client = _image_dataset_client_from_agent_client(
                    self._client
                )
        else:
            await self._client.send(input_message)

        await _emit_agent_message(
            on_message,
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id=self._client.thread_path,
                status="Working",
            ),
        )

        output_parts: list[str] = []
        active_turn_id: str | None = None
        try:
            while True:
                payload = await self._client.receive()
                if payload.get("thread_id") != self._client.thread_path:
                    continue
                event_type = payload.get("type")

                if event_type == AGENT_EVENT_TURN_START_ACCEPTED:
                    turn_start_accepted = TurnStartAccepted.model_validate(payload)
                    if (
                        turn_start_accepted.source_message_id
                        == input_message.message_id
                    ):
                        active_turn_id = turn_start_accepted.turn_id
                        self._active_turn_id = active_turn_id
                    self._client.add_agent_message(turn_start_accepted)
                    await _emit_agent_message(on_message, turn_start_accepted)
                    continue

                if event_type == AGENT_EVENT_TURN_STARTED:
                    turn_started = TurnStarted.model_validate(payload)
                    if turn_started.source_message_id == input_message.message_id:
                        active_turn_id = turn_started.turn_id
                        self._active_turn_id = active_turn_id
                        await _emit_agent_message(on_message, turn_started)
                    continue

                if event_type == AGENT_EVENT_TURN_START_REJECTED:
                    turn_start_rejected = TurnStartRejected.model_validate(payload)
                    if (
                        turn_start_rejected.source_message_id
                        != input_message.message_id
                    ):
                        continue
                    await _emit_agent_message(on_message, turn_start_rejected)
                    raise RoomException(
                        turn_start_rejected.error.message,
                        code=turn_start_rejected.error.code,
                    )

                if event_type == AGENT_EVENT_TURN_STEER_ACCEPTED:
                    steer_accepted = TurnSteerAccepted.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.get(
                        steer_accepted.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    if pending_callbacks.on_accepted is not None:
                        await _maybe_await(pending_callbacks.on_accepted())
                    await _emit_agent_message(on_message, steer_accepted)
                    continue

                if event_type == AGENT_EVENT_TURN_STEERED:
                    steer_applied = TurnSteered.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_applied.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    self._client.add_agent_message(pending_callbacks.message)
                    if pending_callbacks.on_applied is not None:
                        await _maybe_await(pending_callbacks.on_applied())
                    await _emit_agent_message(on_message, steer_applied)
                    continue

                if event_type == AGENT_EVENT_TURN_STEER_REJECTED:
                    steer_rejected = TurnSteerRejected.model_validate(payload)
                    pending_callbacks = self._pending_steer_callbacks.pop(
                        steer_rejected.source_message_id, None
                    )
                    if pending_callbacks is None:
                        continue
                    if pending_callbacks.on_rejected is not None:
                        await _maybe_await(
                            pending_callbacks.on_rejected(
                                RoomException(
                                    steer_rejected.error.message,
                                    code=steer_rejected.error.code,
                                )
                            )
                        )
                    await _emit_agent_message(on_message, steer_rejected)
                    continue

                if event_type == AGENT_EVENT_THREAD_STATUS:
                    thread_status = AgentThreadStatus.model_validate(payload)
                    if active_turn_id is not None and thread_status.turn_id not in (
                        None,
                        active_turn_id,
                    ):
                        continue
                    self._thread_status_text = _thread_status_text(thread_status.status)
                    await _emit_agent_message(on_message, thread_status)
                    continue

                if event_type == AGENT_EVENT_TEXT_CONTENT_DELTA:
                    text_delta = AgentTextContentDelta.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and text_delta.turn_id != active_turn_id
                    ):
                        continue
                    output_parts.append(text_delta.text)
                    await _emit_agent_message(on_message, text_delta)
                    continue

                if event_type == AGENT_EVENT_AUDIO_TRANSCRIPTION_DELTA:
                    transcript_delta = AgentAudioTranscriptionDelta.model_validate(
                        payload
                    )
                    if (
                        active_turn_id is not None
                        and transcript_delta.turn_id != active_turn_id
                    ):
                        continue
                    if transcript_delta.role in {None, "assistant"}:
                        output_parts.append(transcript_delta.text)
                    await _emit_agent_message(on_message, transcript_delta)
                    continue

                if event_type == AGENT_EVENT_USAGE_UPDATED:
                    usage_update = AgentUsageUpdated.model_validate(payload)
                    if active_turn_id is not None and usage_update.turn_id not in (
                        None,
                        active_turn_id,
                    ):
                        continue
                    await _emit_agent_message(on_message, usage_update)
                    continue

                if event_type in _ASK_PASS_THROUGH_AGENT_EVENT_TYPES:
                    agent_message = parse_agent_message(payload)
                    if (
                        active_turn_id is not None
                        and isinstance(
                            agent_message,
                            (
                                AgentFileContentDelta,
                                AgentFileContentEnded,
                                AgentFileContentStarted,
                                AgentAudioGenerationCompleted,
                                AgentAudioGenerationDelta,
                                AgentAudioGenerationFailed,
                                AgentAudioGenerationStarted,
                                AgentAudioTranscriptionCompleted,
                                AgentAudioTranscriptionFailed,
                                AgentAudioTranscriptionStarted,
                                AgentImageGenerationCompleted,
                                AgentImageGenerationFailed,
                                AgentImageGenerationPartial,
                                AgentImageGenerationStarted,
                                AgentReasoningContentDelta,
                                AgentReasoningContentEnded,
                                AgentReasoningContentStarted,
                                AgentTextContentEnded,
                                AgentTextContentStarted,
                                AgentToolCallArgumentsDelta,
                                AgentToolCallApprovalRequested,
                                AgentToolCallEnded,
                                AgentToolCallInProgress,
                                AgentToolCallLogDelta,
                                AgentToolCallPending,
                                AgentToolCallStarted,
                            ),
                        )
                        and agent_message.turn_id != active_turn_id
                    ):
                        continue
                    await _emit_agent_message(on_message, agent_message)
                    continue

                if event_type == AGENT_EVENT_TURN_ENDED:
                    turn_ended = TurnEnded.model_validate(payload)
                    if (
                        active_turn_id is not None
                        and turn_ended.turn_id != active_turn_id
                    ):
                        continue
                    if turn_ended.error is not None:
                        raise RoomException(
                            turn_ended.error.message,
                            code=turn_ended.error.code,
                        )
                    await _emit_agent_message(on_message, turn_ended)
                    return "".join(output_parts)
        finally:
            self._active_turn_id = None
            self._pending_steer_callbacks.clear()
            self._thread_status_text = None
            self._client.clear_applied_queued_agent_inputs()
            await _emit_agent_message(
                on_message,
                AgentThreadStatus(
                    type=AGENT_EVENT_THREAD_STATUS,
                    thread_id=self._client.thread_path,
                    status=None,
                ),
            )

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        if self._active_turn_id is None:
            return None

        turn_steer = TurnSteer(
            type=AGENT_MESSAGE_TURN_STEER,
            thread_id=self._client.thread_path,
            turn_id=self._active_turn_id,
            content=[
                AgentTextContent(
                    type="text",
                    text=prompt,
                )
            ],
        )
        self._pending_steer_callbacks[turn_steer.message_id] = _PendingSteerCallback(
            message=turn_steer,
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

        async def _send_steer() -> None:
            try:
                await self._client.send(turn_steer)
            except RoomException as exc:
                self._pending_steer_callbacks.pop(turn_steer.message_id, None)
                if on_rejected is not None:
                    await _maybe_await(on_rejected(exc))
            except Exception as exc:
                self._pending_steer_callbacks.pop(turn_steer.message_id, None)
                if on_rejected is not None:
                    await _maybe_await(on_rejected(RoomException(str(exc))))

        task = asyncio.create_task(_send_steer())
        task.add_done_callback(_consume_task_exception)
        return turn_steer.message_id

    def interrupt(self) -> bool:
        if self._active_turn_id is None:
            return False

        turn_interrupt = TurnInterrupt(
            type=AGENT_MESSAGE_TURN_INTERRUPT,
            thread_id=self._client.thread_path,
            turn_id=self._active_turn_id,
        )

        async def _send_interrupt() -> None:
            await self._client.send(turn_interrupt)

        task = asyncio.create_task(_send_interrupt())
        task.add_done_callback(_consume_task_exception)
        return True


class _AskSession:
    def __init__(
        self,
        *,
        model: str,
        llm_adapter: LLMAdapter,
        current_working_directory: str | None = None,
        interactive: bool = True,
        preamble_rule: bool = True,
    ) -> None:
        self._model = model
        self._thread_id = f"/ask/{uuid.uuid4()}"
        self._participant = Participant(
            id="meshagent-ask",
            attributes={"name": "meshagent-ask"},
        )
        resolved_current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        self._resource_stack = contextlib.ExitStack()
        create_samples_path = _enter_create_samples_path(self._resource_stack)
        self._create_samples_path = create_samples_path
        self._current_working_directory = resolved_current_working_directory
        self._toolkits = _build_ask_toolkits(
            model=model,
            current_working_directory=resolved_current_working_directory,
            create_samples_path=create_samples_path,
        )
        self._status_adapter = _StatusAwareLLMAdapter(delegate=llm_adapter)
        self._process = LLMAgentProcess(
            thread_id=self._thread_id,
            participant=self._participant,
            llm_adapter=self._status_adapter,
            toolkits=self._toolkits,
            turn_instructions_provider=_build_ask_turn_instructions_provider(
                current_working_directory=resolved_current_working_directory,
                create_samples_path=create_samples_path,
                interactive=interactive,
                preamble_rule=preamble_rule,
            ),
        )
        self._supervisor = _AskSupervisor(process=self._process)
        self._channel_client = LocalChatClient(
            thread_path=self._thread_id,
            send_message=self._supervisor.send,
            events=self._supervisor.events,
        )
        self._session = self._channel_client.thread_session

    @property
    def thread_session(self) -> ChatThreadSession:
        return self._session

    @property
    def current_working_directory(self) -> str:
        return self._current_working_directory

    @property
    def create_samples_path(self) -> str:
        return self._create_samples_path

    @property
    def thread_status_text(self) -> str | None:
        return self._session.thread_status_text

    @property
    def thread_status(self) -> AgentThreadStatus | None:
        return self._session.thread_status

    @property
    def queued_message_labels(self) -> tuple[str, ...]:
        return self._session.queued_message_labels

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._session.messages

    async def __aenter__(self) -> _AskSession:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        await self.stop()

    async def start(self) -> None:
        await self._channel_client.start()
        self._status_adapter.set_status_callback(self._handle_adapter_status)
        await self._supervisor.start()

    async def stop(self) -> None:
        try:
            self._status_adapter.set_status_callback(None)
            await self._session.close(close_client=False)
            await self._channel_client.close()
            await self._supervisor.stop()
        finally:
            self._resource_stack.close()

    def _handle_adapter_status(self, status: str) -> None:
        self._session.add_agent_message(
            AgentThreadStatus(
                type=AGENT_EVENT_THREAD_STATUS,
                thread_id=self._thread_id,
                status=status,
            )
        )

    async def ask(
        self,
        *,
        prompt: str,
        attachments: Sequence[_AskInputAttachment] = (),
        on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    ) -> str:
        async def _handle_adapter_status(status: str) -> None:
            await _emit_agent_message(
                on_message,
                AgentThreadStatus(
                    type=AGENT_EVENT_THREAD_STATUS,
                    thread_id=self._thread_id,
                    status=status,
                ),
            )

        self._status_adapter.set_status_callback(
            None
            if on_message is None
            else lambda status: asyncio.create_task(_handle_adapter_status(status))
        )
        file_attachments = [
            AgentFileContent(type="file", url=attachment.uri, name=attachment.name)
            for attachment in attachments
        ]
        try:
            return await self._session.ask(
                prompt=prompt,
                attachments=file_attachments,
                model=self._model,
                on_message=on_message,
            )
        finally:
            self._status_adapter.set_status_callback(None)

    def steer(
        self,
        *,
        prompt: str,
        on_accepted: Callable[[], Awaitable[None] | None] | None = None,
        on_applied: Callable[[], Awaitable[None] | None] | None = None,
        on_rejected: Callable[[RoomException], Awaitable[None] | None] | None = None,
    ) -> str | None:
        return self._session.steer(
            prompt=prompt,
            on_accepted=on_accepted,
            on_applied=on_applied,
            on_rejected=on_rejected,
        )

    def interrupt(self) -> bool:
        return self._session.interrupt()


def _build_ask_adapter(
    *,
    model: str,
    project_id: str,
    access_token: str,
) -> LLMAdapter:
    default_headers = {_MESHAGENT_PROJECT_ID_HEADER: project_id}

    if model.startswith("claude-"):
        from anthropic import AsyncAnthropic
        from meshagent.anthropic import AnthropicOpenAIResponsesStreamAdapter
        from meshagent.anthropic.proxy import resolve_base_url

        client = AsyncAnthropic(
            base_url=resolve_base_url(None),
            api_key=access_token,
            default_headers=default_headers,
        )
        return AnthropicOpenAIResponsesStreamAdapter(
            model=model,
            api_key=access_token,
            client=client,
        )

    from meshagent.openai import OpenAIResponsesAdapter
    from meshagent.openai.proxy import resolve_base_url
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=resolve_base_url(None),
        api_key=access_token,
        default_headers=default_headers,
    )
    return OpenAIResponsesAdapter(
        model=model,
        api_key=access_token,
        client=client,
    )


def _should_launch_tui(
    *,
    message: str | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    return message is None and stdin_is_tty and stdout_is_tty


async def _resolve_ask_access_token() -> str | None:
    env_token = os.environ.get(_MESHAGENT_TOKEN_ENV)
    if env_token is not None:
        normalized_env_token = env_token.strip()
        if normalized_env_token != "":
            return normalized_env_token

    access_token = await auth_async.get_access_token()
    if access_token is None:
        return None

    normalized_access_token = access_token.strip()
    if normalized_access_token == "":
        return None
    return normalized_access_token


def _enter_create_samples_path(resource_stack: contextlib.ExitStack) -> str:
    create_samples_resource = resources.files(CREATE_TEMPLATE_PACKAGE)
    create_samples_path = resource_stack.enter_context(
        resources.as_file(create_samples_resource)
    )
    return os.path.abspath(os.fspath(create_samples_path))


def _build_ask_toolkits(
    *,
    model: str,
    current_working_directory: str,
    create_samples_path: str | None = None,
) -> list[Toolkit]:
    from meshagent.anthropic.web_fetch import WebFetchTool as AnthropicWebFetchTool
    from meshagent.anthropic.web_search import (
        WebSearchTool as AnthropicWebSearchTool,
    )
    from meshagent.openai.tools.responses_adapter import ApplyPatchTool, WebSearchTool
    from meshagent.tools.web_toolkit import WebFetchTool

    storage_mounts = [
        StorageToolLocalMount(
            path=current_working_directory,
            local_path=current_working_directory,
        )
    ]
    if create_samples_path is not None:
        storage_mounts.append(
            StorageToolLocalMount(
                path=create_samples_path,
                local_path=create_samples_path,
                read_only=True,
            )
        )

    storage_toolkit = StorageToolkit(
        mounts=storage_mounts,
    )
    toolkits: list[Toolkit] = [storage_toolkit]

    if model.startswith("claude-"):
        toolkits.append(Toolkit(name="web_fetch", tools=[AnthropicWebFetchTool()]))
        toolkits.append(Toolkit(name="web_search", tools=[AnthropicWebSearchTool()]))
        return toolkits

    toolkits.append(
        Toolkit(name="apply_patch", tools=[ApplyPatchTool(storage=storage_toolkit)])
    )
    toolkits.append(Toolkit(name="web_fetch", tools=[WebFetchTool()]))
    toolkits.append(Toolkit(name="web_search", tools=[WebSearchTool()]))
    return toolkits


def _build_ask_instructions(
    *,
    current_working_directory: str,
    create_samples_path: str | None = None,
    interactive: bool = True,
    preamble_rule: bool = True,
) -> str:
    sections = [
        (
            "You are the MeshAgent assistant. You can find out more about "
            "meshagent at docs.meshagent.com and www.meshagent.com."
        ),
        (
            "If asked for coding assistance, refer to the installed meshagent sdk "
            "for questions about agents, rooms, datasets, queues, file storage, "
            "and related APIs. If the project doesn't already have the meshagent "
            "sdk installed, ask the user if they would like you to add it to "
            "their project for them if it seems like they are asking for "
            "meshagent coding help."
        ),
        (
            "You can also use the meshagent cli to perform tasks. You can get "
            "help for the sdk and its subcommands with the --help flag. "
            "meshagent create has an interactive mode, and users generally "
            "want that mode when creating sample projects unless they ask for "
            "a fully scripted flow."
        ),
        (
            "When discussing deployment, assume the user wants to deploy with "
            "meshagent and its built-in deployment flow. Do not recommend "
            "third-party deployment services unless the user asks for them or "
            "the project clearly requires one."
        ),
        (
            "The current working directory is "
            f"{current_working_directory}. "
            "You have a storage toolkit mounted at that same absolute path, "
            "so read and write files there using that exact path."
        ),
    ]
    if create_samples_path is not None:
        sections.append(
            "The embedded meshagent create sample projects are mounted read-only "
            f"at {create_samples_path}. Grep and read those examples when "
            "answering questions about generated project structure, generated "
            "files, or MeshAgent sample code."
        )
    if not interactive:
        sections.append(
            "You are not being run interactively. Return useful and actionable "
            "information rather than asking for user input."
        )
    if preamble_rule:
        sections.append(DEFAULT_PREAMBLE_RULE)
    return "\n\n".join(sections)


def _build_ask_turn_instructions_provider(
    *,
    current_working_directory: str,
    create_samples_path: str | None = None,
    interactive: bool,
    preamble_rule: bool = True,
) -> TurnInstructionsProvider:
    instructions = _build_ask_instructions(
        current_working_directory=current_working_directory,
        create_samples_path=create_samples_path,
        interactive=interactive,
        preamble_rule=preamble_rule,
    )

    async def provide_instructions(sender: Participant | None) -> str:
        del sender
        return instructions

    return provide_instructions


async def _run_ask_process(
    *,
    prompt: str,
    model: str,
    llm_adapter: LLMAdapter,
    on_message: Callable[[AgentMessage], Awaitable[None] | None] | None = None,
    preamble_rule: bool = True,
) -> str:
    with _suppress_ask_process_logs():
        async with _AskSession(
            model=model,
            llm_adapter=llm_adapter,
            interactive=False,
            preamble_rule=preamble_rule,
        ) as session:
            return await session.ask(prompt=prompt, on_message=on_message)


def _is_cancelled_turn_error(error: Exception) -> bool:
    return isinstance(error, RoomException) and error.code == "cancelled"


_ASK_SUPPRESSED_LOGGER_NAMES = ("agent-process", "openai_agent")


@contextlib.contextmanager
def _suppress_ask_process_logs() -> Any:
    loggers = [logging.getLogger(name) for name in _ASK_SUPPRESSED_LOGGER_NAMES]
    previous_disabled = {logger: logger.disabled for logger in loggers}
    for logger in loggers:
        logger.disabled = True
    try:
        yield
    finally:
        for logger, disabled in previous_disabled.items():
            logger.disabled = disabled


def _suppress_textual_debug_features() -> None:
    raw_features = os.environ.get("TEXTUAL")
    if raw_features is None or raw_features.strip() == "":
        return

    parsed = [value.strip() for value in raw_features.split(",") if value.strip() != ""]
    if len(parsed) == 0:
        return

    filtered = [value for value in parsed if value.lower() not in ("debug", "devtools")]
    if len(filtered) == len(parsed):
        return

    if len(filtered) == 0:
        os.environ.pop("TEXTUAL", None)
    else:
        os.environ["TEXTUAL"] = ",".join(filtered)


async def _run_ask_tui(
    *,
    model: str,
    llm_adapter: LLMAdapter | None = None,
    session: Any | None = None,
    session_provider: Callable[[], Any] | None = None,
    thread_generation_provider: Callable[[], int] | None = None,
    current_working_directory: str | None = None,
    image_dataset_client: ImageDatasetClient | None = None,
    title: str = "meshagent ask",
    assistant_name: str = "assistant",
    preamble_rule: bool = True,
    command_handler: Callable[[str], Awaitable[str | None] | str | None] | None = None,
    model_label_provider: Callable[[], str | None] | None = None,
    output_label_provider: Callable[[], str | None] | None = None,
    command_options_provider: Callable[[str], Sequence[AskCommandOption]] | None = None,
    command_options_loader: Callable[
        [str], Awaitable[Sequence[AskCommandOption]] | Sequence[AskCommandOption]
    ]
    | None = None,
    side_panel_renderer: Callable[..., Any] | None = None,
    side_panel_key_handler: Callable[[str, str | None], Awaitable[bool] | bool]
    | None = None,
    side_panel_mouse_handler: Callable[[int, int], Awaitable[bool] | bool]
    | None = None,
) -> None:
    try:
        from rich.text import Text
        from rich.style import Style
        from textual import events
        from textual._context import active_app
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical, VerticalScroll
        from textual.screen import Screen
        from textual.widgets import Markdown as TextualMarkdown, Static, TextArea
        from textual.widgets._text_area import TextAreaTheme
        from textual_image.widget import Image as TextualTerminalImage
    except ImportError as exc:
        typer.echo(
            "Textual is required for interactive ask mode. Install meshagent-cli dependencies and retry."
        )
        raise typer.Exit(1) from exc

    _suppress_textual_debug_features()

    @dataclass(slots=True)
    class _AskFeedEntry:
        role: str
        text: str = ""
        pending: bool = False
        kind: Literal["text", "image", "diff"] = "text"
        message_id: str | None = None
        image_renderable: Any | None = None

    @dataclass(slots=True)
    class _RenderedAskFeedEntry:
        entry: _AskFeedEntry
        widget: Vertical
        body_widget: Any

    def _ask_terminal_image_widget(image: _AskImagePreview, **kwargs: Any) -> Any:
        from PIL import Image

        class _AskTerminalImage(
            TextualTerminalImage,
            Renderable=TextualTerminalImage._Renderable,
        ):
            def __init__(
                self,
                terminal_image: Image.Image,
                *,
                preview: _AskImagePreview,
                **image_kwargs: Any,
            ) -> None:
                self._preview = preview
                super().__init__(terminal_image, **image_kwargs)

            def get_content_width(self, container: Any, viewport: Any) -> int:
                del container, viewport
                return self._preview.columns

            def get_content_height(
                self,
                container: Any,
                viewport: Any,
                width: int,
            ) -> int:
                del container, viewport, width
                return self._preview.rows

        with Image.open(io.BytesIO(image.data)) as opened_image:
            terminal_image = opened_image.copy()
        widget = _AskTerminalImage(terminal_image, preview=image, **kwargs)
        widget.styles.width = image.columns
        widget.styles.height = image.rows
        widget.styles.max_height = max(1, image.rows)
        return widget

    def _ask_feed_image_spacer() -> Any:
        return Static(" ", classes="feed-entry-body")

    def _ask_fullscreen_image_preview(
        image: _AskImagePreview,
        *,
        columns: int,
        rows: int,
    ) -> _AskImagePreview:
        preview = _image_preview_from_record(
            ImageDatasetRecord(data=image.data, mime_type="image/png"),
            columns=max(1, columns),
            max_rows=max(1, rows),
        )
        return preview if preview is not None else image

    def _pdf_text_pages(text: str | None) -> tuple[str, ...]:
        if text is None or text.strip() == "":
            return ()
        form_pages = [page.strip() for page in text.split("\f") if page.strip() != ""]
        if len(form_pages) > 1:
            return tuple(form_pages)
        lines = text.strip().splitlines()
        page_size = 48
        return tuple(
            "\n".join(lines[index : index + page_size]).strip()
            for index in range(0, len(lines), page_size)
            if "\n".join(lines[index : index + page_size]).strip() != ""
        )

    class _AskPdfViewer(Screen[None]):
        CSS = """
        #pdf-viewer-header {
            width: 100%;
            height: 1;
            background: #1a1d22;
            color: #cfd3dc;
        }
        #pdf-viewer-scroll {
            width: 100%;
            height: 1fr;
            padding: 0;
            align: center middle;
        }
        #pdf-viewer-body {
            width: auto;
            height: auto;
        }
        .pdf-viewer-image {
            width: auto;
            height: auto;
        }
        """

        BINDINGS = [
            Binding("escape", "close", "Close", priority=True),
            Binding("q", "close", "Close", priority=True),
            Binding("right", "next_page", "Next", priority=True),
            Binding("pagedown", "next_page", "Next", priority=True),
            Binding("space", "next_page", "Next", priority=True),
            Binding("left", "previous_page", "Previous", priority=True),
            Binding("pageup", "previous_page", "Previous", priority=True),
        ]

        def __init__(self, preview: _AskPdfPreview) -> None:
            super().__init__()
            self._preview = preview
            self._page_index = 0
            self._body: Vertical | None = None
            self._header: Static | None = None
            self._text_pages = _pdf_text_pages(preview.text)

        def compose(self) -> ComposeResult:
            yield Static("", id="pdf-viewer-header")
            with VerticalScroll(id="pdf-viewer-scroll"):
                yield Vertical(id="pdf-viewer-body")

        async def on_mount(self) -> None:
            self._header = self.query_one("#pdf-viewer-header", Static)
            self._body = self.query_one("#pdf-viewer-body", Vertical)
            await self._render_page()

        async def on_resize(self, event: events.Resize) -> None:
            del event
            await self._render_page()

        def _page_count(self) -> int:
            if len(self._preview.pages) > 0:
                return len(self._preview.pages)
            return max(1, len(self._text_pages))

        async def _render_page(self) -> None:
            if self._header is not None:
                self._header.update(
                    f" {self._preview.name}  "
                    f"{self._page_index + 1}/{self._page_count()}  "
                    "←/→ page  esc close"
                )
            if self._body is None:
                return
            await self._body.remove_children()
            if len(self._preview.pages) > 0:
                await self._body.mount(
                    _ask_terminal_image_widget(
                        _ask_fullscreen_image_preview(
                            self._preview.pages[self._page_index],
                            columns=self.size.width,
                            rows=max(1, self.size.height - 1),
                        ),
                        classes="pdf-viewer-image",
                    )
                )
                return
            text = (
                self._text_pages[self._page_index]
                if len(self._text_pages) > 0
                else "Unable to render PDF preview."
            )
            await self._body.mount(Static(Text.from_ansi(text)))

        async def action_next_page(self) -> None:
            self._page_index = min(self._page_count() - 1, self._page_index + 1)
            await self._render_page()

        async def action_previous_page(self) -> None:
            self._page_index = max(0, self._page_index - 1)
            await self._render_page()

        def key_escape(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            self.dismiss(None)

        def key_q(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            self.dismiss(None)

        async def key_right(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            await self.action_next_page()

        async def key_pagedown(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            await self.action_next_page()

        async def key_space(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            await self.action_next_page()

        async def key_left(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            await self.action_previous_page()

        async def key_pageup(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            await self.action_previous_page()

        def action_close(self) -> None:
            self.dismiss(None)

    class _AskImageViewer(Screen[None]):
        CSS = """
        #image-viewer-scroll {
            width: 100%;
            height: 100%;
            padding: 0;
            align: center middle;
        }
        #image-viewer-body {
            width: auto;
            height: auto;
        }
        .image-viewer-image {
            width: auto;
            height: auto;
        }
        """

        BINDINGS = [
            Binding("escape", "close", "Close", priority=True),
            Binding("q", "close", "Close", priority=True),
        ]

        def __init__(self, preview: _AskImagePreview) -> None:
            super().__init__()
            self._preview = preview
            self._body: Vertical | None = None

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="image-viewer-scroll"):
                yield Vertical(id="image-viewer-body")

        async def on_mount(self) -> None:
            self._body = self.query_one("#image-viewer-body", Vertical)
            await self._render_image()

        async def on_resize(self, event: events.Resize) -> None:
            del event
            await self._render_image()

        async def _render_image(self) -> None:
            if self._body is None:
                return
            await self._body.remove_children()
            await self._body.mount(
                _ask_terminal_image_widget(
                    _ask_fullscreen_image_preview(
                        self._preview,
                        columns=self.size.width,
                        rows=self.size.height,
                    ),
                    classes="image-viewer-image",
                )
            )

        def key_escape(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            self.dismiss(None)

        def key_q(self, event: events.Key) -> None:
            event.prevent_default()
            event.stop()
            self.dismiss(None)

        def action_close(self) -> None:
            self.dismiss(None)

    class _AskImagePreviewWidget(Vertical):
        can_focus = True

        def __init__(self, preview: _AskImagePreview) -> None:
            self._preview = preview
            super().__init__(
                _ask_feed_image_spacer(),
                _ask_terminal_image_widget(
                    preview,
                    classes="feed-entry-image",
                ),
                classes="feed-entry-images",
            )

        def on_click(self, event: Any) -> None:
            event.prevent_default()
            event.stop()
            self.app.push_screen(_AskImageViewer(self._preview))

    class _AskPdfPreviewWidget(Vertical):
        can_focus = True

        def __init__(self, preview: _AskPdfPreview) -> None:
            self._preview = preview
            children: list[Any] = [
                Static(
                    Text(f"[{preview.name}]", style="cyan underline"),
                    classes="feed-entry-body",
                )
            ]
            if len(preview.pages) > 0:
                children.append(_ask_feed_image_spacer())
                children.append(
                    _ask_terminal_image_widget(
                        preview.pages[0],
                        classes="feed-entry-image",
                    )
                )
            elif preview.text is not None:
                children.append(_ask_feed_image_spacer())
                children.append(
                    Static(
                        Text.from_ansi(preview.text[:4000]),
                        classes="feed-entry-body",
                    )
                )
            super().__init__(*children, classes="feed-entry-images")

        def on_click(self, event: Any) -> None:
            event.prevent_default()
            event.stop()
            self.app.push_screen(_AskPdfViewer(self._preview))

    class _AskInputTextArea(TextArea):
        BINDINGS = [
            *TextArea.BINDINGS,
            Binding("cmd+v", "paste", show=False),
            Binding("meta+v", "paste", show=False),
        ]
        _placeholder_style_name = "meshagent.image-placeholder"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.register_theme(
                TextAreaTheme(
                    name="meshagent-ask-input",
                    base_style=Style(color="white", bgcolor="#1a1d22"),
                    cursor_line_style=Style(bgcolor="#1a1d22"),
                    gutter_style=Style(bgcolor="#1a1d22"),
                    cursor_line_gutter_style=Style(bgcolor="#1a1d22"),
                    syntax_styles={
                        self._placeholder_style_name: Style(color="#7dd3fc", bold=True)
                    },
                )
            )
            self.theme = "meshagent-ask-input"

        def _build_highlight_map(self) -> None:
            super()._build_highlight_map()
            for line_number, line in enumerate(self.document.lines):
                for match in _ASK_INPUT_ATTACHMENT_PLACEHOLDER_RE.finditer(line):
                    self._highlights[line_number].append(
                        (
                            match.start(),
                            match.end(),
                            self._placeholder_style_name,
                        )
                    )

        def on_paste(self, event: events.Paste) -> None:
            app = self.app
            if not isinstance(app, _AskTextualApp):
                return
            if not app._handle_input_paste(event.text, source="input-textarea"):
                return
            event.prevent_default()
            event.stop()

        async def _on_paste(self, event: events.Paste) -> None:
            app = self.app
            if isinstance(app, _AskTextualApp) and app._handle_input_paste(
                event.text, source="input-textarea-private"
            ):
                event.prevent_default()
                event.stop()
                return
            await super()._on_paste(event)

        async def action_paste(self) -> None:
            app = self.app
            if isinstance(
                app, _AskTextualApp
            ) and app._handle_clipboard_attachment_paste(
                source="input-textarea-action"
            ):
                return
            result = super().action_paste()
            if inspect.isawaitable(result):
                await result

    @dataclass(slots=True)
    class _AskToolCallState:
        toolkit: str
        tool: str
        arguments: dict[str, Any] | None = None
        argument_delta_text: str = ""
        logs: list[str] | None = None

    @dataclass(slots=True)
    class _AskUsageState:
        context_used_tokens: int
        context_total_tokens: int | None
        compaction_mode: str | None
        compaction_threshold: int | None
        total_tokens: float

    @dataclass(slots=True)
    class _CommandSelectorState:
        prompt: str
        options: list[AskCommandOption]
        selected_index: int = 0

    class _AskSidePanel(Static):
        can_focus = True

        def on_click(self, event: Any) -> None:
            event.prevent_default()
            event.stop()
            self.focus()
            app = self.app
            if isinstance(app, _AskTextualApp):
                app._submit_side_panel_click(x=event.x, y=event.y)

        def on_mouse_scroll_up(self, event: Any) -> None:
            event.prevent_default()
            event.stop()
            app = self.app
            if isinstance(app, _AskTextualApp):
                app._submit_side_panel_key("scroll_up", None)

        def on_mouse_scroll_down(self, event: Any) -> None:
            event.prevent_default()
            event.stop()
            app = self.app
            if isinstance(app, _AskTextualApp):
                app._submit_side_panel_key("scroll_down", None)

    class _AskTextualApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
            padding: 0;
            background: #101114;
            color: white;
        }
        #app-row {
            width: 100%;
            height: 100%;
        }
        #main-panel {
            width: 4fr;
            height: 100%;
            layout: grid;
            grid-size: 1 6;
            grid-rows: 1fr 3 auto auto auto auto;
        }
        #feed-scroll {
            width: 4fr;
            height: 1fr;
            align: left bottom;
        }
        #content-row {
            width: 100%;
            height: 1fr;
        }
        #side-panel {
            display: none;
            width: 1fr;
            height: 100%;
            padding: 1 1 0 1;
            border-left: solid #2d3138;
            background: #101114;
            color: #cfd3dc;
        }
        #side-panel.side-panel--visible {
            display: block;
        }
        #side-panel.side-panel--focused {
            border-left: solid #7dd3fc;
        }
        #feed {
            width: 100%;
            height: auto;
        }
        .feed-entry {
            width: 100%;
            height: auto;
            padding: 1 2 0 2;
        }
        .feed-entry--continued {
            padding: 1 2 0 2;
        }
        .feed-entry--you {
            background: #2d3138;
            padding: 1 2 1 2;
        }
        .feed-entry-header {
            width: 100%;
            color: cyan;
            text-style: bold;
        }
        .feed-entry--you .feed-entry-header {
            color: white;
        }
        .feed-entry--error .feed-entry-header {
            color: red;
        }
        .feed-entry-body {
            width: 100%;
        }
        .feed-entry-image {
            width: auto;
            height: auto;
        }
        .feed-entry-images {
            width: auto;
            height: auto;
        }
        .feed-entry--you .feed-entry-body {
            color: white;
        }
        .feed-entry--error .feed-entry-body {
            color: red;
        }
        .feed-entry-markdown {
            width: 100%;
            padding: 0;
            background: transparent;
        }
        .feed-entry--you .feed-entry-markdown {
            color: white;
        }
        .feed-entry--error .feed-entry-markdown {
            color: red;
        }
        .feed-event-break {
            width: 100%;
            height: 1;
        }
        #active-assistant-event-break {
            display: none;
        }
        #active-assistant-entry {
            display: none;
            width: 100%;
            height: auto;
            padding: 1 2 0 2;
        }
        #active-assistant-header {
            display: none;
            width: 100%;
            padding: 0;
            color: cyan;
        }
        #active-assistant-body {
            display: block;
            width: 100%;
            padding: 0;
            background: transparent;
        }
        #status-line {
            width: 100%;
            height: 3;
            padding: 0 2 0 2;
            color: #9aa5b8;
            background: #101114;
        }
        #turn-queue {
            display: none;
            width: 100%;
            padding: 0 2 1 2;
            color: #9aa5b8;
        }
        #command-menu {
            display: none;
            width: 100%;
            padding: 0 2 1 2;
            color: #cfd3dc;
            background: #101114;
        }
        #input-row {
            margin: 0;
            background: #1a1d22;
            padding: 1 0 1 0;
        }
        #session-meta {
            width: 100%;
            padding: 0 2 0 2;
            color: #7e8699;
            background: #101114;
        }
        #input-prompt {
            width: 2;
            height: auto;
            content-align: center top;
            color: #8b93a5;
            background: #1a1d22;
        }
        #ask-input {
            width: 1fr;
            height: 1;
            min-height: 1;
            max-height: 8;
            border: none;
            outline: none;
            padding: 0;
            margin: 0;
            color: white;
            background: #1a1d22;
            background-tint: 0%;
        }
        #ask-input:focus {
            border: none;
            background: #1a1d22;
            background-tint: 0%;
        }
        #ask-input .text-area--cursor-line {
            background: #1a1d22;
        }
        #ask-input .text-area--gutter {
            background: #1a1d22;
        }
        #ask-input .text-area--cursor-gutter {
            background: #1a1d22;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit_app", "Quit", priority=True),
            Binding("tab", "toggle_side_panel_focus", "Focus", priority=True),
            Binding("escape", "interrupt_turn", "Interrupt", priority=True),
            Binding("shift+enter", "insert_newline", show=False, priority=True),
            Binding("enter", "submit_prompt", "Send", priority=True),
        ]

        def __init__(
            self,
            *,
            session: Any,
            session_provider: Callable[[], Any],
            thread_generation_provider: Callable[[], int] | None,
            title: str,
            assistant_name: str,
            current_working_directory: str,
            image_dataset_client: ImageDatasetClient | None,
            command_handler: Callable[[str], Awaitable[str | None] | str | None] | None,
            model_label_provider: Callable[[], str | None] | None,
            output_label_provider: Callable[[], str | None] | None,
            command_options_provider: Callable[[str], Sequence[AskCommandOption]]
            | None,
            command_options_loader: Callable[
                [str],
                Awaitable[Sequence[AskCommandOption]] | Sequence[AskCommandOption],
            ]
            | None,
            side_panel_renderer: Callable[..., Any] | None,
            side_panel_key_handler: Callable[[str, str | None], Awaitable[bool] | bool]
            | None,
            side_panel_mouse_handler: Callable[[int, int], Awaitable[bool] | bool]
            | None,
        ) -> None:
            super().__init__()
            self._session_fallback = session
            self._session_provider = session_provider
            self._thread_generation_provider = thread_generation_provider
            self._title = title
            self._assistant_name = assistant_name
            self._current_working_directory = current_working_directory
            self._image_dataset_client = image_dataset_client
            self._command_handler = command_handler
            self._model_label_provider = model_label_provider
            self._output_label_provider = output_label_provider
            self._command_options_provider = command_options_provider
            self._command_options_loader = command_options_loader
            self._side_panel_renderer = side_panel_renderer
            self._side_panel_key_handler = side_panel_key_handler
            self._side_panel_mouse_handler = side_panel_mouse_handler
            self._side_panel_enabled = False
            self._entries: list[_AskFeedEntry] = []
            self._feed_view: Vertical | None = None
            self._feed_scroll: VerticalScroll | None = None
            self._side_panel_view: Static | None = None
            self._active_assistant_event_break: Static | None = None
            self._active_assistant_entry_view: Vertical | None = None
            self._active_assistant_header: Static | None = None
            self._active_assistant_body: TextualMarkdown | None = None
            self._active_assistant_stream = None
            self._active_assistant_text = ""
            self._active_assistant_name: str | None = None
            self._active_assistant_item_id: str | None = None
            self._status_view: Static | None = None
            self._queue_view: Static | None = None
            self._command_menu_view: Static | None = None
            self._input_row: Horizontal | None = None
            self._input_view: TextArea | None = None
            self._session_meta_view: Static | None = None
            self._input_height = 1
            self._rendered_entry_count = 0
            self._rendered_feed_entries_by_message_id: dict[
                str, _RenderedAskFeedEntry
            ] = {}
            self._submit_task: asyncio.Task[None] | None = None
            self._pending = False
            self._external_queued_messages: list[str] = []
            self._rendered_session_message_ids: set[str] = set()
            self._pending_session_image_message_ids: set[str] = set()
            self._external_thread_active = False
            self._active_assistant_entry: _AskFeedEntry | None = None
            self._thread_status_entry_id = "__meshagent_ask_thread_status__"
            self._status_text: str | None = None
            self._usage_state: _AskUsageState | None = None
            self._reasoning_parts: dict[str, list[str]] = {}
            self._tool_calls: dict[str, _AskToolCallState] = {}
            self._status_started_at: float | None = None
            self._status_gradient_offset = 0
            self._spinner_timer = None
            self._active_command_option: AskCommandOption | None = None
            self._command_selector: _CommandSelectorState | None = None
            self._audio_player = _StreamingAudioPlayer()
            self._audio_error_reported = False
            self._side_panel_focused = False
            self._thread_generation: int | None = None
            self._input_attachments: list[_AskInputAttachment] = []
            self._next_input_image_number = 1
            self._attaching_pasted_images = False

        @property
        def _session(self) -> Any:
            return self._session_provider()

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-row"):
                with Vertical(id="main-panel"):
                    with Horizontal(id="content-row"):
                        with VerticalScroll(id="feed-scroll"):
                            yield Vertical(id="feed")
                            yield Static("", id="active-assistant-event-break")
                            with Vertical(
                                id="active-assistant-entry", classes="feed-entry"
                            ):
                                yield Static("", id="active-assistant-header")
                                yield TextualMarkdown(
                                    "",
                                    id="active-assistant-body",
                                    classes="feed-entry-body feed-entry-markdown",
                                )
                    yield Static("", id="status-line")
                    yield Static("", id="turn-queue")
                    yield Static("", id="command-menu")
                    with Horizontal(id="input-row"):
                        yield Static("›", id="input-prompt")
                        yield _AskInputTextArea(
                            "",
                            id="ask-input",
                            soft_wrap=True,
                            show_line_numbers=False,
                        )
                    yield Static("", id="session-meta")
                yield _AskSidePanel("", id="side-panel")

        async def on_mount(self) -> None:
            self._feed_view = self.query_one("#feed", Vertical)
            self._feed_scroll = self.query_one("#feed-scroll", VerticalScroll)
            self._side_panel_view = self.query_one("#side-panel", _AskSidePanel)
            self._active_assistant_event_break = self.query_one(
                "#active-assistant-event-break", Static
            )
            self._active_assistant_entry_view = self.query_one(
                "#active-assistant-entry", Vertical
            )
            self._active_assistant_header = self.query_one(
                "#active-assistant-header", Static
            )
            self._active_assistant_body = self.query_one(
                "#active-assistant-body", TextualMarkdown
            )
            self._status_view = self.query_one("#status-line", Static)
            self._queue_view = self.query_one("#turn-queue", Static)
            self._command_menu_view = self.query_one("#command-menu", Static)
            self._input_row = self.query_one("#input-row", Horizontal)
            self._input_view = self.query_one("#ask-input", TextArea)
            self._session_meta_view = self.query_one("#session-meta", Static)
            self._input_view.focus()
            self._resize_input(self._input_view)
            self._spinner_timer = self.set_interval(0.12, self._on_spinner_tick)
            self._sync_external_thread_state()
            self._render_feed()
            self._render_status_line()
            self._render_turn_queue()
            self._render_command_menu()
            self._render_session_meta()
            self._render_side_panel()

        async def on_unmount(self) -> None:
            await self._audio_player.close()
            await self._stop_active_assistant_stream()
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            if self._submit_task is not None and not self._submit_task.done():
                self._submit_task.cancel()
                await asyncio.gather(self._submit_task, return_exceptions=True)
                self._submit_task = None

        async def action_quit_app(self) -> None:
            self.exit()

        async def action_interrupt_turn(self) -> None:
            if isinstance(self.screen, (_AskImageViewer, _AskPdfViewer)):
                self.screen.dismiss(None)
                return
            if self._side_panel_focused:
                self._submit_side_panel_key("escape", None)
                return
            if self._command_selector is not None:
                self._close_command_selector()
                return
            if not (self._pending or self._external_thread_active):
                return
            if self._session.interrupt():
                self._set_status_text("Interrupting")

        def on_key(self, event: Any) -> None:
            if self._side_panel_focused:
                event.prevent_default()
                event.stop()
                self._submit_side_panel_key(event.key, event.character)
                return
            if self._command_selector is None:
                return
            if event.key == "up":
                event.stop()
                self._select_previous_command_option()
            elif event.key == "down":
                event.stop()
                self._select_next_command_option()

        async def action_toggle_side_panel_focus(self) -> None:
            if self._side_panel_renderer is None:
                return
            if self._command_selector is not None:
                self._close_command_selector()
            self._side_panel_focused = not self._side_panel_focused
            self._render_side_panel()
            if self._side_panel_focused and self._side_panel_view is not None:
                self._side_panel_view.focus()
            elif self._input_view is not None:
                self._input_view.focus()

        def _submit_side_panel_key(self, key: str, character: str | None) -> None:
            if self._side_panel_key_handler is None:
                return
            task = asyncio.create_task(
                self._run_side_panel_key_handler(key=key, character=character)
            )
            task.add_done_callback(_consume_task_exception)

        def _submit_side_panel_click(self, *, x: int, y: int) -> None:
            self._side_panel_focused = True
            self._render_side_panel()
            if self._side_panel_mouse_handler is None:
                return
            task = asyncio.create_task(self._run_side_panel_mouse_handler(x=x, y=y))
            task.add_done_callback(_consume_task_exception)

        async def _run_side_panel_mouse_handler(self, *, x: int, y: int) -> None:
            handler = self._side_panel_mouse_handler
            if handler is None:
                return
            try:
                handled = handler(x, y)
                if inspect.isawaitable(handled):
                    handled = await handled
                if handled:
                    self._side_panel_focused = False
                    self._render_side_panel()
                    self._sync_external_thread_state()
                    if self._input_view is not None:
                        self._input_view.focus()
            except Exception as exc:
                self._entries.append(_AskFeedEntry(role="error", text=str(exc)))
                self._render_feed()
                self._scroll_to_end()

        async def _run_side_panel_key_handler(
            self, *, key: str, character: str | None
        ) -> None:
            handler = self._side_panel_key_handler
            if handler is None:
                return
            try:
                handled = handler(key, character)
                if inspect.isawaitable(handled):
                    handled = await handled
                if handled:
                    if key == "enter":
                        self._side_panel_focused = False
                        if self._input_view is not None:
                            self._input_view.focus()
                    self._render_side_panel()
                    self._sync_external_thread_state()
            except Exception as exc:
                self._entries.append(_AskFeedEntry(role="error", text=str(exc)))
                self._render_feed()
                self._scroll_to_end()

        def _select_previous_command_option(self) -> None:
            selector = self._command_selector
            if selector is None:
                return
            selector.selected_index = max(0, selector.selected_index - 1)
            self._render_command_menu()

        def _select_next_command_option(self) -> None:
            selector = self._command_selector
            if selector is None:
                return
            selector.selected_index = min(
                len(selector.options) - 1,
                selector.selected_index + 1,
            )
            self._render_command_menu()

        async def action_insert_newline(self) -> None:
            if self._input_view is None or self.focused is not self._input_view:
                return
            self._input_view.insert("\n")
            self._resize_input(self._input_view)

        async def action_submit_prompt(self) -> None:
            if self._input_view is None:
                return

            if self._side_panel_focused:
                self._submit_side_panel_key("enter", None)
                return

            if self._command_selector is not None:
                await self._submit_command_selector_selection()
                return

            raw_prompt = self._input_view.text.rstrip()
            attachments = _ask_present_input_attachments(
                raw_prompt,
                self._input_attachments,
            )
            prompt = _ask_prompt_without_attachment_placeholders(
                raw_prompt,
                attachments,
            )
            if len(attachments) == 0 and await self._open_command_selector_if_available(
                prompt.strip()
            ):
                return

            self._input_view.load_text("")
            self._input_attachments.clear()
            self._resize_input(self._input_view)
            self._render_command_menu()
            self._input_view.focus()

            if prompt.strip() == "" and len(attachments) == 0:
                return
            if len(attachments) == 0 and prompt.strip() in {"/quit", "/exit"}:
                self.exit()
                return

            if len(attachments) == 0 and self._handle_builtin_command(prompt.strip()):
                return

            resolved_command = (
                None
                if len(attachments) > 0
                else self._resolve_command_submission(prompt.strip())
            )
            if resolved_command is not None and self._command_handler is not None:
                if self._pending:
                    self._entries.append(
                        _AskFeedEntry(
                            role="error",
                            text="Wait for the current turn to finish before running commands.",
                        )
                    )
                    self._render_feed()
                    self._scroll_to_end()
                    return
                self._submit_task = asyncio.create_task(
                    self._run_command(command=resolved_command)
                )
                return

            if self._pending:
                if len(attachments) > 0:
                    self._entries.append(
                        _AskFeedEntry(
                            role="error",
                            text="Wait for the current turn to finish before sending image attachments.",
                        )
                    )
                    self._render_feed()
                    self._scroll_to_end()
                    return
                self._steer_active_turn(prompt=prompt)
                return

            self._start_turn(prompt=prompt, attachments=attachments)

        def _handle_builtin_command(self, command: str) -> bool:
            if command == "/threads":
                self._side_panel_enabled = (
                    self._side_panel_renderer is not None
                    and not self._side_panel_enabled
                )
                self._side_panel_focused = False
                self._render_side_panel()
                if self._input_view is not None:
                    self._input_view.focus()
                return True
            if command == "/threads on":
                self._side_panel_enabled = self._side_panel_renderer is not None
                self._render_side_panel()
                return True
            if command == "/threads off":
                self._side_panel_enabled = False
                self._side_panel_focused = False
                self._render_side_panel()
                if self._input_view is not None:
                    self._input_view.focus()
                return True
            return False

        def _start_turn(
            self,
            *,
            prompt: str,
            attachments: Sequence[_AskInputAttachment] = (),
        ) -> None:
            if isinstance(self._session, ChatThreadSession):
                self._pending = True
                self._status_started_at = time.monotonic()
                self._set_status_text("Working")
                self._render_feed()
                self._render_turn_queue()
                self._scroll_to_end()
                self._submit_task = asyncio.create_task(
                    self._run_prompt(
                        prompt=prompt,
                        attachments=attachments,
                    )
                )
                return

            self._pending = True
            self._begin_active_assistant()
            self._status_started_at = time.monotonic()
            self._set_status_text("Working")
            self._render_feed()
            self._render_turn_queue()
            self._scroll_to_end()
            self._submit_task = asyncio.create_task(
                self._run_prompt(
                    prompt=prompt,
                    attachments=attachments,
                )
            )

        async def _run_command(self, *, command: str) -> None:
            self._pending = True
            self._status_started_at = time.monotonic()
            self._set_status_text("Running command")
            self._render_status_line()
            try:
                handler = self._command_handler
                if handler is None:
                    return
                result = handler(command)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, str) and result.strip() != "":
                    self._entries.append(
                        _AskFeedEntry(role="event", text=result.strip())
                    )
            except Exception as exc:
                self._entries.append(_AskFeedEntry(role="error", text=str(exc)))
            finally:
                self._pending = False
                self._status_started_at = None
                self._status_text = None
                self._submit_task = None
                self._sync_external_thread_state()
                self._render_status_line()
                self._render_feed()
                self._render_command_menu()
                self._render_session_meta()
                self._scroll_to_end()
                if self._input_view is not None:
                    self._input_view.focus()

        async def _open_command_selector_if_available(self, prompt: str) -> bool:
            if prompt not in {"/model", "/output"} or self._command_handler is None:
                return False
            options = self._command_options(prompt)
            if len(options) == 0 and not self._pending:
                loader = self._command_options_loader
                if loader is not None:
                    self._set_status_text("Loading options")
                    self._render_status_line()
                    try:
                        loaded = loader(prompt)
                        if inspect.isawaitable(loaded):
                            loaded = await loaded
                        options = list(loaded)
                    except Exception as exc:
                        self._entries.append(_AskFeedEntry(role="error", text=str(exc)))
                        self._status_text = None
                        self._render_status_line()
                        self._render_feed()
                        self._scroll_to_end()
                        return True
                    finally:
                        self._status_text = None
                        self._render_status_line()
            if len(options) == 0:
                return False
            self._open_command_selector(prompt=prompt, options=options)
            return True

        def _open_command_selector(
            self,
            *,
            prompt: str,
            options: Sequence[AskCommandOption] | None = None,
        ) -> None:
            if self._input_view is None:
                return
            if options is None:
                options = self._command_options(prompt)
            options_list = list(options)
            if len(options_list) == 0:
                return
            selected_index = next(
                (index for index, option in enumerate(options_list) if option.active),
                0,
            )
            self._command_selector = _CommandSelectorState(
                prompt=prompt,
                options=options_list,
                selected_index=selected_index,
            )
            self._input_view.load_text("")
            self._resize_input(self._input_view)
            if self._input_row is not None:
                self._input_row.styles.display = "none"
            self._render_command_menu()

        def _close_command_selector(self) -> None:
            self._command_selector = None
            if self._input_row is not None:
                self._input_row.styles.display = "block"
            self._render_command_menu()
            if self._input_view is not None:
                self._input_view.focus()

        async def _submit_command_selector_selection(self) -> None:
            selector = self._command_selector
            if selector is None:
                return
            selected_option = selector.options[selector.selected_index]
            command = selected_option.command
            self._close_command_selector()
            if self._pending:
                self._entries.append(
                    _AskFeedEntry(
                        role="error",
                        text="Wait for the current turn to finish before changing settings.",
                    )
                )
                self._render_feed()
                self._scroll_to_end()
                return
            self._submit_task = asyncio.create_task(self._run_command(command=command))

        def _begin_active_assistant(self) -> None:
            self._active_assistant_text = ""
            self._active_assistant_name = None
            self._active_assistant_item_id = None
            if self._active_assistant_entry_view is not None:
                self._active_assistant_entry_view.styles.display = "block"
            self._render_active_assistant_event_break()
            if self._active_assistant_header is not None:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
            if self._active_assistant_body is not None:
                self._active_assistant_body.update("")
                self._active_assistant_stream = TextualMarkdown.get_stream(
                    self._active_assistant_body
                )
            self._render_active_assistant_header()

        def _resolve_command_submission(self, prompt: str) -> str | None:
            if not prompt.startswith("/") or self._command_handler is None:
                return None
            if self._active_command_option is not None:
                if prompt == self._active_command_option.command:
                    return self._active_command_option.command
            if " " in prompt:
                return prompt
            matches = [
                command
                for command, _description in _ASK_SLASH_COMMANDS
                if command.startswith(prompt)
            ]
            if len(matches) == 1:
                return matches[0]
            return prompt

        async def _stop_active_assistant_stream(self) -> None:
            if self._active_assistant_stream is not None:
                await self._active_assistant_stream.stop()
                self._active_assistant_stream = None

        def _stop_active_assistant_stream_in_background(self) -> None:
            active_stream = self._active_assistant_stream
            if active_stream is None:
                return
            self._active_assistant_stream = None
            task = asyncio.create_task(active_stream.stop())
            task.add_done_callback(_consume_task_exception)

        async def _run_prompt(
            self,
            *,
            prompt: str,
            attachments: Sequence[_AskInputAttachment] = (),
        ) -> None:
            try:
                session = self._session
                if isinstance(session, ChatThreadSession):
                    await self._run_chat_thread_prompt(
                        session=session,
                        prompt=prompt,
                        attachments=attachments,
                    )
                else:
                    await session.ask(
                        prompt=prompt,
                        attachments=attachments,
                        on_message=self._handle_agent_message,
                    )
            except asyncio.CancelledError:
                raise
            except RoomException as ex:
                if not _is_cancelled_turn_error(ex):
                    self._entries.append(_AskFeedEntry(role="error", text=str(ex)))
            except Exception as ex:
                self._entries.append(_AskFeedEntry(role="error", text=str(ex)))
            finally:
                await self._audio_player.close()
                await self._finalize_active_assistant()
                with self.batch_update():
                    self._active_assistant_entry = None
                    self._sync_session_messages()
                    self._pending = False
                    self._status_started_at = None
                    self._status_text = None
                    self._render_status_line()
                    self._render_feed()
                    self._render_turn_queue()
                    self._render_session_meta()
                await self._stop_active_assistant_stream()
                self._scroll_to_end()
                if self._input_view is not None:
                    self._input_view.focus()

        async def _run_chat_thread_prompt(
            self,
            *,
            session: ChatThreadSession,
            prompt: str,
            attachments: Sequence[_AskInputAttachment] = (),
        ) -> None:
            await _run_ask_chat_thread_prompt(
                session=session,
                prompt=prompt,
                attachments=attachments,
                on_message=self._handle_agent_message,
            )

        async def _finalize_active_assistant(self) -> None:
            active_text = self._active_assistant_text
            active_name = self._active_assistant_name
            active_item_id = self._active_assistant_item_id
            self._stop_active_assistant_stream_in_background()
            with self.batch_update():
                if active_text.strip() != "":
                    changed = self._append_or_replace_feed_entry(
                        _AskFeedEntry(
                            role=active_name or "agent",
                            text=active_text,
                            message_id=active_item_id,
                        ),
                    )
                    if changed:
                        self._render_feed()
                    if active_item_id is not None:
                        self._rendered_session_message_ids.add(active_item_id)
                if self._active_assistant_event_break is not None:
                    self._active_assistant_event_break.styles.display = "none"
                if self._active_assistant_entry_view is not None:
                    self._active_assistant_entry_view.styles.display = "none"
                if self._active_assistant_header is not None:
                    self._active_assistant_header.styles.display = "none"
                    self._active_assistant_header.update("")
                if self._active_assistant_body is not None:
                    self._active_assistant_body.update("")
            self._active_assistant_text = ""
            self._active_assistant_name = None
            self._active_assistant_item_id = None

        async def _prepare_active_assistant_text_item(
            self,
            *,
            item_id: str,
            sender_name: str | None = None,
        ) -> None:
            if (
                self._active_assistant_item_id is not None
                and self._active_assistant_item_id != item_id
            ):
                await self._finalize_active_assistant()
            elif (
                self._active_assistant_item_id is None
                and self._active_assistant_text.strip() != ""
            ):
                await self._finalize_active_assistant()

            self._active_assistant_item_id = item_id
            resolved_name = self._agent_message_sender_name(sender_name)
            if resolved_name is not None:
                self._active_assistant_name = resolved_name
            if self._active_assistant_entry_view is not None:
                self._active_assistant_entry_view.styles.display = "block"
            self._render_active_assistant_event_break()
            if self._active_assistant_body is not None:
                if self._active_assistant_stream is None:
                    self._active_assistant_stream = TextualMarkdown.get_stream(
                        self._active_assistant_body
                    )
            self._render_active_assistant_header()

        def _render_active_assistant_event_break(self) -> None:
            if self._active_assistant_event_break is None:
                return
            if (
                self._pending
                and len(self._entries) > 0
                and self._entries[-1].role == "event"
            ):
                self._active_assistant_event_break.styles.display = "block"
                return
            self._active_assistant_event_break.styles.display = "none"

        def _steer_active_turn(self, *, prompt: str) -> None:
            session = self._session
            if not isinstance(session, _AskSteerableSession):
                self._entries.append(
                    _AskFeedEntry(
                        role="error",
                        text="Wait for the current turn to finish.",
                    )
                )
                self._render_feed()
                self._scroll_to_end()
                return

            message_id = session.steer(
                prompt=prompt,
                on_accepted=self._handle_steer_accepted,
                on_applied=self._handle_steer_applied,
                on_rejected=self._handle_steer_rejected,
            )
            if message_id is None:
                self._entries.append(
                    _AskFeedEntry(
                        role="error",
                        text="Wait for the current turn to become steerable.",
                    )
                )
                self._render_feed()
                self._scroll_to_end()
                return
            self._render_turn_queue()

        def _handle_steer_accepted(self) -> None:
            self._sync_external_thread_state()
            self._render_turn_queue()

        async def _handle_steer_applied(self) -> None:
            await self._finalize_active_assistant()
            with self.batch_update():
                self._render_turn_queue()
                self._sync_session_messages()
                self._render_feed()
            await self._stop_active_assistant_stream()
            if self._pending:
                with self.batch_update():
                    self._begin_active_assistant()
            self._scroll_to_end()

        def _handle_steer_rejected(self, error: RoomException) -> None:
            self._entries.append(
                _AskFeedEntry(
                    role="error",
                    text=f"Unable to steer turn: {error}",
                )
            )
            self._render_turn_queue()
            self._render_feed()
            self._scroll_to_end()

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if self._input_view is None or event.text_area is not self._input_view:
                return
            self._sync_input_attachments_from_text()
            self._resize_input(event.text_area)
            self._render_command_menu()

        def on_paste(self, event: events.Paste) -> None:
            if self._input_view is None:
                return
            if not self._handle_input_paste(event.text, source="app"):
                return
            event.prevent_default()
            event.stop()

        async def _on_paste(self, event: events.Paste) -> None:
            if self._input_view is None:
                return
            if not self._handle_input_paste(event.text, source="app-private"):
                return
            event.prevent_default()
            event.stop()

        def _handle_input_paste(self, text: str, *, source: str) -> bool:
            file_paths = dropped_file_paths_from_text(
                text,
                current_working_directory=self._current_working_directory,
            )
            attachment_paths = _input_attachment_file_paths_from_text(
                text,
                current_working_directory=self._current_working_directory,
            )
            image_paths = [
                path
                for path in attachment_paths
                if _mime_type_for_path(path).startswith("image/")
            ]
            _debug_ask_paste_event(
                source=source,
                text=text,
                current_working_directory=self._current_working_directory,
                file_paths=file_paths,
                image_paths=image_paths,
                attachment_paths=attachment_paths,
            )
            if len(attachment_paths) == 0:
                return False
            if self._attaching_pasted_images:
                return True
            self._attaching_pasted_images = True
            task = asyncio.create_task(self._attach_input_files(attachment_paths))
            task.add_done_callback(_consume_task_exception)
            return True

        def _handle_clipboard_attachment_paste(self, *, source: str) -> bool:
            attachments = _macos_clipboard_attachments()
            if len(attachments.files) == 0 and len(attachments.data) == 0:
                return False
            _debug_ask_paste_event(
                source=source,
                text="<clipboard>",
                current_working_directory=self._current_working_directory,
                file_paths=attachments.files,
                image_paths=[
                    path
                    for path in attachments.files
                    if _mime_type_for_path(path).startswith("image/")
                ],
                attachment_paths=attachments.files,
            )
            if self._attaching_pasted_images:
                return True
            self._attaching_pasted_images = True
            task = asyncio.create_task(
                self._attach_input_files(
                    attachments.files,
                    data_attachments=attachments.data,
                )
            )
            task.add_done_callback(_consume_task_exception)
            return True

        def _sync_input_attachments_from_text(self) -> None:
            if self._input_view is None:
                self._input_attachments.clear()
                return
            self._input_attachments = _ask_present_input_attachments(
                self._input_view.text,
                self._input_attachments,
            )

        async def _attach_input_files(
            self,
            paths: Sequence[Path],
            *,
            data_attachments: Sequence[_ClipboardAttachmentData] = (),
        ) -> None:
            try:
                for path in paths:
                    placeholder = _input_attachment_placeholder_for_file(
                        path,
                        image_number=self._next_input_image_number,
                    )
                    if _mime_type_for_path(path).startswith("image/"):
                        self._next_input_image_number += 1
                    try:
                        attachment = await _save_ask_input_file_attachment(
                            path=path,
                            placeholder=placeholder,
                        )
                    except Exception as exc:
                        _debug_ask_attachment_event(
                            event="attach-error",
                            placeholder=placeholder,
                            image_path=path,
                            error=exc,
                        )
                        self._entries.append(
                            _AskFeedEntry(
                                role="error",
                                text=f"Unable to attach {path.name}: {exc}",
                            )
                        )
                        self._render_feed()
                        self._scroll_to_end()
                        continue
                    self._insert_input_attachment(attachment, path)

                for data_attachment in data_attachments:
                    placeholder = _input_attachment_placeholder_for_data(
                        data_attachment,
                        image_number=self._next_input_image_number,
                    )
                    if data_attachment.mime_type.startswith("image/"):
                        self._next_input_image_number += 1
                    attachment = await asyncio.to_thread(
                        _ask_input_attachment_from_bytes,
                        data=data_attachment.data,
                        mime_type=data_attachment.mime_type,
                        name=data_attachment.name,
                        placeholder=placeholder,
                    )
                    self._insert_input_attachment(
                        attachment, Path(data_attachment.name)
                    )

                if self._input_view is not None:
                    self._resize_input(self._input_view)
                    self._render_command_menu()
                    self._input_view.focus()
            finally:
                self._attaching_pasted_images = False

        def _insert_input_attachment(
            self, attachment: _AskInputAttachment, debug_path: Path
        ) -> None:
            self._input_attachments.append(attachment)
            if self._input_view is not None:
                insertion = self._attachment_placeholder_insertion(
                    attachment.placeholder
                )
                self._append_input_text(insertion)
                self._input_view._build_highlight_map()
                self._input_view.refresh()
                _debug_ask_attachment_event(
                    event="attach-inserted",
                    placeholder=attachment.placeholder,
                    image_path=debug_path,
                    input_text=self._input_view.text,
                )

        def _attachment_placeholder_insertion(self, placeholder: str) -> str:
            if self._input_view is None:
                return placeholder
            text = self._input_view.text
            if text == "":
                return placeholder
            return f" {placeholder} "

        def _append_input_text(self, text: str) -> None:
            if self._input_view is None:
                return
            next_text = f"{self._input_view.text}{text}"
            self._input_view.load_text(next_text)
            last_line = next_text.splitlines()[-1] if next_text != "" else ""
            self._input_view.move_cursor(
                (len(next_text.splitlines()) - 1, len(last_line))
            )

        def _render_command_menu(self) -> None:
            if self._command_menu_view is None:
                return
            self._active_command_option = None
            if self._command_selector is not None:
                self._render_command_selector()
                return
            if self._input_view is None or self._command_handler is None:
                self._command_menu_view.styles.display = "none"
                self._command_menu_view.update("")
                return

            prompt = self._input_view.text.strip()
            if not prompt.startswith("/"):
                self._command_menu_view.styles.display = "none"
                self._command_menu_view.update("")
                return

            command_options = self._command_options(prompt)
            if len(command_options) > 0:
                self._active_command_option = command_options[0]
                lines = Text()
                for index, option in enumerate(command_options[:8]):
                    if index > 0:
                        lines.append("\n")
                    prefix = "›" if index == 0 else " "
                    lines.append(prefix, style="bold #9aa5b8")
                    lines.append(f" {option.label}", style="bold #e5e7eb")
                    if option.active:
                        lines.append("  current", style="#7dd3fc")
                    if option.description is not None:
                        lines.append(f"  {option.description}", style="#9aa5b8")
                self._command_menu_view.styles.display = "block"
                self._command_menu_view.update(lines)
                return

            if " " in prompt:
                self._command_menu_view.styles.display = "none"
                self._command_menu_view.update("")
                return

            matches = [
                (command, description)
                for command, description in _ASK_SLASH_COMMANDS
                if command.startswith(prompt) or command.split(" ", 1)[0] == prompt
            ]
            if len(matches) == 0:
                self._command_menu_view.styles.display = "none"
                self._command_menu_view.update("")
                return

            lines = Text()
            for index, (command, description) in enumerate(matches):
                if index > 0:
                    lines.append("\n")
                prefix = "›" if index == 0 else " "
                lines.append(prefix, style="bold #9aa5b8")
                lines.append(f" {command}", style="bold #e5e7eb")
                lines.append(f"  {description}", style="#9aa5b8")
            self._command_menu_view.styles.display = "block"
            self._command_menu_view.update(lines)

        def _render_command_selector(self) -> None:
            if self._command_menu_view is None:
                return
            selector = self._command_selector
            if selector is None:
                return
            lines = Text()
            title = "Select output" if selector.prompt == "/output" else "Select model"
            lines.append(title, style="bold #e5e7eb")
            lines.append("  Enter applies  Esc cancels", style="#9aa5b8")
            for index, option in enumerate(selector.options):
                lines.append("\n")
                selected = index == selector.selected_index
                prefix = "›" if selected else " "
                style = "bold #e5e7eb" if selected else "#cfd3dc"
                lines.append(prefix, style="bold #7dd3fc" if selected else "#9aa5b8")
                lines.append(f" {option.label}", style=style)
                if option.active:
                    lines.append("  current", style="#7dd3fc")
                if option.description is not None:
                    lines.append(f"  {option.description}", style="#9aa5b8")
            self._command_menu_view.styles.display = "block"
            self._command_menu_view.update(lines)

        def _command_options(self, prompt: str) -> list[AskCommandOption]:
            provider = self._command_options_provider
            if provider is None:
                return []
            try:
                return list(provider(prompt))
            except Exception:
                return []

        async def _append_delta(self, text: str) -> None:
            self._active_assistant_text += text
            if self._active_assistant_stream is not None:
                await self._active_assistant_stream.write(text)
            self._scroll_to_end()

        async def _handle_agent_message(self, message: AgentMessage) -> None:
            if isinstance(message, (StartThread, TurnStart, TurnSteer)):
                self._sync_session_messages()
                return
            if isinstance(message, AgentThreadStatus):
                self._sync_session_messages()
                self._set_thread_status_text(message)
                return
            if isinstance(message, AgentTextContentStarted):
                await self._prepare_active_assistant_text_item(item_id=message.item_id)
                return
            if isinstance(message, AgentTextContentDelta):
                await self._prepare_active_assistant_text_item(
                    item_id=message.item_id,
                    sender_name=message.sender_name,
                )
                await self._append_delta(message.text)
                return
            if isinstance(message, AgentAudioTranscriptionDelta):
                if message.role in {None, "assistant"}:
                    await self._prepare_active_assistant_text_item(
                        item_id=message.item_id,
                        sender_name=message.sender_name,
                    )
                    await self._append_delta(message.text)
                return
            if isinstance(message, AgentTextContentEnded):
                if (
                    self._active_assistant_item_id is None
                    or self._active_assistant_item_id == message.item_id
                ):
                    await self._finalize_active_assistant()
                return
            if isinstance(message, AgentAudioTranscriptionCompleted):
                if message.role in {None, "assistant"} and (
                    self._active_assistant_item_id is None
                    or self._active_assistant_item_id == message.item_id
                ):
                    await self._finalize_active_assistant()
                return
            if isinstance(message, AgentAudioTranscriptionFailed):
                if message.error is not None:
                    self._append_event_entry(
                        f"Voice transcript failed: {message.error.message}"
                    )
                return
            if isinstance(message, AgentAudioGenerationDelta):
                error = await self._audio_player.play_delta(message.data)
                if error is not None and not self._audio_error_reported:
                    self._audio_error_reported = True
                    self._append_event_entry(error)
                return
            if isinstance(message, AgentAudioGenerationCompleted):
                await self._audio_player.close()
                return
            if isinstance(message, AgentAudioGenerationFailed):
                await self._audio_player.close()
                if message.error is not None:
                    self._append_event_entry(
                        f"Voice response failed: {message.error.message}"
                    )
                return
            if isinstance(message, AgentAudioGenerationStarted):
                self._audio_error_reported = False
                return
            if isinstance(message, AgentFileContentDelta):
                await self._finalize_active_assistant()
                await self._append_image_or_attachment_entry(
                    role=_role_for_sender(
                        message.sender_name,
                        local_participant_name=None,
                        default="assistant",
                    ),
                    uri=message.url,
                    message_id=message.item_id,
                )
                return
            if isinstance(message, AgentImageGenerationPartial):
                await self._finalize_active_assistant()
                if message.image is not None:
                    await self._append_image_or_attachment_entry(
                        role="assistant",
                        uri=message.image.uri,
                        fallback_mime_type=message.image.mime_type,
                        message_id=message.item_id,
                    )
                return
            if isinstance(message, AgentImageGenerationCompleted):
                await self._finalize_active_assistant()
                await self._append_image_or_attachment_entry(
                    role="assistant",
                    uri=[image.uri for image in message.images],
                    message_id=message.item_id,
                )
                return
            if isinstance(message, AgentImageGenerationFailed):
                await self._finalize_active_assistant()
                if message.error is not None:
                    self._append_event_entry(
                        f"Image generation failed: {message.error.message}"
                    )
                return
            if isinstance(message, AgentImageGenerationStarted):
                await self._finalize_active_assistant()
                return
            if isinstance(message, AgentReasoningContentStarted):
                await self._finalize_active_assistant()
                self._reasoning_parts[message.item_id] = []
                return
            if isinstance(message, AgentReasoningContentDelta):
                self._reasoning_parts.setdefault(message.item_id, []).append(
                    message.text
                )
                return
            if isinstance(message, AgentReasoningContentEnded):
                parts = self._reasoning_parts.pop(message.item_id, [])
                summary = "".join(parts).strip()
                if summary != "":
                    self._append_event_entry(f"Reasoning\n{summary}")
                return
            if isinstance(
                message,
                (
                    AgentToolCallPending,
                    AgentToolCallInProgress,
                    AgentToolCallStarted,
                    AgentToolCallApprovalRequested,
                ),
            ):
                await self._finalize_active_assistant()
                self._tool_calls[message.item_id] = _AskToolCallState(
                    toolkit=message.toolkit,
                    tool=message.tool,
                    arguments=message.arguments,
                    logs=[],
                )
                return
            if isinstance(message, AgentToolCallArgumentsDelta):
                await self._finalize_active_assistant()
                state = self._tool_calls.get(message.item_id)
                if state is None:
                    return
                state.argument_delta_text += message.delta
                arguments = _merge_ask_tool_call_arguments_delta(
                    tool=state.tool,
                    arguments=state.arguments,
                    delta_text=state.argument_delta_text,
                )
                if arguments is not None:
                    state.arguments = arguments
                return
            if isinstance(message, AgentToolCallLogDelta):
                await self._finalize_active_assistant()
                state = self._tool_calls.get(message.item_id)
                if state is None:
                    return
                if state.logs is None:
                    state.logs = []
                for line in message.lines:
                    state.logs.append(line.text)
                return
            if isinstance(message, AgentToolCallEnded):
                await self._finalize_active_assistant()
                state = self._tool_calls.pop(message.item_id, None)
                entry = self._tool_call_entry(message, state)
                if self._append_or_replace_feed_entry(entry):
                    self._render_feed()
                    self._scroll_to_end()
                return
            if isinstance(message, AgentThreadEvent):
                await self._finalize_active_assistant()
                entry = self._thread_event_entry(message.event)
                if entry is not None and self._append_or_replace_feed_entry(entry):
                    self._render_feed()
                    self._scroll_to_end()
                return
            if isinstance(message, AgentContextCompacted):
                await self._finalize_active_assistant()
                self._append_event_entry("Compacted context")
                return
            if isinstance(message, AgentUsageUpdated):
                self._set_usage(message)

        def _append_event_entry(
            self,
            text: str,
            *,
            kind: Literal["text", "image", "diff"] = "text",
            message_id: str | None = None,
        ) -> None:
            normalized = text.strip()
            if normalized == "":
                return
            prefix = "" if kind == "diff" else "• "
            self._entries.append(
                _AskFeedEntry(
                    role="event",
                    text=f"{prefix}{normalized}",
                    kind=kind,
                    message_id=message_id,
                )
            )
            self._render_feed()
            self._scroll_to_end()

        async def _append_image_or_attachment_entry(
            self,
            *,
            role: str,
            uri: str
            | _AskAttachmentReference
            | Sequence[str | _AskAttachmentReference]
            | None,
            text: str = "",
            fallback_mime_type: str | None = None,
            message_id: str | None = None,
        ) -> None:
            attachment_references = (
                [uri]
                if isinstance(uri, (str, _AskAttachmentReference)) or uri is None
                else list(uri)
            )
            inline_renderables: list[Any] = []
            attachment_parts: list[str] = []
            image_dataset_client = (
                self._image_dataset_client
                if self._image_dataset_client is not None
                else (
                    self._session.image_dataset_client
                    if isinstance(self._session, _AskImageDatasetProvider)
                    else None
                )
            )
            for attachment_reference in attachment_references:
                if isinstance(attachment_reference, _AskAttachmentReference):
                    item_uri = attachment_reference.uri
                    item_name = attachment_reference.name
                else:
                    item_uri = attachment_reference
                    item_name = None
                attachment_record = await _attachment_record_from_uri(
                    item_uri,
                    image_dataset_client=image_dataset_client,
                    fallback_mime_type=fallback_mime_type,
                )
                if (
                    attachment_record is not None
                    and attachment_record.mime_type.startswith("image/")
                ):
                    image_preview = await asyncio.to_thread(
                        _image_preview_from_record,
                        attachment_record,
                        max_rows=max(1, self.size.height // 2),
                    )
                    if image_preview is not None:
                        inline_renderables.append(image_preview)
                        continue
                if (
                    attachment_record is not None
                    and attachment_record.mime_type == "application/pdf"
                ):
                    pdf_preview = await asyncio.to_thread(
                        _pdf_preview_from_record,
                        attachment_record,
                        name=_attachment_display_name(item_uri, name=item_name),
                        max_rows=max(1, self.size.height // 2),
                    )
                    if pdf_preview is not None:
                        inline_renderables.append(pdf_preview)
                        continue
                if isinstance(item_uri, str) and item_uri.strip() != "":
                    attachment_parts.append(
                        _attachment_display_text(item_uri, name=item_name)
                    )
            if len(inline_renderables) > 0:
                inline_renderables.extend(attachment_parts)
                entry = _AskFeedEntry(
                    role=role,
                    text=text.strip(),
                    kind="image",
                    message_id=message_id,
                    image_renderable=inline_renderables,
                )
            elif len(attachment_parts) > 0:
                entry = _AskFeedEntry(
                    role=role,
                    text="\n\n".join(
                        part
                        for part in (text.strip(), "\n\n".join(attachment_parts))
                        if part != ""
                    ),
                    message_id=message_id,
                )
            else:
                return
            self._append_or_replace_feed_entry(entry)
            self._render_feed()
            self._scroll_to_end()

        def _append_or_replace_feed_entry(self, entry: _AskFeedEntry) -> bool:
            if entry.message_id is not None:
                for index, existing in enumerate(self._entries):
                    if existing.message_id != entry.message_id:
                        continue
                    if existing == entry:
                        return False
                    self._entries[index] = entry
                    if index < self._rendered_entry_count:
                        if not self._update_rendered_feed_entry(entry):
                            self._reset_rendered_feed()
                    return True
            self._entries.append(entry)
            return True

        async def _hydrate_session_image_entry(
            self,
            *,
            message: _AskConversationMessage,
            uri: str
            | _AskAttachmentReference
            | Sequence[str | _AskAttachmentReference],
        ) -> None:
            try:
                await self._append_image_or_attachment_entry(
                    role=message.role,
                    uri=uri,
                    text=message.text,
                    message_id=message.message_id,
                )
                self._rendered_session_message_ids.add(message.message_id)
            finally:
                self._pending_session_image_message_ids.discard(message.message_id)

        def _reset_rendered_feed(self) -> None:
            if self._feed_view is not None:
                self._feed_view.remove_children()
            self._rendered_entry_count = 0
            self._rendered_feed_entries_by_message_id.clear()

        def _update_rendered_feed_entry(self, entry: _AskFeedEntry) -> bool:
            if entry.message_id is None:
                return False
            rendered = self._rendered_feed_entries_by_message_id.get(entry.message_id)
            if rendered is None:
                return False
            if rendered.entry.role != entry.role or rendered.entry.kind != entry.kind:
                return False
            if rendered.entry.pending != entry.pending:
                return False
            body_text = entry.text if entry.text.strip() != "" else " "
            body_widget = rendered.body_widget
            if isinstance(body_widget, TextualMarkdown):
                body_widget.update(body_text)
            elif isinstance(body_widget, Static):
                body_widget.update(self._feed_entry_body_renderable(entry))
            else:
                return False
            rendered.entry = entry
            return True

        def _tool_call_entry_text(
            self,
            message: AgentToolCallEnded,
            state: _AskToolCallState | None,
        ) -> str:
            tool = message.tool.strip() if isinstance(message.tool, str) else "tool"
            if tool == "":
                tool = "tool"
            toolkit = (
                message.toolkit.strip() if isinstance(message.toolkit, str) else ""
            )
            arguments: dict[str, Any] | None = None
            logs: list[str] = []
            if state is not None:
                state_tool = state.tool.strip()
                if state_tool != "":
                    tool = state_tool
                state_toolkit = state.toolkit.strip()
                if state_toolkit != "":
                    toolkit = state_toolkit
                arguments = state.arguments
                if state.logs is not None:
                    logs = state.logs

            error_message = None
            if message.error is not None:
                error_message = message.error.message
            return _format_ask_tool_call_entry_text(
                toolkit=toolkit,
                tool=tool,
                arguments=arguments,
                logs=logs,
                error_message=error_message,
            )

        def _tool_call_entry(
            self,
            message: AgentToolCallEnded,
            state: _AskToolCallState | None,
        ) -> _AskFeedEntry:
            text = self._tool_call_entry_text(message, state)
            if self._is_codex_diff_tool_call(message=message, state=state):
                diff_text = self._tool_call_diff_text(state=state)
                if diff_text != "":
                    return _AskFeedEntry(
                        role="event",
                        text=f"{text}\n{diff_text}",
                        kind="diff",
                        message_id=f"tool:{message.item_id}",
                    )
            return _AskFeedEntry(role="event", text=f"• {text}")

        @staticmethod
        def _is_codex_diff_tool_call(
            *, message: AgentToolCallEnded, state: _AskToolCallState | None
        ) -> bool:
            toolkit = ""
            tool = ""
            if state is not None:
                toolkit = state.toolkit.strip().lower()
                tool = state.tool.strip().lower()
            if toolkit == "":
                toolkit = message.toolkit.strip().lower() if message.toolkit else ""
            if tool == "":
                tool = message.tool.strip().lower() if message.tool else ""
            return toolkit == "codex" and tool.startswith("diff")

        @staticmethod
        def _tool_call_diff_text(*, state: _AskToolCallState | None) -> str:
            if state is None or state.arguments is None:
                return ""
            diff = state.arguments.get("diff")
            if not isinstance(diff, str):
                return ""
            return diff.strip()

        def _thread_event_entry_text(self, event: dict[str, Any]) -> str:
            for key in (
                "headline",
                "summary",
                "status_detail",
                "message",
                "type",
                "kind",
            ):
                value = event.get(key)
                if isinstance(value, str) and value.strip() != "":
                    return _friendly_ask_thread_event_text(value)
            return ""

        def _thread_event_entry(self, event: dict[str, Any]) -> _AskFeedEntry | None:
            event_text = self._thread_event_entry_text(event)
            if event_text == "":
                return None

            message_id = None
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id.strip() != "":
                message_id = f"event:{item_id.strip()}"

            kind = event.get("kind")
            if isinstance(kind, str) and kind.strip().lower() == "diff":
                diff_text = self._thread_event_diff_text(event)
                if diff_text != "":
                    return _AskFeedEntry(
                        role="event",
                        text=f"{event_text}\n{diff_text}",
                        kind="diff",
                        message_id=message_id,
                    )

            return _AskFeedEntry(
                role="event",
                text=f"• {event_text}",
                message_id=message_id,
            )

        @staticmethod
        def _thread_event_diff_text(event: dict[str, Any]) -> str:
            for key in ("preview", "data"):
                value = event.get(key)
                if isinstance(value, str) and value.strip() != "":
                    return value.strip()
            details = event.get("details")
            if isinstance(details, str) and details.strip() != "":
                return details.strip()
            if isinstance(details, list):
                lines = [item for item in details if isinstance(item, str)]
                return "\n".join(lines).strip()
            return ""

        def _resize_input(self, input_view: TextArea) -> None:
            if input_view.text == "":
                target_height = 1
            else:
                target_height = max(1, min(6, input_view.virtual_size.height))
            if target_height == self._input_height:
                self._sync_input_row_padding()
                return
            self._input_height = target_height
            input_view.styles.height = target_height
            self._sync_input_row_padding()

        def _sync_input_row_padding(self) -> None:
            if self._input_row is None:
                return
            if self._input_height <= 1:
                self._input_row.styles.padding = (1, 0, 1, 0)
            else:
                self._input_row.styles.padding = (0, 0, 0, 0)

        def _on_spinner_tick(self) -> None:
            self._sync_external_thread_state()
            self._render_side_panel()
            if not (self._pending or self._external_thread_active):
                return
            self._status_gradient_offset = self._status_gradient_offset + 1
            self._render_status_line()

        def _scroll_to_end(self) -> None:
            if self._feed_scroll is not None:
                self._feed_scroll.anchor()
                self._feed_scroll.scroll_end(animate=False)

        def _set_status_text(self, status: str | None) -> None:
            self._status_text = status
            self._render_status_line()
            self._render_turn_queue()

        def _set_thread_status_text(self, status: object) -> None:
            if isinstance(status, AgentThreadStatus):
                self._status_text = _format_agent_thread_status_text(status)
            else:
                self._status_text = _thread_status_text(status)
            self._render_status_line()
            self._render_turn_queue()

        def _set_thread_status_feed_entry(self, status: object) -> bool:
            text = _ask_thread_status_feed_text(status)
            if text is None:
                for index, entry in enumerate(self._entries):
                    if entry.message_id != self._thread_status_entry_id:
                        continue
                    del self._entries[index]
                    if index < self._rendered_entry_count:
                        self._reset_rendered_feed()
                    return True
                return False
            return self._append_or_replace_feed_entry(
                _AskFeedEntry(
                    role="event",
                    text=text,
                    pending=True,
                    message_id=self._thread_status_entry_id,
                )
            )

        def _sync_external_thread_state(self) -> None:
            session = self._session
            if not isinstance(session, _AskExternalThreadState):
                return
            generation = (
                self._thread_generation_provider()
                if self._thread_generation_provider is not None
                else (
                    session.thread_generation
                    if isinstance(session, _AskThreadGenerationState)
                    else None
                )
            )
            if generation is not None:
                if self._thread_generation is None:
                    self._thread_generation = generation
                elif self._thread_generation != generation:
                    self._thread_generation = generation
                    self._reset_current_thread_feed()

            thread_status = session.thread_status
            status = (
                _format_agent_thread_status_text(thread_status)
                if thread_status is not None
                else session.thread_status_text
            )
            active = status is not None and status.strip() != ""
            labels = _ask_queued_message_labels(
                session,
                external_thread_active=active,
            )
            status_changed = active != self._external_thread_active
            queue_changed = labels != self._external_queued_messages
            self._external_thread_active = active
            self._external_queued_messages = labels
            next_status_started_at = _sync_status_timer_started_at(
                started_at=self._status_started_at,
                active=active,
                pending=self._pending,
                now=time.monotonic(),
            )
            status_changed = (
                status_changed or next_status_started_at != self._status_started_at
            )
            self._status_started_at = next_status_started_at

            if self._pending:
                if status is not None and status.strip() != "":
                    status_changed = status_changed or status != self._status_text
                    self._status_text = status
            else:
                next_status = status if active else None
                status_changed = status_changed or next_status != self._status_text
                self._status_text = next_status

            if status_changed:
                self._render_status_line()
            if status_changed or queue_changed:
                self._render_turn_queue()
            self._sync_session_messages()

        def _reset_current_thread_feed(self) -> None:
            self._entries.clear()
            self._rendered_session_message_ids.clear()
            self._pending_session_image_message_ids.clear()
            self._active_assistant_entry = None
            self._active_assistant_text = ""
            self._active_assistant_name = None
            self._active_assistant_item_id = None
            self._reset_rendered_feed()
            self._render_feed()
            self._scroll_to_end()

        def _sync_session_messages(self) -> None:
            session = self._session
            if not isinstance(session, _AskExternalThreadState):
                return
            session_messages = session.messages
            hidden_queued_message_ids: set[str] = set()
            if isinstance(session, ChatThreadSession):
                inline_pending_message_ids = _ask_inline_pending_message_ids(
                    session,
                    external_thread_active=self._external_thread_active,
                )
                hidden_queued_message_ids = {
                    pending.message_id
                    for pending in session.pending_inputs
                    if pending.message_id not in inline_pending_message_ids
                }
            if any(isinstance(message, AgentMessage) for message in session_messages):
                local_participant_name = (
                    session.local_participant_name
                    if isinstance(session, ChatThreadSession)
                    else None
                )
                session_messages = _ask_conversation_messages_from_agent_messages(
                    session_messages,
                    local_participant_name=local_participant_name,
                )
            visible_count = _ask_conversation_message_render_window(self.size.height)
            start_index = max(0, len(session_messages) - visible_count)
            if len(self._rendered_session_message_ids) == 0:
                for message in session_messages[:start_index]:
                    self._rendered_session_message_ids.add(message.message_id)

            changed = False
            for message in session_messages[start_index:]:
                if message.message_id in hidden_queued_message_ids:
                    continue
                if message.message_id == self._active_assistant_item_id:
                    continue
                entry = _AskFeedEntry(
                    role=message.role,
                    text=message.text,
                    kind=message.kind,
                    message_id=message.message_id,
                )
                renderable_attachment_references = tuple(
                    attachment_reference
                    for attachment_reference in message.attachment_references
                    if _attachment_uri_may_render_inline(attachment_reference.uri)
                )
                dataset_image_uri = _dataset_image_attachment_uri(message.text)
                if (
                    len(renderable_attachment_references) == 0
                    and dataset_image_uri is not None
                ):
                    renderable_attachment_references = (
                        _AskAttachmentReference(uri=dataset_image_uri),
                    )
                existing_entry = next(
                    (
                        existing
                        for existing in self._entries
                        if existing.message_id == message.message_id
                    ),
                    None,
                )
                already_rendered_attachment = (
                    existing_entry is not None
                    and existing_entry.kind == "image"
                    and len(renderable_attachment_references) > 0
                )
                if already_rendered_attachment:
                    self._rendered_session_message_ids.add(message.message_id)
                    continue
                if (
                    len(renderable_attachment_references) > 0
                    and message.message_id
                    not in self._pending_session_image_message_ids
                ):
                    self._pending_session_image_message_ids.add(message.message_id)
                    task = asyncio.create_task(
                        self._hydrate_session_image_entry(
                            message=message,
                            uri=renderable_attachment_references
                            if len(renderable_attachment_references) > 1
                            else renderable_attachment_references[0],
                        )
                    )
                    task.add_done_callback(_consume_task_exception)
                    continue
                if message.message_id in self._rendered_session_message_ids:
                    if any(
                        existing.message_id == message.message_id
                        for existing in self._entries
                    ) and self._append_or_replace_feed_entry(entry):
                        changed = True
                    continue
                if self._append_or_replace_feed_entry(entry):
                    changed = True
                self._rendered_session_message_ids.add(message.message_id)
            if changed:
                self._render_feed()
                self._scroll_to_end()

        def _set_usage(self, usage_update: AgentUsageUpdated) -> None:
            total_tokens = usage_update.usage.get("total_tokens")
            if total_tokens is None:
                total_tokens = sum(usage_update.usage.values())
            self._usage_state = _AskUsageState(
                context_used_tokens=usage_update.context_window.used_tokens,
                context_total_tokens=usage_update.context_window.total_tokens,
                compaction_mode=usage_update.context_window.compaction_mode,
                compaction_threshold=usage_update.context_window.compaction_threshold,
                total_tokens=total_tokens,
            )
            self._render_session_meta()

        def _render_status_line(self) -> None:
            if self._status_view is None:
                return
            if (
                self._pending or self._external_thread_active
            ) and self._status_text is not None:
                self._status_view.update(self._status_block(self._status_renderable()))
                return

            if len(self._entries) == 0:
                self._status_view.update(
                    self._status_block(
                        Text(
                            "Ask a question to start a conversation.",
                            style="dim",
                        )
                    )
                )
                return

            self._status_view.update(
                self._status_block(Text("Waiting for user input", style="dim"))
            )

        def _status_block(self, status: Text) -> Text:
            block = Text(" \n")
            block.append_text(status)
            block.append("\n ")
            return block

        def _render_turn_queue(self) -> None:
            if self._queue_view is None:
                return

            queued_labels = self._external_queued_messages

            if len(queued_labels) == 0:
                self._queue_view.styles.display = "none"
                self._queue_view.update("")
                return

            queue_lines = Text()
            queue_lines.append("Queued\n", style="bold #8aa6cf")
            for index, label in enumerate(queued_labels, start=1):
                normalized_prompt = " ".join(label.split())
                if len(normalized_prompt) > 72:
                    normalized_prompt = normalized_prompt[:69].rstrip() + "..."
                queue_lines.append(f"{index}. ", style="dim")
                queue_lines.append(normalized_prompt)
                if index < len(queued_labels):
                    queue_lines.append("\n")

            self._queue_view.styles.display = "block"
            self._queue_view.update(queue_lines)

        def _render_session_meta(self) -> None:
            if self._session_meta_view is None:
                return
            model_label = model
            if self._model_label_provider is not None:
                provided_model_label = self._model_label_provider()
                if (
                    provided_model_label is not None
                    and provided_model_label.strip() != ""
                ):
                    model_label = provided_model_label.strip()
            modality_label = None
            if self._output_label_provider is not None:
                provided_output_label = self._output_label_provider()
                if (
                    provided_output_label is not None
                    and provided_output_label.strip() != ""
                ):
                    modality_label = provided_output_label.strip()
            meta_text = Text.assemble(
                ("model ", "bold #9aa5b8"), (model_label, "#cfd3dc")
            )
            if modality_label is not None:
                meta_text.append("  •  ", style="#5f6778")
                meta_text.append("output ", style="bold #9aa5b8")
                meta_text.append(modality_label, style="#cfd3dc")
            meta_text.append("  •  ", style="#5f6778")
            meta_text.append("cwd ", style="bold #9aa5b8")
            meta_text.append(self._current_working_directory, style="#cfd3dc")
            table = Table.grid(expand=True)
            table.add_column(ratio=1)
            table.add_column(justify="right", no_wrap=True)
            table.add_row(
                meta_text,
                self._usage_footer_text(),
            )
            self._session_meta_view.update(table)

        def _render_side_panel(self) -> None:
            if self._side_panel_view is None:
                return
            renderer = self._side_panel_renderer
            if renderer is None or not self._side_panel_enabled:
                self._side_panel_view.styles.display = "none"
                self._side_panel_view.remove_class("side-panel--visible")
                self._side_panel_view.remove_class("side-panel--focused")
                self._side_panel_view.update("")
                return
            self._side_panel_view.styles.display = "block"
            self._side_panel_view.add_class("side-panel--visible")
            if self._side_panel_focused:
                self._side_panel_view.add_class("side-panel--focused")
            else:
                self._side_panel_view.remove_class("side-panel--focused")
            self._side_panel_view.update(
                renderer(
                    self._side_panel_focused,
                    width=self._side_panel_view.size.width,
                    height=self._side_panel_view.size.height,
                )
            )

        def _usage_footer_text(self) -> Text:
            usage_state = self._usage_state
            if usage_state is None:
                return Text("")

            context_label = _format_token_count(usage_state.context_used_tokens)
            context_limit_tokens = usage_state.compaction_threshold
            if context_limit_tokens is None:
                context_limit_tokens = usage_state.context_total_tokens
            if context_limit_tokens is not None:
                context_label = (
                    f"{context_label}/{_format_token_count(context_limit_tokens)}"
                )

            compaction_label = ""
            if usage_state.compaction_mode is not None:
                compaction_label = usage_state.compaction_mode

            if compaction_label != "":
                return Text.assemble(
                    ("compaction ", "bold #9aa5b8"),
                    (compaction_label, "#cfd3dc"),
                    ("  •  ", "#5f6778"),
                    ("context ", "bold #9aa5b8"),
                    (context_label, "#cfd3dc"),
                    ("  •  ", "#5f6778"),
                    ("tokens ", "bold #9aa5b8"),
                    (_format_token_count(usage_state.total_tokens), "#cfd3dc"),
                )

            return Text.assemble(
                ("context ", "bold #9aa5b8"),
                (context_label, "#cfd3dc"),
                ("  •  ", "#5f6778"),
                ("tokens ", "bold #9aa5b8"),
                (_format_token_count(usage_state.total_tokens), "#cfd3dc"),
            )

        def _render_active_assistant_header(self) -> None:
            if self._active_assistant_header is None:
                return
            if not self._pending:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
                return
            if self._active_assistant_name is None:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
                return
            previous_participant_role = _ask_feed_previous_participant_role(
                [entry.role for entry in self._entries],
                before_index=len(self._entries),
            )
            if previous_participant_role == self._active_assistant_name:
                self._active_assistant_header.styles.display = "none"
                self._active_assistant_header.update("")
                return
            self._active_assistant_header.styles.display = "block"
            self._active_assistant_header.update(
                Text(
                    self._active_assistant_name,
                    style="bold cyan",
                )
            )

        def _agent_message_sender_name(self, sender_name: object) -> str | None:
            if not isinstance(sender_name, str):
                return None
            normalized_sender_name = sender_name.strip()
            if normalized_sender_name == "":
                return None
            return normalized_sender_name

        def _status_renderable(self) -> Text:
            elapsed_label = "0s"
            if self._status_started_at is not None:
                elapsed_seconds = max(
                    0, int(time.monotonic() - self._status_started_at)
                )
                minutes, seconds = divmod(elapsed_seconds, 60)
                hours, minutes = divmod(minutes, 60)
                if hours > 0:
                    elapsed_label = f"{hours}h {minutes}m"
                elif minutes > 0:
                    elapsed_label = f"{minutes}m {seconds}s"
                else:
                    elapsed_label = f"{seconds}s"

            label = f"• {self._status_text} ({elapsed_label} • esc to interrupt)"
            if label == "":
                return Text("")

            palette = [
                "#6f7b90",
                "#8aa6cf",
                "#c4dbff",
                "#f4f8ff",
                "#c4dbff",
                "#8aa6cf",
                "#6f7b90",
            ]
            overlay_width = max(6, min(14, len(label)))
            text = Text()
            for index, character in enumerate(label):
                style = "#8892a3"
                offset = (index - self._status_gradient_offset) % len(label)
                if offset < overlay_width:
                    palette_index = int(
                        (offset / max(overlay_width - 1, 1)) * (len(palette) - 1)
                    )
                    color = palette[palette_index]
                    style = f"bold {color}"
                text.append(character, style=style)
            return text

        def _render_feed(self) -> None:
            if self._feed_view is None:
                return

            if self._rendered_entry_count >= len(self._entries):
                return

            entry_roles = [entry.role for entry in self._entries]
            for entry in self._entries[self._rendered_entry_count :]:
                if (
                    self._rendered_entry_count > 0
                    and entry.role != "event"
                    and self._entries[self._rendered_entry_count - 1].role == "event"
                ):
                    self._feed_view.mount(Static("", classes="feed-event-break"))
                previous_participant_role = _ask_feed_previous_participant_role(
                    entry_roles,
                    before_index=self._rendered_entry_count,
                )
                rendered = self._render_entry(
                    entry,
                    show_header=self._should_show_entry_header(
                        entry=entry,
                        previous_participant_role=previous_participant_role,
                    ),
                )
                self._feed_view.mount(rendered.widget)
                if entry.message_id is not None:
                    self._rendered_feed_entries_by_message_id[entry.message_id] = (
                        rendered
                    )
                self._rendered_entry_count += 1

        def _should_show_entry_header(
            self,
            *,
            entry: _AskFeedEntry,
            previous_participant_role: str | None,
        ) -> bool:
            if entry.role in {"event", "error"}:
                return True
            if previous_participant_role is None:
                return True
            if previous_participant_role == "error":
                return True
            return previous_participant_role != entry.role

        def _feed_entry_body_renderable(self, entry: _AskFeedEntry) -> Any:
            body_text = entry.text if entry.text.strip() != "" else " "
            body_style = "white" if entry.role == "you" else ""
            if entry.role == "error":
                body_style = "red"
            if entry.kind == "image" and body_text.strip() != "":
                image_text = Text.from_ansi(body_text)
                image_text.no_wrap = False
                image_text.overflow = "fold"
                return image_text
            if entry.kind == "diff" and body_text.strip() != "":
                return self._diff_feed_entry_renderable(body_text)
            return Text(
                body_text,
                style=body_style,
                no_wrap=False,
                overflow="fold",
            )

        def _diff_feed_entry_renderable(self, body_text: str) -> Text:
            output = Text(no_wrap=False, overflow="fold")
            for index, line in enumerate(body_text.splitlines()):
                if index > 0:
                    output.append("\n")
                output.append_text(self._render_diff_feed_line(line))
            return output

        @staticmethod
        def _render_diff_feed_line(line: str) -> Text:
            text = Text(line)
            if line.startswith("@@"):
                text.stylize("bold cyan")
            elif (
                line.startswith("diff ")
                or line.startswith("index ")
                or line.startswith("*** ")
            ):
                text.stylize("dim")
            elif line.startswith("+++ ") or line.startswith("--- "):
                text.stylize("bold yellow")
            elif line.startswith("+"):
                text.stylize("green")
            elif line.startswith("-"):
                text.stylize("red")
            elif line.startswith(" "):
                text.stylize("dim")
            return text

        def _feed_entry_image_widget(self, entry: _AskFeedEntry) -> Any:
            renderables = entry.image_renderable
            body_text = entry.text.strip()
            if isinstance(renderables, list):
                image_widgets: list[Any] = []
                if body_text != "":
                    image_widgets.append(
                        Static(
                            self._feed_entry_body_renderable(
                                _AskFeedEntry(
                                    role=entry.role,
                                    text=body_text,
                                    kind="text",
                                )
                            ),
                            classes="feed-entry-body",
                        )
                    )
                for renderable in renderables:
                    if isinstance(renderable, _AskImagePreview):
                        image_widgets.append(_AskImagePreviewWidget(renderable))
                    elif isinstance(renderable, _AskPdfPreview):
                        image_widgets.append(_AskPdfPreviewWidget(renderable))
                    else:
                        image_widgets.append(
                            Static(
                                renderable,
                                classes="feed-entry-body",
                            )
                        )
                if len(image_widgets) == 1:
                    return image_widgets[0]
                return Vertical(*image_widgets, classes="feed-entry-images")
            return Static(
                self._feed_entry_body_renderable(entry),
                classes="feed-entry-body",
            )

        def _render_entry(
            self, entry: _AskFeedEntry, *, show_header: bool
        ) -> _RenderedAskFeedEntry:
            header_style = "bold cyan"
            entry_classes = "feed-entry"
            if entry.role == "you":
                header_style = "bold white"
                entry_classes += " feed-entry--you"
            elif entry.role == "error":
                header_style = "bold red"
                entry_classes += " feed-entry--error"
            elif entry.role == "event":
                event_text = Text("", no_wrap=False, overflow="fold")
                lines = entry.text.splitlines()
                if len(lines) == 0:
                    event_text.append(" ")
                else:
                    event_text.append(lines[0], style="white")
                    for line in lines[1:]:
                        event_text.append("\n")
                        event_text.append(line, style="dim")
                body_widget = Static(
                    event_text,
                    classes="feed-entry-body",
                )
                widget = Vertical(
                    body_widget,
                    classes="feed-entry feed-entry--event",
                )
                return _RenderedAskFeedEntry(
                    entry=entry,
                    widget=widget,
                    body_widget=body_widget,
                )
            elif not show_header:
                entry_classes += " feed-entry--continued"

            body_text = entry.text if entry.text.strip() != "" else " "
            if entry.role == "error":
                body_widget = Static(
                    self._feed_entry_body_renderable(entry),
                    classes="feed-entry-body",
                )
            elif entry.kind == "image":
                body_widget = self._feed_entry_image_widget(entry)
            elif entry.role != "you" and body_text.strip() != "":
                body_widget = TextualMarkdown(
                    body_text,
                    classes="feed-entry-body feed-entry-markdown",
                )
            elif body_text.strip() != "" and _ask_text_needs_markdown(body_text):
                body_widget = TextualMarkdown(
                    body_text,
                    classes="feed-entry-body feed-entry-markdown",
                )
            elif body_text.strip() != "":
                body_widget = Static(
                    self._feed_entry_body_renderable(entry),
                    classes="feed-entry-body",
                )
            else:
                body_widget = Static(
                    self._feed_entry_body_renderable(entry),
                    classes="feed-entry-body",
                )

            widgets = [body_widget]
            if show_header:
                widgets.insert(
                    0,
                    Static(
                        Text(entry.role, style=header_style),
                        classes="feed-entry-header",
                    ),
                )

            widget = Vertical(*widgets, classes=entry_classes)
            return _RenderedAskFeedEntry(
                entry=entry,
                widget=widget,
                body_widget=body_widget,
            )

    async def _run_app(session_arg: Any) -> None:
        resolved_current_working_directory = os.path.abspath(
            current_working_directory or os.getcwd()
        )
        resolved_session_provider = session_provider or (lambda: session_arg)
        app = _AskTextualApp(
            session=session_arg,
            session_provider=resolved_session_provider,
            thread_generation_provider=thread_generation_provider,
            title=title,
            assistant_name=assistant_name,
            current_working_directory=resolved_current_working_directory,
            image_dataset_client=image_dataset_client,
            command_handler=command_handler,
            model_label_provider=model_label_provider,
            output_label_provider=output_label_provider,
            command_options_provider=command_options_provider,
            command_options_loader=command_options_loader,
            side_panel_renderer=side_panel_renderer,
            side_panel_key_handler=side_panel_key_handler,
            side_panel_mouse_handler=side_panel_mouse_handler,
        )
        token = active_app.set(app)
        try:
            await app.run_async()
        except KeyboardInterrupt:
            return
        finally:
            active_app.reset(token)

    with _suppress_ask_process_logs():
        if session is not None:
            await _run_app(session)
            return

        if llm_adapter is None:
            raise TypeError("llm_adapter is required when session is not supplied")

        async with _AskSession(
            model=model,
            llm_adapter=llm_adapter,
            interactive=True,
            preamble_rule=preamble_rule,
        ) as created_session:
            await _run_app(created_session)


@app.async_command("ask", help="Send a one-shot LLM prompt.")
async def ask(
    *,
    project_id: ProjectIdOption,
    message: Annotated[
        str | None,
        typer.Option("--message", "-m", help="Prompt to send to the LLM"),
    ] = None,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format for non-interactive responses.",
            case_sensitive=False,
        ),
    ] = "markdown",
    model: Annotated[
        str,
        typer.Option("--model", help="Name of the LLM model to use"),
    ] = _DEFAULT_ASK_MODEL,
    preamble_rule: Annotated[
        bool,
        typer.Option(
            "--preamble-rule/--no-preamble-rule",
            help="Include the default rule asking the model to send concise pre-tool preambles.",
        ),
    ] = True,
) -> None:
    resolved_project_id = await resolve_project_id(project_id=project_id)
    access_token = await _resolve_ask_access_token()
    if access_token is None:
        typer.echo(
            "No MeshAgent token or OAuth access token available. "
            "Set MESHAGENT_TOKEN or run `meshagent auth login` first."
        )
        raise typer.Exit(1)

    llm_adapter = _build_ask_adapter(
        model=model,
        project_id=resolved_project_id,
        access_token=access_token,
    )
    if _should_launch_tui(
        message=message,
        stdin_is_tty=sys.stdin.isatty(),
        stdout_is_tty=sys.stdout.isatty(),
    ):
        await _run_ask_tui(
            model=model,
            llm_adapter=llm_adapter,
            preamble_rule=preamble_rule,
        )
        return

    if message is None:
        typer.echo(
            "Prompt required. Pass `-m/--message`, or run in a TTY for interactive mode."
        )
        raise typer.Exit(1)

    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "markdown"}:
        typer.echo(f"Unsupported format: {format}. Expected one of: text, markdown.")
        raise typer.Exit(1)

    if normalized_format == "markdown":
        result = await _run_ask_process(
            prompt=message,
            model=model,
            llm_adapter=llm_adapter,
            preamble_rule=preamble_rule,
        )
        Console().print(Markdown(result))
        return

    wrote_output = False

    def _write_message(message: AgentMessage) -> None:
        nonlocal wrote_output
        if isinstance(message, AgentTextContentDelta):
            wrote_output = True
            typer.echo(message.text, nl=False)

    result = await _run_ask_process(
        prompt=message,
        model=model,
        llm_adapter=llm_adapter,
        on_message=_write_message,
        preamble_rule=preamble_rule,
    )
    if wrote_output:
        typer.echo()
    else:
        typer.echo(result)


ask_command = async_typer.get_command(app, materialize_lazy=True)
