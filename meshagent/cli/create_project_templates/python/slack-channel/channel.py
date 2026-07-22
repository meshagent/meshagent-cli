from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Sequence
from urllib.parse import unquote, unquote_to_bytes, urlparse

from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.images_dataset import ImageDatasetClient
from meshagent.agents.messages import (
    AGENT_MESSAGE_TURN_START,
    AgentFileContentDelta,
    AgentGeneratedImage,
    AgentImageGenerationCompleted,
    AgentTextContent,
    AgentTextContentDelta,
    ThreadCleared,
    TurnEnded,
    TurnStart,
    TurnStarted,
    TurnStartRejected,
)
from meshagent.agents.process import Message
from meshagent.agents.threaded_channel import ThreadedChannel
from meshagent.api import Participant, RoomClient, RoomException
from meshagent.api.http import new_client_session


logger = logging.getLogger("meshagent.slack_channel")

QUEUE_NAME = os.getenv("MESHAGENT_SLACK_QUEUE_NAME", "slack-events")
QUEUE_POLL_INTERVAL_SECONDS = float(
    os.getenv("MESHAGENT_SLACK_QUEUE_POLL_INTERVAL_SECONDS", "1")
)
THREAD_PREFIX = os.getenv("MESHAGENT_SLACK_THREAD_PREFIX", "threads/slack")
RESPONSE_TIMEOUT_SECONDS = float(os.getenv("MESHAGENT_SLACK_RESPONSE_TIMEOUT", "300"))
SLACK_API_BASE_URL = os.getenv(
    "MESHAGENT_SLACK_API_BASE_URL",
    "https://slack.com/api",
).rstrip("/")
MAX_SLACK_MESSAGE_CHARS = 39000
DEFAULT_OUTBOUND_FILE_MAX_BYTES = 50_000_000
EVENT_STDOUT_ENV = "MESHAGENT_SLACK_EVENT_STDOUT"
TEXT_AUTHORED_INLINE_MEDIA_TYPES = frozenset(
    {
        "image/svg+xml",
        "image/x-portable-bitmap",
        "image/x-portable-graymap",
        "image/x-portable-pixmap",
    }
)


def _is_queue_already_exists_error(error: RoomException, *, queue_name: str) -> bool:
    message = str(error).lower()
    return (
        "queue" in message
        and "already exists" in message
        and queue_name.lower() in message
    )


@dataclass(frozen=True, slots=True)
class SlackInboundMessage:
    event_id: str
    team_id: str | None
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None = None
    channel_type: str | None = None
    event_type: str = "message"
    bot_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SlackTurnResponse:
    text: str
    files: tuple[_SlackOutboundFile, ...] = ()


@dataclass(frozen=True, slots=True)
class _SlackOutboundFile:
    data: bytes
    name: str
    mime_type: str | None = None


@dataclass(slots=True)
class _PendingSlackTurn:
    message: SlackInboundMessage
    response: asyncio.Future[_SlackTurnResponse]


@dataclass(slots=True)
class _ActiveSlackTurn:
    message: SlackInboundMessage
    response: asyncio.Future[_SlackTurnResponse]
    text_parts: list[str] = field(default_factory=list)
    attachment_lines: list[str] = field(default_factory=list)
    attachment_keys: set[str] = field(default_factory=set)
    files: list[_SlackOutboundFile] = field(default_factory=list)
    file_keys: set[str] = field(default_factory=set)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Slack channel.")
    return value


def env_flag(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value == "":
        return default
    return value in {"1", "true", "yes", "on"}


def env_int(name: str, *, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise RuntimeError(f"Set {name} to an integer.") from exc
    if value < 0:
        raise RuntimeError(f"Set {name} to 0 or a positive integer.")
    return value


def event_stdout(message: str) -> None:
    if env_flag(EVENT_STDOUT_ENV):
        print(f"[meshagent-slack] {message}", flush=True)


def slack_chunks(text: str) -> list[str]:
    normalized = text.strip()
    if normalized == "":
        return ["The room agent returned an empty response."]
    return [
        normalized[index : index + MAX_SLACK_MESSAGE_CHARS]
        for index in range(0, len(normalized), MAX_SLACK_MESSAGE_CHARS)
    ]


def slack_post_message_payload(
    *,
    channel: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, str]:
    normalized_channel = channel.strip()
    normalized_text = text.strip()
    if normalized_channel == "":
        raise ValueError("Slack messages require channel.")
    if normalized_text == "":
        raise ValueError("Slack messages require text.")
    payload = {"channel": normalized_channel, "text": normalized_text}
    if thread_ts is not None and thread_ts.strip() != "":
        payload["thread_ts"] = thread_ts.strip()
    return payload


def _slug(value: object) -> str:
    raw = str(value)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return slug or "unknown"


def room_storage_path_from_agent_file_url(*, url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme != "room":
        return None

    raw_path = f"{parsed.netloc}{parsed.path}"
    normalized = PurePosixPath("/" + raw_path).as_posix().strip("/")
    if normalized == "":
        return None
    if any(part in {".", ".."} for part in PurePosixPath(normalized).parts):
        return None
    return normalized


def is_slack_fetchable_file_url(*, url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc != ""


def _mime_type_from_http_header(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    mime_type = value.split(";", 1)[0].strip().lower()
    return mime_type or None


def _file_name_from_http_url(*, url: str, mime_type: str | None = None) -> str:
    parsed = urlparse(url.strip())
    name = unquote(PurePosixPath(parsed.path).name).strip()
    if name == "":
        name = "slack-file"
    if "." not in PurePosixPath(name).name:
        extension = mimetypes.guess_extension(mime_type or "") or ""
        if extension != "":
            name = f"{name}{extension}"
    return name


def _ascii_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def _decode_base64_text(value: str) -> bytes | None:
    compact = "".join(value.strip().split())
    if compact == "" or len(compact) % 4 != 0:
        return None
    try:
        decoded = base64.b64decode(compact, validate=True)
    except Exception:
        return None
    return decoded or None


def _decode_data_url_payload(value: str) -> tuple[bytes, str | None] | None:
    raw = value.strip()
    if not raw.lower().startswith("data:"):
        return None

    header, separator, payload = raw.partition(",")
    if separator != ",":
        return None

    metadata = header[5:]
    parts = [part.strip() for part in metadata.split(";")]
    mime_type = parts[0].lower() if parts and parts[0] != "" else None
    is_base64 = any(part.lower() == "base64" for part in parts[1:])
    if is_base64:
        try:
            payload_text = unquote_to_bytes(payload).decode("ascii")
        except UnicodeDecodeError:
            return None
        decoded = _decode_base64_text(payload_text)
        return None if decoded is None else (decoded, mime_type)

    return (unquote_to_bytes(payload), mime_type)


def _data_url_is_base64(*, url: str) -> bool:
    header, separator, _payload = url.strip().partition(",")
    if separator != "," or not header.lower().startswith("data:"):
        return False
    return any(part.strip().lower() == "base64" for part in header[5:].split(";")[1:])


def _decode_base64_file_payload(value: object) -> tuple[bytes, str | None] | None:
    text = _ascii_text(value)
    if text is None:
        return None

    raw = text.strip()
    if raw.lower().startswith("data:"):
        return _decode_data_url_payload(raw)

    decoded = _decode_base64_text(raw)
    return None if decoded is None else (decoded, None)


def _looks_like_base64_file_data(*, data: bytes, mime_type: str | None) -> bool:
    normalized_mime_type = (mime_type or "").strip().lower()
    if normalized_mime_type.startswith("text/"):
        return False
    return _decode_base64_file_payload(data) is not None


def _slack_file_bytes_from_content_data(
    *,
    data: object,
    mime_type: str | None,
) -> bytes:
    if isinstance(data, str):
        decoded = _decode_base64_file_payload(data)
        return decoded[0] if decoded is not None else data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw_data = bytes(data)
        decoded = _decode_base64_file_payload(raw_data)
        if decoded is not None and _looks_like_base64_file_data(
            data=raw_data,
            mime_type=mime_type,
        ):
            return decoded[0]
        return raw_data
    raise TypeError("Slack file content data must be bytes or base64 text.")


def _slack_file_for_data_url(
    *, url: str, name: str = "slack-file"
) -> _SlackOutboundFile | None:
    decoded = _decode_data_url_payload(url)
    if decoded is None:
        return None
    data, mime_type = decoded
    normalized_mime_type = (mime_type or "").strip().lower()
    if (
        not _data_url_is_base64(url=url)
        and normalized_mime_type in TEXT_AUTHORED_INLINE_MEDIA_TYPES
    ):
        logger.warning(
            "slack_file_data_url_ignored_text_authored mime_type=%s",
            normalized_mime_type,
        )
        return None
    extension = mimetypes.guess_extension(mime_type or "") or ""
    filename = name if PurePosixPath(name).suffix != "" else f"{name}{extension}"
    return _SlackOutboundFile(data=data, name=filename, mime_type=mime_type)


def _slack_api_form_payload(payload: dict[str, Any]) -> dict[str, str]:
    form: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            form[key] = json.dumps(value, separators=(",", ":"))
            continue
        if isinstance(value, bool):
            form[key] = "true" if value else "false"
            continue
        form[key] = str(value)
    return form


def _slack_generated_image_name(*, mime_type: str | None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ".png"
    return f"generated-image{extension}"


def _slack_media_source_key(*, kind: str, value: object) -> str | None:
    normalized = str(value).strip()
    if normalized == "":
        return None
    return f"{kind}:{normalized}"


def _slack_outbound_file_content_key(media_file: _SlackOutboundFile) -> str:
    digest = hashlib.sha256(media_file.data).hexdigest()
    return f"bytes:{len(media_file.data)}:{digest}:{media_file.mime_type or ''}"


def _append_unique_slack_file(
    *,
    active: _ActiveSlackTurn,
    media_file: _SlackOutboundFile,
    source_key: str | None = None,
) -> bool:
    content_key = _slack_outbound_file_content_key(media_file)
    candidate_keys = [key for key in (source_key, content_key) if key is not None]
    if any(key in active.file_keys for key in candidate_keys):
        logger.info(
            "slack_duplicate_file_skipped source_key=%s name=%s mime_type=%s",
            source_key,
            media_file.name,
            media_file.mime_type,
        )
        return False
    active.files.append(media_file)
    active.file_keys.update(candidate_keys)
    return True


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _sequence_from_value(value: str | Sequence[str] | None) -> frozenset[str] | None:
    if value is None:
        return None
    values = value.split(",") if isinstance(value, str) else value
    normalized = frozenset(
        str(item).strip() for item in values if str(item).strip() != ""
    )
    return normalized or None


def _queue_body_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        raise ValueError("Slack queue messages must be JSON objects.")
    body = message.get("body")
    if not isinstance(body, str):
        raise ValueError("Slack queue messages must include a string body.")
    return body


def _queue_json_from_message(message: Any) -> dict[str, Any]:
    try:
        data = json.loads(_queue_body_from_message(message))
    except json.JSONDecodeError as exc:
        raise ValueError("Slack queue body must be JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("Slack queue body must be a JSON object.")
    return data


def _slack_event_log_fields(data: dict[str, Any]) -> dict[str, str]:
    event = data.get("event")
    if not isinstance(event, dict):
        event = {}
    return {
        "type": _string_value(data.get("type")),
        "event_type": _string_value(event.get("type")),
        "subtype": _string_value(event.get("subtype")),
        "event_id": _string_value(data.get("event_id")),
        "channel": _string_value(event.get("channel")),
        "user": _string_value(event.get("user")),
        "bot_id": _string_value(event.get("bot_id")),
    }


def _log_slack_queue_event(
    message: str,
    *,
    data: dict[str, Any],
    level: int = logging.INFO,
) -> None:
    fields = _slack_event_log_fields(data)
    logger.log(
        level,
        "%s type=%s event_type=%s subtype=%s event_id=%s channel=%s user=%s bot_id=%s",
        message,
        fields["type"],
        fields["event_type"],
        fields["subtype"],
        fields["event_id"],
        fields["channel"],
        fields["user"],
        fields["bot_id"],
    )


def _is_transient_room_disconnect_error(error: BaseException) -> bool:
    message = str(error).lower()
    return (
        "room connection is disconnected" in message
        or "room connection closed before tool call completed" in message
        or "websocket closed with code 1006" in message
        or "cannot write to closing transport" in message
    )


def _event_text(event: dict[str, Any]) -> str:
    text = _string_value(event.get("text"))
    if text != "":
        return text
    blocks = event.get("blocks")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text_obj = block.get("text")
        if not isinstance(text_obj, dict):
            continue
        block_text = _string_value(text_obj.get("text"))
        if block_text != "":
            parts.append(block_text)
    return "\n".join(parts).strip()


def _parse_slack_inbound_message_from_data(
    data: dict[str, Any],
) -> SlackInboundMessage | None:
    payload_type = _string_value(data.get("type"))
    if payload_type == "url_verification":
        return None
    if payload_type != "event_callback":
        raise ValueError("Slack webhook body must be an event_callback payload.")

    event = data.get("event")
    if not isinstance(event, dict):
        raise ValueError("Slack event_callback payload must include an event object.")

    event_type = _string_value(event.get("type"))
    if event_type not in {"app_mention", "message"}:
        return None

    subtype = _string_value(event.get("subtype"))
    if subtype not in {"", "bot_message"}:
        return None
    bot_id = _string_value(event.get("bot_id")) or None

    channel = _string_value(event.get("channel"))
    user = _string_value(event.get("user")) or bot_id or ""
    text = _event_text(event)
    ts = _string_value(event.get("ts")) or _string_value(event.get("event_ts"))
    if channel == "" or user == "" or text == "" or ts == "":
        return None

    event_id = (
        _string_value(data.get("event_id"))
        or _string_value(event.get("client_msg_id"))
        or f"{channel}:{ts}"
    )
    return SlackInboundMessage(
        event_id=event_id,
        team_id=_string_value(data.get("team_id")) or None,
        channel=channel,
        user=user,
        text=text,
        ts=ts,
        thread_ts=_string_value(event.get("thread_ts")) or None,
        channel_type=_string_value(event.get("channel_type")) or None,
        event_type=event_type,
        bot_id=bot_id,
    )


def parse_slack_inbound_messages(message: Any) -> list[SlackInboundMessage]:
    parsed = _parse_slack_inbound_message_from_data(_queue_json_from_message(message))
    return [] if parsed is None else [parsed]


def parse_slack_inbound_message(message: Any) -> SlackInboundMessage:
    messages = parse_slack_inbound_messages(message)
    if len(messages) == 0:
        raise ValueError("Slack webhook body did not include a supported message.")
    return messages[0]


def _append_unique_attachment_line(
    *,
    active: _ActiveSlackTurn,
    key: str,
    line: str,
) -> None:
    normalized_key = key.strip()
    normalized_line = line.strip()
    if normalized_key == "" or normalized_line == "":
        return
    if normalized_key in active.attachment_keys:
        return
    active.attachment_keys.add(normalized_key)
    active.attachment_lines.append(normalized_line)


class SlackChannel(ThreadedChannel):
    def __init__(
        self,
        *,
        room: RoomClient,
        bot_token: str,
        queue_name: str = QUEUE_NAME,
        api_base_url: str = SLACK_API_BASE_URL,
        threading_mode: str | None = None,
        thread_dir: str | None = None,
        thread_url_scheme: str | None = None,
        thread_path_extension: str = ".thread",
        thread_list_path: str | None = None,
        llm_adapter: LLMAdapter | None = None,
        thread_prefix: str = THREAD_PREFIX,
        allowed_channels: str | Sequence[str] | None = None,
        reply_in_thread: bool = True,
        ignore_bots: bool = True,
        dry_run: bool = False,
        outbound_file_max_bytes: int = DEFAULT_OUTBOUND_FILE_MAX_BYTES,
        queue_poll_interval_seconds: float = QUEUE_POLL_INTERVAL_SECONDS,
        receive_from_http: bool = False,
    ) -> None:
        super().__init__(
            room=room,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_url_scheme=thread_url_scheme,
            thread_path_extension=thread_path_extension,
            thread_list_path=thread_list_path,
            llm_adapter=llm_adapter,
        )
        normalized_queue_name = queue_name.strip()
        if normalized_queue_name == "":
            raise ValueError("queue_name must not be empty")
        if not dry_run and bot_token.strip() == "":
            raise ValueError("bot_token must not be empty unless dry_run is enabled")
        if queue_poll_interval_seconds <= 0:
            raise ValueError("queue_poll_interval_seconds must be positive")
        self._queue_name = normalized_queue_name
        self._bot_token = bot_token.strip()
        self._api_base_url = api_base_url.rstrip("/")
        self._thread_prefix = thread_prefix.rstrip("/") or "threads/slack"
        self._allowed_channels = _sequence_from_value(allowed_channels)
        self._reply_in_thread = reply_in_thread
        self._ignore_bots = ignore_bots
        self._dry_run = dry_run
        self._outbound_file_max_bytes = outbound_file_max_bytes
        self._queue_poll_interval_seconds = queue_poll_interval_seconds
        self._receive_from_http = receive_from_http
        self._http_session: Any | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._pending_turns_by_message_id: dict[str, _PendingSlackTurn] = {}
        self._active_turns_by_turn_id: dict[str, _ActiveSlackTurn] = {}
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    def _default_thread_dir_fallback_name(self) -> str:
        return "slack"

    def handles(self, message: Message) -> bool:
        return isinstance(
            message.data,
            (
                AgentFileContentDelta,
                AgentImageGenerationCompleted,
                AgentTextContentDelta,
                ThreadCleared,
                TurnEnded,
                TurnStarted,
                TurnStartRejected,
            ),
        )

    async def on_start(self) -> None:
        await self.publish_thread_attributes()
        await self.open_thread_list_document()
        if not self._dry_run:
            self._http_session = new_client_session(
                headers={"Authorization": f"Bearer {self._bot_token}"}
            )
        if not self._receive_from_http:
            try:
                await self._room.queues.open(name=self._queue_name)
            except RoomException as exc:
                if not _is_queue_already_exists_error(exc, queue_name=self._queue_name):
                    raise
                logger.info("slack_channel_queue_exists queue=%s", self._queue_name)
            self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("slack_channel_started transport=%s", self.transport)
        event_stdout(f"started transport={self.transport}")

    @property
    def transport(self) -> str:
        return "http" if self._receive_from_http else f"queue:{self._queue_name}"

    async def process_webhook(self, body: str) -> None:
        await self._process_queue_message({"body": body})

    async def on_stop(self) -> None:
        receive_task = self._receive_task
        self._receive_task = None
        if receive_task is not None:
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receive_task

        message_tasks = list(self._message_tasks)
        for task in message_tasks:
            task.cancel()
        if message_tasks:
            await asyncio.gather(*message_tasks, return_exceptions=True)
        self._message_tasks.clear()

        await self._cancel_thread_list_background_tasks()
        await self.close_thread_list_document()
        for pending in self._pending_turns_by_message_id.values():
            if not pending.response.done():
                pending.response.cancel()
        for active in self._active_turns_by_turn_id.values():
            if not active.response.done():
                active.response.cancel()
        self._pending_turns_by_message_id.clear()
        self._active_turns_by_turn_id.clear()

        http_session = self._http_session
        self._http_session = None
        if http_session is not None:
            await http_session.close()

    async def on_message(self, message: Message) -> None:
        data = message.data
        if isinstance(data, TurnStarted):
            pending = self._pending_turns_by_message_id.pop(
                data.source_message_id,
                None,
            )
            if pending is None:
                return
            event_stdout(
                "turn_started "
                f"event_id={pending.message.event_id} turn_id={data.turn_id}"
            )
            self._active_turns_by_turn_id[data.turn_id] = _ActiveSlackTurn(
                message=pending.message,
                response=pending.response,
            )
            return

        if isinstance(data, TurnStartRejected):
            pending = self._pending_turns_by_message_id.pop(
                data.source_message_id,
                None,
            )
            if pending is None or pending.response.done():
                return
            event_stdout(
                "turn_rejected "
                f"event_id={pending.message.event_id} error={data.error.message}"
            )
            pending.response.set_result(_SlackTurnResponse(text=data.error.message))
            return

        if isinstance(data, AgentTextContentDelta):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is not None:
                active.text_parts.append(data.text)
            return

        if isinstance(data, AgentFileContentDelta):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is not None:
                source_key = _slack_media_source_key(kind="file-url", value=data.url)
                if source_key is not None and source_key in active.file_keys:
                    logger.info(
                        "slack_duplicate_file_skipped source_key=%s", source_key
                    )
                    return
                media = await self._slack_file_for_agent_file_url(url=data.url)
                if media is None:
                    _append_unique_attachment_line(
                        active=active,
                        key=f"file:{data.url}",
                        line=f"Attachment: {data.url}",
                    )
                else:
                    _append_unique_slack_file(
                        active=active,
                        media_file=media,
                        source_key=source_key,
                    )
            return

        if isinstance(data, AgentImageGenerationCompleted):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is not None:
                for image in data.images:
                    source_key = _slack_media_source_key(
                        kind="generated-image-uri",
                        value=image.uri or "",
                    )
                    if source_key is not None and source_key in active.file_keys:
                        logger.info(
                            "slack_duplicate_file_skipped source_key=%s",
                            source_key,
                        )
                        continue
                    media = await self._slack_file_for_generated_image(image=image)
                    if media is None:
                        uri = _string_value(image.uri)
                        if uri == "":
                            continue
                        _append_unique_attachment_line(
                            active=active,
                            key=f"image:{uri}",
                            line=f"Image: {uri}",
                        )
                    else:
                        _append_unique_slack_file(
                            active=active,
                            media_file=media,
                            source_key=source_key,
                        )
            return

        if isinstance(data, ThreadCleared):
            self._clear_thread_state(thread_id=data.thread_id)
            return

        if not isinstance(data, TurnEnded):
            return

        active = self._active_turns_by_turn_id.pop(data.turn_id, None)
        if active is None or active.response.done():
            return
        event_stdout(
            f"turn_ended event_id={active.message.event_id} has_error={data.error is not None}"
        )
        if data.error is not None:
            active.response.set_result(_SlackTurnResponse(text=data.error.message))
            return
        text = "".join(active.text_parts).strip()
        if len(active.attachment_lines) > 0:
            attachment_text = "\n".join(active.attachment_lines)
            text = f"{text}\n\n{attachment_text}".strip()
        active.response.set_result(
            _SlackTurnResponse(text=text, files=tuple(active.files))
        )

    async def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                queued_message = await self._room.queues.receive(
                    name=self._queue_name,
                    create=True,
                    wait=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._room.is_closed:
                    logger.debug("stopping Slack receive loop after room close")
                    return
                if _is_transient_room_disconnect_error(exc):
                    logger.info(
                        "slack_queue_receive_waiting_for_room_reconnect queue=%s error=%s",
                        self._queue_name,
                        exc,
                    )
                    event_stdout(
                        "queue_receive_waiting_for_room_reconnect "
                        f"queue={self._queue_name}"
                    )
                    await asyncio.sleep(2)
                    continue
                logger.exception(
                    "slack_queue_receive_failed queue=%s", self._queue_name
                )
                await asyncio.sleep(1)
                continue

            if queued_message is None:
                await asyncio.sleep(self._queue_poll_interval_seconds)
                continue

            task = asyncio.create_task(self._process_queue_message(queued_message))
            self._message_tasks.add(task)
            task.add_done_callback(self._message_tasks.discard)
            task.add_done_callback(self._log_message_task_failure)

    async def _process_queue_message(self, queued_message: Any) -> None:
        try:
            data = _queue_json_from_message(queued_message)
        except ValueError as exc:
            logger.warning("slack_queue_message_ignored error=%s", exc)
            event_stdout(f"queue_ignored error={exc}")
            return

        fields = _slack_event_log_fields(data)
        event_stdout(
            "queue_received "
            f"type={fields['type']} event_type={fields['event_type']} "
            f"subtype={fields['subtype']} event_id={fields['event_id']} "
            f"channel={fields['channel']} user={fields['user']} bot_id={fields['bot_id']}"
        )
        parsed = _parse_slack_inbound_message_from_data(data)
        slack_messages = [] if parsed is None else [parsed]
        if len(slack_messages) == 0:
            _log_slack_queue_event(
                "slack_queue_message_ignored reason=no_supported_events",
                data=data,
            )
            event_stdout(
                "queue_ignored reason=no_supported_events "
                f"event_id={fields['event_id']}"
            )
            return

        for slack_message in slack_messages:
            if self._ignore_bots and slack_message.bot_id is not None:
                logger.info(
                    "slack_message_ignored reason=bot_message bot_id=%s event_id=%s",
                    slack_message.bot_id,
                    slack_message.event_id,
                )
                event_stdout(
                    "message_ignored reason=bot_message "
                    f"event_id={slack_message.event_id} bot_id={slack_message.bot_id}"
                )
                continue
            if not self._is_channel_allowed(slack_message.channel):
                logger.warning(
                    "slack_message_denied reason=not_allowlisted channel=%s "
                    "event_id=%s allowed_channels_hint=MESHAGENT_SLACK_ALLOWED_CHANNELS=%s",
                    slack_message.channel,
                    slack_message.event_id,
                    slack_message.channel,
                )
                event_stdout(
                    "message_denied reason=not_allowlisted "
                    f"channel={slack_message.channel} event_id={slack_message.event_id}"
                )
                continue

            logger.info(
                "slack_message_allowed channel=%s user=%s event_id=%s type=%s",
                slack_message.channel,
                slack_message.user,
                slack_message.event_id,
                slack_message.event_type,
            )
            event_stdout(
                "message_allowed "
                f"channel={slack_message.channel} user={slack_message.user} "
                f"event_id={slack_message.event_id} type={slack_message.event_type}"
            )
            conversation_key = (
                f"{slack_message.channel}:{slack_message.thread_ts or slack_message.ts}"
            )
            lock = self._conversation_locks.setdefault(
                conversation_key,
                asyncio.Lock(),
            )
            async with lock:
                response = await self._send_slack_turn(message=slack_message)
                reply_thread_ts = self._reply_thread_ts(message=slack_message)
                text_chunks = (
                    slack_chunks(response.text)
                    if response.text.strip() != "" or len(response.files) == 0
                    else []
                )
                for chunk in text_chunks:
                    await self._send_slack_message(
                        channel=slack_message.channel,
                        text=chunk,
                        thread_ts=reply_thread_ts,
                    )
                for media_file in response.files:
                    await self._send_slack_response_file(
                        message=slack_message,
                        media_file=media_file,
                        thread_ts=reply_thread_ts,
                    )

    async def _send_slack_turn(
        self,
        *,
        message: SlackInboundMessage,
    ) -> _SlackTurnResponse:
        participant_attributes = {
            "name": message.user,
            "role": "user",
            "slack.channel": message.channel,
            "slack.user": message.user,
            "slack.event_id": message.event_id,
            "slack.event_type": message.event_type,
            "slack.ts": message.ts,
        }
        if message.team_id is not None:
            participant_attributes["slack.team_id"] = message.team_id
        if message.thread_ts is not None:
            participant_attributes["slack.thread_ts"] = message.thread_ts
        if message.channel_type is not None:
            participant_attributes["slack.channel_type"] = message.channel_type

        participant = Participant(
            id=f"slack:{_slug(message.team_id or 'team')}:{_slug(message.user)}",
            attributes=participant_attributes,
        )
        thread_id = self._thread_id_for_conversation(message=message)
        self.bump_thread(path=thread_id, name=message.user)

        response: asyncio.Future[_SlackTurnResponse] = (
            asyncio.get_running_loop().create_future()
        )
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=thread_id,
            content=[AgentTextContent(type="text", text=message.text)],
        )
        self._pending_turns_by_message_id[turn_start.message_id] = _PendingSlackTurn(
            message=message,
            response=response,
        )
        event_stdout(
            "turn_emit "
            f"event_id={message.event_id} message_id={turn_start.message_id} "
            f"thread_id={thread_id}"
        )
        self.emit(sender=participant, payload=turn_start)

        try:
            return await asyncio.wait_for(response, timeout=RESPONSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._clear_pending_response(response=response)
            logger.warning(
                "slack_turn_response_timed_out event_id=%s",
                message.event_id,
            )
            event_stdout(f"turn_timeout event_id={message.event_id}")
            return _SlackTurnResponse(
                text="The room agent did not answer before the Slack channel timed out."
            )
        except Exception:
            self._clear_pending_response(response=response)
            logger.exception("slack_turn_failed event_id=%s", message.event_id)
            event_stdout(f"turn_failed event_id={message.event_id}")
            return _SlackTurnResponse(
                text="The room agent could not answer that message."
            )

    def _raise_if_outbound_file_too_large(
        self,
        *,
        file_id: object,
        size_bytes: int,
        source: str,
    ) -> None:
        if (
            self._outbound_file_max_bytes > 0
            and size_bytes > self._outbound_file_max_bytes
        ):
            raise ValueError(
                f"Slack file {file_id} is {size_bytes} bytes from {source}, "
                "which exceeds MESHAGENT_SLACK_OUTBOUND_FILE_MAX_BYTES="
                f"{self._outbound_file_max_bytes}."
            )

    async def _download_slack_http_file_url(
        self,
        *,
        url: str,
    ) -> _SlackOutboundFile | None:
        try:
            async with new_client_session() as http_session:
                async with http_session.get(url) as response:
                    if response.status >= 400:
                        response_text = await response.text()
                        logger.error(
                            "slack_file_http_download_failed status=%s url=%s body=%s",
                            response.status,
                            url,
                            response_text[:500],
                        )
                        response.raise_for_status()

                    mime_type = _mime_type_from_http_header(
                        response.headers.get("content-type")
                    )
                    content_length = response.headers.get("content-length")
                    if isinstance(content_length, str) and content_length.isdigit():
                        self._raise_if_outbound_file_too_large(
                            file_id=url,
                            size_bytes=int(content_length),
                            source="metadata",
                        )
                    data = await response.read()
        except ValueError as exc:
            logger.warning(
                "slack_file_http_download_skipped_oversize url=%s error=%s",
                url,
                exc,
            )
            return None
        except Exception:
            logger.exception("slack_file_http_download_failed url=%s", url)
            return None

        try:
            self._raise_if_outbound_file_too_large(
                file_id=url,
                size_bytes=len(data),
                source="download",
            )
        except ValueError as exc:
            logger.warning(
                "slack_file_http_download_skipped_oversize url=%s error=%s",
                url,
                exc,
            )
            return None
        return _SlackOutboundFile(
            data=data,
            name=_file_name_from_http_url(url=url, mime_type=mime_type),
            mime_type=mime_type,
        )

    async def _slack_file_for_agent_file_url(
        self,
        *,
        url: str,
    ) -> _SlackOutboundFile | None:
        normalized_url = url.strip()
        if is_slack_fetchable_file_url(url=normalized_url):
            return await self._download_slack_http_file_url(url=normalized_url)

        data_url_file = _slack_file_for_data_url(url=normalized_url)
        if data_url_file is not None:
            try:
                self._raise_if_outbound_file_too_large(
                    file_id=normalized_url,
                    size_bytes=len(data_url_file.data),
                    source="data-url",
                )
            except ValueError as exc:
                logger.warning("slack_file_data_url_skipped_oversize error=%s", exc)
                return None
            return data_url_file
        if normalized_url.lower().startswith("data:"):
            return None

        storage_path = room_storage_path_from_agent_file_url(url=normalized_url)
        if storage_path is None:
            logger.warning("slack_file_url_ignored url=%s", normalized_url)
            return None

        try:
            content = await self._room.storage.download(path=storage_path)
        except Exception:
            logger.exception("slack_file_download_failed path=%s", storage_path)
            return None

        try:
            file_data = _slack_file_bytes_from_content_data(
                data=content.data,
                mime_type=content.mime_type,
            )
            self._raise_if_outbound_file_too_large(
                file_id=storage_path,
                size_bytes=len(file_data),
                source="room-storage",
            )
        except ValueError as exc:
            logger.warning(
                "slack_file_skipped_oversize path=%s error=%s",
                storage_path,
                exc,
            )
            return None
        except Exception:
            logger.exception("slack_file_content_decode_failed path=%s", storage_path)
            return None

        return _SlackOutboundFile(
            data=file_data,
            name=content.name or PurePosixPath(storage_path).name,
            mime_type=content.mime_type,
        )

    async def _slack_file_for_generated_image(
        self,
        *,
        image: AgentGeneratedImage,
    ) -> _SlackOutboundFile | None:
        uri = (image.uri or "").strip()
        if uri == "":
            return None

        data_url_file = _slack_file_for_data_url(
            url=uri,
            name=_slack_generated_image_name(mime_type=image.mime_type),
        )
        if data_url_file is not None:
            mime_type = data_url_file.mime_type or image.mime_type
            try:
                self._raise_if_outbound_file_too_large(
                    file_id=uri,
                    size_bytes=len(data_url_file.data),
                    source="data-url",
                )
            except ValueError as exc:
                logger.warning(
                    "slack_generated_image_data_url_skipped_oversize error=%s",
                    exc,
                )
                return None
            return _SlackOutboundFile(
                data=data_url_file.data,
                name=_slack_generated_image_name(mime_type=mime_type),
                mime_type=mime_type,
            )

        try:
            record = await ImageDatasetClient(self._room.datasets).read_record_from_uri(
                uri,
                fallback_mime_type=image.mime_type,
            )
        except Exception:
            logger.exception("slack_generated_image_download_failed uri=%s", uri)
            return None

        if record is None:
            logger.warning("slack_generated_image_ignored uri=%s", uri)
            return None

        try:
            self._raise_if_outbound_file_too_large(
                file_id=uri,
                size_bytes=len(record.data),
                source="image-dataset",
            )
        except ValueError as exc:
            logger.warning(
                "slack_generated_image_skipped_oversize uri=%s error=%s",
                uri,
                exc,
            )
            return None

        return _SlackOutboundFile(
            data=record.data,
            name=_slack_generated_image_name(mime_type=record.mime_type),
            mime_type=record.mime_type,
        )

    def _reply_thread_ts(self, *, message: SlackInboundMessage) -> str | None:
        if message.thread_ts is not None:
            return message.thread_ts
        if self._reply_in_thread:
            return message.ts
        return None

    async def _send_slack_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None,
    ) -> None:
        payload = slack_post_message_payload(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
        if self._dry_run:
            logger.info("slack_dry_run_message payload=%s", payload)
            event_stdout(
                "dry_run_message "
                f"channel={channel} thread_ts={thread_ts or ''} chars={len(text)}"
            )
            return

        if self._http_session is None:
            raise RuntimeError("Slack HTTP session was not started.")

        async with self._http_session.post(
            f"{self._api_base_url}/chat.postMessage",
            json=payload,
        ) as response:
            body_text = await response.text()
            if response.status >= 400:
                logger.error(
                    "slack_message_send_failed status=%s body=%s",
                    response.status,
                    body_text,
                )
                event_stdout(
                    f"message_send_failed status={response.status} channel={channel}"
                )
                response.raise_for_status()
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                logger.error("slack_message_send_failed invalid_json=%s", body_text)
                raise RuntimeError("Slack chat.postMessage returned invalid JSON.")
            if not body.get("ok"):
                error = body.get("error") or "unknown_error"
                logger.error("slack_message_send_failed error=%s body=%s", error, body)
                event_stdout(f"message_send_failed error={error} channel={channel}")
                raise RuntimeError(f"Slack chat.postMessage failed: {error}")
            logger.info("slack_message_sent channel=%s", channel)
            event_stdout(f"message_sent channel={channel}")

    async def _send_slack_response_file(
        self,
        *,
        message: SlackInboundMessage,
        media_file: _SlackOutboundFile,
        thread_ts: str | None,
    ) -> None:
        try:
            await self._send_slack_file(
                channel=message.channel,
                media_file=media_file,
                thread_ts=thread_ts,
            )
        except Exception as exc:
            logger.exception(
                "slack_file_send_failed event_id=%s channel=%s name=%s",
                message.event_id,
                message.channel,
                media_file.name,
            )
            event_stdout(
                "file_send_failed "
                f"event_id={message.event_id} channel={message.channel} "
                f"name={media_file.name} error={exc}"
            )
            await self._send_slack_message(
                channel=message.channel,
                text=(
                    "I generated an attachment, but Slack rejected the upload "
                    f"for {media_file.name}: {exc}"
                ),
                thread_ts=thread_ts,
            )

    async def _slack_api_json(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._http_session is None:
            raise RuntimeError("Slack HTTP session was not started.")

        async with self._http_session.post(
            f"{self._api_base_url}/{method}",
            data=_slack_api_form_payload(payload),
        ) as response:
            body_text = await response.text()
            if response.status >= 400:
                logger.error(
                    "slack_api_request_failed method=%s status=%s body=%s",
                    method,
                    response.status,
                    body_text,
                )
                response.raise_for_status()
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError as exc:
                logger.error(
                    "slack_api_request_failed method=%s invalid_json=%s",
                    method,
                    body_text,
                )
                raise RuntimeError(f"Slack {method} returned invalid JSON.") from exc
            if not body.get("ok"):
                error = body.get("error") or "unknown_error"
                logger.error(
                    "slack_api_request_failed method=%s error=%s body=%s",
                    method,
                    error,
                    body,
                )
                raise RuntimeError(f"Slack {method} failed: {error}")
            return body

    async def _send_slack_file(
        self,
        *,
        channel: str,
        media_file: _SlackOutboundFile,
        thread_ts: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "filename": media_file.name,
            "length": len(media_file.data),
        }
        if self._dry_run:
            logger.info(
                "slack_dry_run_file channel=%s thread_ts=%s payload=%s",
                channel,
                thread_ts,
                {
                    **payload,
                    "mime_type": media_file.mime_type,
                    "data_bytes": len(media_file.data),
                },
            )
            return

        upload = await self._slack_api_json("files.getUploadURLExternal", payload)
        upload_url = _string_value(upload.get("upload_url"))
        file_id = _string_value(upload.get("file_id"))
        if upload_url == "" or file_id == "":
            raise RuntimeError(
                "Slack files.getUploadURLExternal returned no upload URL."
            )

        async with new_client_session() as http_session:
            async with http_session.post(
                upload_url,
                data=media_file.data,
                headers={
                    "Content-Type": media_file.mime_type or "application/octet-stream"
                },
            ) as response:
                body_text = await response.text()
                if response.status >= 400:
                    logger.error(
                        "slack_file_upload_failed status=%s body=%s",
                        response.status,
                        body_text,
                    )
                    response.raise_for_status()

        complete_payload: dict[str, Any] = {
            "files": [{"id": file_id, "title": media_file.name}],
            "channel_id": channel,
        }
        if thread_ts is not None and thread_ts.strip() != "":
            complete_payload["thread_ts"] = thread_ts.strip()
        await self._slack_api_json("files.completeUploadExternal", complete_payload)
        logger.info("slack_file_sent channel=%s name=%s", channel, media_file.name)

    def _is_channel_allowed(self, channel: str) -> bool:
        if self._allowed_channels is None:
            return True
        return channel in self._allowed_channels

    def _thread_id_for_conversation(self, *, message: SlackInboundMessage) -> str:
        thread_key = message.thread_ts or message.ts
        uses_dataset_thread_url = (
            self._thread_url_scheme == "dataset://"
            or self._thread_prefix.startswith("dataset://")
        )
        extension = "" if uses_dataset_thread_url else self._thread_path_extension
        path = f"{self._thread_prefix}/{_slug(message.channel)}-{_slug(thread_key)}{extension}"
        if "://" in path:
            return path
        return self._thread_url_for_path(path=path)

    def _clear_thread_state(self, *, thread_id: str) -> None:
        pending_message_ids = [
            message_id
            for message_id, pending in self._pending_turns_by_message_id.items()
            if self._thread_id_for_conversation(message=pending.message) == thread_id
        ]
        for message_id in pending_message_ids:
            pending = self._pending_turns_by_message_id.pop(message_id)
            if not pending.response.done():
                pending.response.cancel()

        active_turn_ids = [
            turn_id
            for turn_id, active in self._active_turns_by_turn_id.items()
            if self._thread_id_for_conversation(message=active.message) == thread_id
        ]
        for turn_id in active_turn_ids:
            active = self._active_turns_by_turn_id.pop(turn_id)
            if not active.response.done():
                active.response.cancel()

    def _clear_pending_response(
        self, *, response: asyncio.Future[_SlackTurnResponse]
    ) -> None:
        pending_message_ids = [
            message_id
            for message_id, pending in self._pending_turns_by_message_id.items()
            if pending.response is response
        ]
        for message_id in pending_message_ids:
            self._pending_turns_by_message_id.pop(message_id, None)
        active_turn_ids = [
            turn_id
            for turn_id, active in self._active_turns_by_turn_id.items()
            if active.response is response
        ]
        for turn_id in active_turn_ids:
            self._active_turns_by_turn_id.pop(turn_id, None)

    @staticmethod
    def _log_message_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("slack_message_task_failed", exc_info=exc)


def create_channel(
    *,
    room: RoomClient,
    threading_mode: str | None = None,
    thread_dir: str | None = None,
    thread_url_scheme: str | None = None,
    thread_path_extension: str = ".thread",
    thread_list_path: str | None = None,
    llm_adapter: LLMAdapter | None = None,
    receive_from_http: bool = False,
) -> SlackChannel:
    dry_run = env_flag("MESHAGENT_SLACK_DRY_RUN")
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not dry_run and bot_token == "":
        bot_token = required_env("SLACK_BOT_TOKEN")
    return SlackChannel(
        room=room,
        bot_token=bot_token,
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_url_scheme=thread_url_scheme,
        thread_path_extension=thread_path_extension,
        thread_list_path=thread_list_path,
        llm_adapter=llm_adapter,
        thread_prefix=os.getenv("MESHAGENT_SLACK_THREAD_PREFIX", THREAD_PREFIX),
        allowed_channels=os.getenv("MESHAGENT_SLACK_ALLOWED_CHANNELS"),
        reply_in_thread=env_flag("MESHAGENT_SLACK_REPLY_IN_THREAD", default=True),
        ignore_bots=env_flag("MESHAGENT_SLACK_IGNORE_BOTS", default=True),
        dry_run=dry_run,
        outbound_file_max_bytes=env_int(
            "MESHAGENT_SLACK_OUTBOUND_FILE_MAX_BYTES",
            default=DEFAULT_OUTBOUND_FILE_MAX_BYTES,
        ),
        queue_poll_interval_seconds=float(
            os.getenv(
                "MESHAGENT_SLACK_QUEUE_POLL_INTERVAL_SECONDS",
                str(QUEUE_POLL_INTERVAL_SECONDS),
            )
        ),
        receive_from_http=receive_from_http,
    )
