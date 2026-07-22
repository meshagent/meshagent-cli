from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, unquote_to_bytes, urlparse

from aiohttp import FormData, web
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.messages import (
    AGENT_MESSAGE_TURN_START,
    AgentFileContent,
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
from meshagent.agents.images_dataset import ImageDatasetClient
from meshagent.api import Participant, RoomClient
from meshagent.api.http import new_client_session
from telethon import TelegramClient, events
from telethon.tl.types import User

__version__ = "local"


logger = logging.getLogger("meshagent.telegram_channel")

DEFAULT_THREAD_PREFIX = ".threads/telegram"
THREAD_PREFIX = os.getenv("MESHAGENT_TELEGRAM_THREAD_PREFIX", DEFAULT_THREAD_PREFIX)
QUEUE_NAME = os.getenv("MESHAGENT_TELEGRAM_QUEUE_NAME", "telegram-inbound")
MEDIA_STORAGE_PREFIX = os.getenv(
    "MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX",
    ".threads/telegram-media",
)
MAX_TELEGRAM_MESSAGE_CHARS = 3900
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 50_000_000
RESPONSE_TIMEOUT_SECONDS = float(
    os.getenv("MESHAGENT_TELEGRAM_RESPONSE_TIMEOUT", "300")
)
DEFAULT_TELEGRAM_HTTP_USER_AGENT = f"meshagent-telegram-example/{__version__}"
DEFAULT_TELEGRAM_BOT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_BLOCKED_HTTP_MEDIA_HOSTS = frozenset(
    {
        "wikimedia.org",
        "wikipedia.org",
    }
)
TEXT_AUTHORED_INLINE_MEDIA_TYPES = frozenset(
    {
        "image/svg+xml",
        "image/x-portable-bitmap",
        "image/x-portable-graymap",
        "image/x-portable-pixmap",
    }
)


class TelegramMediaTooLargeError(ValueError):
    def __init__(
        self,
        *,
        message_id: object,
        size_bytes: int,
        max_bytes: int,
        source: str,
    ) -> None:
        super().__init__(
            f"Telegram media {message_id} is {size_bytes} bytes, "
            f"which exceeds the configured inbound media limit of {max_bytes} bytes."
        )
        self.message_id = message_id
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        self.source = source


@dataclass(frozen=True, slots=True)
class _TelegramOutboundFile:
    file: str | bytes
    name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


@dataclass(frozen=True, slots=True)
class _TelegramWebhookFile:
    file_id: str
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    ext: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramWebhookInboundMessage:
    message_id: object
    chat_id: object
    sender_id: object | None
    text: str
    file: _TelegramWebhookFile | None = None
    reply_to: TelegramWebhookInboundMessage | None = None


class _TelegramFileInfo(Protocol):
    size: int | None
    mime_type: str | None
    name: str | None
    ext: str | None


class _TelegramReplyInfo(Protocol):
    reply_to_msg_id: object | None


class _TelegramEvent(Protocol):
    id: object
    raw_text: str
    text: str
    chat_id: object | None
    sender_id: object | None
    file: _TelegramFileInfo | None
    media: object | None
    reply_to_msg_id: object | None
    reply_to: _TelegramReplyInfo | None

    async def respond(self, text: str) -> None: ...

    async def download_media(self, *, file: object) -> bytes | None: ...

    async def get_reply_message(self) -> _TelegramEvent | None: ...


@dataclass(frozen=True, slots=True)
class _TelegramTurnResponse:
    text: str
    files: tuple[_TelegramOutboundFile, ...] = ()


@dataclass(slots=True)
class _PendingTelegramTurn:
    chat_id: object
    response: asyncio.Future[_TelegramTurnResponse]


@dataclass(slots=True)
class _ActiveTelegramTurn:
    chat_id: object
    response: asyncio.Future[_TelegramTurnResponse]
    text_parts: list[str] = field(default_factory=list)
    files: list[_TelegramOutboundFile] = field(default_factory=list)
    file_keys: set[str] = field(default_factory=set)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Telegram channel.")
    return value


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


def telegram_chunks(text: str) -> list[str]:
    normalized = text.strip()
    if normalized == "":
        return ["The room agent returned an empty response."]
    return [
        normalized[index : index + MAX_TELEGRAM_MESSAGE_CHARS]
        for index in range(0, len(normalized), MAX_TELEGRAM_MESSAGE_CHARS)
    ]


def _slug(value: object) -> str:
    raw = str(value)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
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


def is_telegram_fetchable_media_url(*, url: str) -> bool:
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
        name = "telegram-file"
    if "." not in PurePosixPath(name).name:
        extension = mimetypes.guess_extension(mime_type or "") or ""
        if extension != "":
            name = f"{name}{extension}"
    return name


def _telegram_http_download_headers() -> dict[str, str]:
    user_agent = os.getenv(
        "MESHAGENT_TELEGRAM_HTTP_USER_AGENT",
        DEFAULT_TELEGRAM_HTTP_USER_AGENT,
    ).strip()
    if user_agent == "":
        user_agent = DEFAULT_TELEGRAM_HTTP_USER_AGENT
    return {"User-Agent": user_agent}


def _host_matches_domain(*, hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _blocked_http_media_hosts() -> frozenset[str]:
    raw = os.getenv("MESHAGENT_TELEGRAM_HTTP_MEDIA_BLOCKED_HOSTS")
    if raw is None:
        return DEFAULT_BLOCKED_HTTP_MEDIA_HOSTS
    return frozenset(
        host.strip().lower() for host in raw.split(",") if host.strip() != ""
    )


def _is_blocked_http_media_url(*, url: str) -> bool:
    parsed = urlparse(url.strip())
    hostname = parsed.hostname
    if hostname is None:
        return False
    normalized_hostname = hostname.lower()
    return any(
        _host_matches_domain(hostname=normalized_hostname, domain=blocked_host)
        for blocked_host in _blocked_http_media_hosts()
    )


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


def _telegram_file_bytes_from_content_data(
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
    raise TypeError("Telegram file content data must be bytes or base64 text.")


def _telegram_file_for_data_url(*, url: str) -> _TelegramOutboundFile | None:
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
            "telegram_media_data_url_ignored_text_authored mime_type=%s",
            normalized_mime_type,
        )
        return None
    extension = mimetypes.guess_extension(mime_type or "") or ""
    return _TelegramOutboundFile(
        file=data,
        name=f"telegram-file{extension}" if extension != "" else "telegram-file",
        mime_type=mime_type,
        file_size=len(data),
    )


def _telegram_generated_image_name(*, mime_type: str | None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ".png"
    return f"generated-image{extension}"


def _telegram_media_source_key(*, kind: str, value: object) -> str | None:
    normalized = str(value).strip()
    if normalized == "":
        return None
    return f"{kind}:{normalized}"


def _telegram_outbound_file_content_key(media_file: _TelegramOutboundFile) -> str:
    if isinstance(media_file.file, bytes):
        digest = hashlib.sha256(media_file.file).hexdigest()
        return f"bytes:{len(media_file.file)}:{digest}:{media_file.mime_type or ''}"
    return f"url:{media_file.file.strip()}"


def _append_unique_telegram_file(
    *,
    active: _ActiveTelegramTurn,
    media_file: _TelegramOutboundFile,
    source_key: str | None = None,
) -> bool:
    content_key = _telegram_outbound_file_content_key(media_file)
    candidate_keys = [key for key in (source_key, content_key) if key is not None]
    if any(key in active.file_keys for key in candidate_keys):
        logger.info(
            "telegram_duplicate_media_skipped source_key=%s name=%s mime_type=%s",
            source_key,
            media_file.name,
            media_file.mime_type,
        )
        return False
    active.files.append(media_file)
    active.file_keys.update(candidate_keys)
    return True


def _allowed_chat_ids_from_value(
    value: str | Sequence[object] | None,
) -> frozenset[str] | None:
    if value is None:
        return None
    values = value.split(",") if isinstance(value, str) else value
    allowed_chat_ids = frozenset(
        normalized
        for raw_chat_id in values
        if (normalized := str(raw_chat_id).strip()) != ""
    )
    return allowed_chat_ids or None


def _queue_payload_from_message(message: dict[str, Any] | str | None) -> Any:
    payload = message
    if isinstance(payload, dict) and "body" in payload:
        body = payload.get("body")
        if isinstance(body, dict):
            return body
        if isinstance(body, str):
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise ValueError("Telegram webhook body was not valid JSON.") from exc
        raise ValueError("Telegram webhook queue body must be JSON text.")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Telegram webhook payload was not valid JSON.") from exc
    return payload


def _telegram_webhook_text(message_data: dict[str, Any]) -> str:
    for key in ("text", "caption"):
        value = message_data.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return ""


def _telegram_webhook_file(
    message_data: dict[str, Any],
    *,
    message_id: object,
) -> _TelegramWebhookFile | None:
    photos = message_data.get("photo")
    if isinstance(photos, list) and len(photos) > 0:
        photo_candidates = [
            photo
            for photo in photos
            if isinstance(photo, dict) and photo.get("file_id")
        ]
        if len(photo_candidates) > 0:
            photo = max(
                photo_candidates,
                key=lambda item: (
                    item.get("file_size")
                    if isinstance(item.get("file_size"), int)
                    else 0
                ),
            )
            return _TelegramWebhookFile(
                file_id=str(photo["file_id"]),
                name=f"telegram-{_slug(message_id)}.jpg",
                mime_type="image/jpeg",
                size=photo.get("file_size")
                if isinstance(photo.get("file_size"), int)
                else None,
                ext=".jpg",
            )

    media_specs = (
        ("document", "application/octet-stream", ".bin"),
        ("animation", "video/mp4", ".mp4"),
        ("video", "video/mp4", ".mp4"),
        ("audio", "audio/mpeg", ".mp3"),
        ("voice", "audio/ogg", ".ogg"),
        ("video_note", "video/mp4", ".mp4"),
        ("sticker", "image/webp", ".webp"),
    )
    for key, default_mime_type, default_ext in media_specs:
        value = message_data.get(key)
        if not isinstance(value, dict) or not value.get("file_id"):
            continue
        file_name = value.get("file_name")
        mime_type = value.get("mime_type")
        return _TelegramWebhookFile(
            file_id=str(value["file_id"]),
            name=str(file_name).strip() if isinstance(file_name, str) else None,
            mime_type=str(mime_type).strip()
            if isinstance(mime_type, str) and mime_type.strip() != ""
            else default_mime_type,
            size=value.get("file_size")
            if isinstance(value.get("file_size"), int)
            else None,
            ext=default_ext,
        )
    return None


def _telegram_inbound_message_from_payload(
    message_data: dict[str, Any],
) -> TelegramWebhookInboundMessage:
    if not isinstance(message_data, dict):
        raise ValueError("Telegram webhook message must be an object.")

    message_id = message_data.get("message_id", "unknown")
    chat = message_data.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        raise ValueError("Telegram webhook message was missing chat.id.")

    sender = message_data.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    reply_data = message_data.get("reply_to_message")
    reply_to = (
        _telegram_inbound_message_from_payload(reply_data)
        if isinstance(reply_data, dict)
        else None
    )
    return TelegramWebhookInboundMessage(
        message_id=message_id,
        chat_id=chat["id"],
        sender_id=sender_id,
        text=_telegram_webhook_text(message_data),
        file=_telegram_webhook_file(message_data, message_id=message_id),
        reply_to=reply_to,
    )


def parse_telegram_webhook_update(
    message: dict[str, Any] | str | None,
) -> TelegramWebhookInboundMessage:
    payload = _queue_payload_from_message(message)
    if not isinstance(payload, dict):
        raise ValueError("Telegram webhook payload must be an object.")

    message_data = payload.get("message") or payload.get("edited_message")
    if not isinstance(message_data, dict):
        raise ValueError("Telegram webhook update did not include a message.")
    return _telegram_inbound_message_from_payload(message_data)


class _TelegramWebhookEvent:
    def __init__(
        self,
        *,
        channel: TelegramWebhookChannel,
        message: TelegramWebhookInboundMessage,
    ) -> None:
        self._channel = channel
        self._message = message
        self.id = message.message_id
        self.raw_text = message.text
        self.text = message.text
        self.chat_id = message.chat_id
        self.sender_id = message.sender_id
        self.file = message.file
        self.media = message.file
        self.reply_to_msg_id = (
            message.reply_to.message_id if message.reply_to is not None else None
        )
        self.reply_to: _TelegramReplyInfo | None = None
        self._reply_message = (
            _TelegramWebhookEvent(channel=channel, message=message.reply_to)
            if message.reply_to is not None
            else None
        )

    async def respond(self, text: str) -> None:
        await self._channel._send_telegram_text(chat_id=self.chat_id, text=text)

    async def download_media(self, *, file: object) -> bytes | None:
        del file
        if self._message.file is None:
            return None
        return await self._channel._download_telegram_bot_file(
            file_id=self._message.file.file_id
        )

    async def get_reply_message(self) -> _TelegramWebhookEvent | None:
        return self._reply_message


class TelegramChannel(ThreadedChannel):
    def __init__(
        self,
        *,
        room: RoomClient,
        api_id: int,
        api_hash: str,
        bot_token: str,
        threading_mode: str | None = None,
        thread_dir: str | None = None,
        thread_url_scheme: str | None = None,
        thread_path_extension: str = ".thread",
        thread_list_path: str | None = None,
        llm_adapter: LLMAdapter | None = None,
        thread_prefix: str | None = None,
        media_storage_prefix: str = MEDIA_STORAGE_PREFIX,
        inbound_media_max_bytes: int = DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        allowed_chat_ids: str | Sequence[object] | None = None,
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
        self._thread_prefix = self._resolve_thread_prefix(
            thread_prefix=thread_prefix,
            thread_dir=thread_dir,
        )
        self._media_storage_prefix = (
            media_storage_prefix.strip().strip("/") or ".threads/telegram-media"
        )
        if inbound_media_max_bytes < 0:
            raise ValueError("inbound_media_max_bytes must be 0 or greater")
        self._inbound_media_max_bytes = inbound_media_max_bytes
        self._allowed_chat_ids = _allowed_chat_ids_from_value(allowed_chat_ids)
        self._bot_token = bot_token
        self._telegram = TelegramClient(
            None,
            api_id,
            api_hash,
        )
        self._telegram_task: asyncio.Task[Any] | None = None
        self._pending_turns_by_message_id: dict[str, _PendingTelegramTurn] = {}
        self._active_turns_by_turn_id: dict[str, _ActiveTelegramTurn] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}

    def _default_thread_dir_fallback_name(self) -> str:
        return "telegram"

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
        await self._telegram.start(bot_token=self._bot_token)

        self._telegram.add_event_handler(
            self._handle_telegram_message,
            events.NewMessage(incoming=True),
        )
        self._telegram_task = asyncio.create_task(
            self._telegram.run_until_disconnected()
        )
        self._telegram_task.add_done_callback(self._log_telegram_task_failure)

        me = await self._telegram.get_me()
        if not isinstance(me, User):
            raise RuntimeError("Telegram returned an unexpected current-user response.")
        username = me.username or str(me.id)
        logger.info("telegram_channel_connected username=%s", username)

    async def on_stop(self) -> None:
        self._telegram.remove_event_handler(self._handle_telegram_message)
        telegram_task = self._telegram_task
        self._telegram_task = None
        if telegram_task is not None:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
        await self._telegram.disconnect()
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

    async def on_message(self, message: Message) -> None:
        data = message.data
        if isinstance(data, TurnStarted):
            pending = self._pending_turns_by_message_id.pop(
                data.source_message_id,
                None,
            )
            if pending is None:
                return
            self._active_turns_by_turn_id[data.turn_id] = _ActiveTelegramTurn(
                chat_id=pending.chat_id,
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
            pending.response.set_result(_TelegramTurnResponse(text=data.error.message))
            return

        if isinstance(data, AgentTextContentDelta):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is None:
                return
            active.text_parts.append(data.text)
            return

        if isinstance(data, AgentFileContentDelta):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is None:
                return
            source_key = _telegram_media_source_key(kind="file-url", value=data.url)
            if source_key is not None and source_key in active.file_keys:
                logger.info(
                    "telegram_duplicate_media_skipped source_key=%s",
                    source_key,
                )
                return
            media = await self._telegram_file_for_agent_file_url(url=data.url)
            if media is not None:
                _append_unique_telegram_file(
                    active=active,
                    media_file=media,
                    source_key=source_key,
                )
            return

        if isinstance(data, AgentImageGenerationCompleted):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is None:
                return
            for image in data.images:
                source_key = _telegram_media_source_key(
                    kind="generated-image-uri",
                    value=image.uri or "",
                )
                if source_key is not None and source_key in active.file_keys:
                    logger.info(
                        "telegram_duplicate_media_skipped source_key=%s",
                        source_key,
                    )
                    continue
                media = await self._telegram_file_for_generated_image(image=image)
                if media is not None:
                    _append_unique_telegram_file(
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
        if data.error is not None:
            active.response.set_result(_TelegramTurnResponse(text=data.error.message))
            return
        active.response.set_result(
            _TelegramTurnResponse(
                text="".join(active.text_parts).strip(),
                files=tuple(active.files),
            )
        )

    async def _handle_telegram_message(self, event: _TelegramEvent) -> None:
        text = (event.raw_text or "").strip()
        has_media = self._event_has_media(event)
        if text == "" and not has_media:
            return

        chat_id = event.chat_id or event.sender_id or "unknown"
        message_id = event.id
        if not self._is_chat_id_allowed(chat_id):
            logger.warning(
                "telegram_message_denied reason=not_allowlisted chat_id=%s sender_id=%s message_id=%s has_media=%s allowed_chat_ids_hint=MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS=%s",
                chat_id,
                event.sender_id,
                message_id,
                has_media,
                chat_id,
            )
            return
        logger.info(
            "telegram_message_allowed chat_id=%s sender_id=%s message_id=%s has_media=%s",
            chat_id,
            event.sender_id,
            message_id,
            has_media,
        )

        lock = self._chat_locks.setdefault(str(chat_id), asyncio.Lock())
        async with lock:
            async with self._telegram.action(chat_id, "typing"):
                response = await self._send_telegram_turn(event=event, text=text)
                await self._send_telegram_response(event=event, response=response)

    async def _send_telegram_turn(
        self,
        *,
        event: _TelegramEvent,
        text: str,
    ) -> _TelegramTurnResponse:
        chat_id = event.chat_id or event.sender_id or "unknown"
        participant_name = f"telegram-chat-{chat_id}"
        participant_key = event.sender_id or uuid.uuid5(
            uuid.NAMESPACE_URL, str(chat_id)
        )
        participant = Participant(
            id=f"telegram:{participant_key}",
            attributes={"name": participant_name, "role": "user"},
        )
        thread_id = self._thread_id_for_chat(chat_id)
        self.bump_thread(path=thread_id, name=participant_name)

        response: asyncio.Future[_TelegramTurnResponse] = (
            asyncio.get_running_loop().create_future()
        )
        content = await self._turn_content_for_telegram_event(event=event, text=text)
        if len(content) == 0:
            return _TelegramTurnResponse(
                text="The Telegram message did not include text or downloadable media."
            )
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=thread_id,
            content=content,
            sender_name=participant_name,
        )
        self._pending_turns_by_message_id[turn_start.message_id] = _PendingTelegramTurn(
            chat_id=chat_id, response=response
        )
        self.emit(sender=participant, payload=turn_start)

        try:
            return await asyncio.wait_for(response, timeout=RESPONSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._clear_pending_response(response=response)
            logger.warning("telegram_turn_response_timed_out chat_id=%s", chat_id)
            return _TelegramTurnResponse(
                text=(
                    "The room agent did not answer before the Telegram channel timed out."
                )
            )
        except Exception:
            self._clear_pending_response(response=response)
            logger.exception("telegram_turn_failed chat_id=%s", chat_id)
            return _TelegramTurnResponse(
                text="The room agent could not answer that message."
            )

    async def _send_telegram_response(
        self,
        *,
        event: _TelegramEvent,
        response: _TelegramTurnResponse,
    ) -> None:
        text_chunks = (
            telegram_chunks(response.text)
            if response.text.strip() != "" or len(response.files) == 0
            else []
        )
        for chunk in text_chunks:
            await event.respond(chunk)
        for media_file in response.files:
            await self._send_telegram_file(event=event, media_file=media_file)

    async def _send_telegram_file(
        self,
        *,
        event: _TelegramEvent,
        media_file: _TelegramOutboundFile,
    ) -> None:
        chat_id = event.chat_id or event.sender_id or "unknown"
        file_arg: str | io.BytesIO
        if isinstance(media_file.file, bytes):
            file_buffer = io.BytesIO(media_file.file)
            file_buffer.name = media_file.name or "telegram-file"
            file_arg = file_buffer
        else:
            file_arg = media_file.file
        await self._telegram.send_file(
            chat_id,
            file_arg,
            mime_type=media_file.mime_type,
            file_size=media_file.file_size,
        )

    async def _download_telegram_http_file_url(
        self,
        *,
        url: str,
    ) -> _TelegramOutboundFile | None:
        try:
            async with new_client_session() as http_session:
                async with http_session.get(
                    url,
                    headers=_telegram_http_download_headers(),
                ) as response:
                    if response.status >= 400:
                        response_text = await response.text()
                        logger.error(
                            "telegram_media_http_download_failed status=%s url=%s body=%s",
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
                        self._raise_if_media_too_large(
                            message_id=url,
                            size_bytes=int(content_length),
                            max_bytes=self._inbound_media_max_bytes,
                            source="metadata",
                        )
                    data = await response.read()
        except TelegramMediaTooLargeError:
            raise
        except Exception:
            logger.exception("telegram_media_http_download_failed url=%s", url)
            return None

        self._raise_if_media_too_large(
            message_id=url,
            size_bytes=len(data),
            max_bytes=self._inbound_media_max_bytes,
            source="download",
        )
        return _TelegramOutboundFile(
            file=data,
            name=_file_name_from_http_url(url=url, mime_type=mime_type),
            mime_type=mime_type,
            file_size=len(data),
        )

    async def _telegram_file_for_agent_file_url(
        self,
        *,
        url: str,
    ) -> _TelegramOutboundFile | None:
        normalized_url = url.strip()
        if is_telegram_fetchable_media_url(url=normalized_url):
            if _is_blocked_http_media_url(url=normalized_url):
                logger.warning(
                    "telegram_media_http_download_blocked_host url=%s",
                    normalized_url,
                )
                return None
            try:
                return await self._download_telegram_http_file_url(url=normalized_url)
            except TelegramMediaTooLargeError as exc:
                logger.warning(
                    "telegram_media_http_download_skipped_oversize url=%s "
                    "size_bytes=%s max_bytes=%s source=%s",
                    normalized_url,
                    exc.size_bytes,
                    exc.max_bytes,
                    exc.source,
                )
                return None
        data_url_file = _telegram_file_for_data_url(url=normalized_url)
        if data_url_file is not None:
            return data_url_file
        if normalized_url.lower().startswith("data:"):
            return None

        storage_path = room_storage_path_from_agent_file_url(url=normalized_url)
        if storage_path is None:
            logger.warning("telegram_media_url_ignored url=%s", normalized_url)
            return None

        try:
            content = await self._room.storage.download(path=storage_path)
        except Exception:
            logger.exception(
                "telegram_media_download_failed path=%s",
                storage_path,
            )
            return None

        try:
            file_data = _telegram_file_bytes_from_content_data(
                data=content.data,
                mime_type=content.mime_type,
            )
        except Exception:
            logger.exception(
                "telegram_media_content_decode_failed path=%s",
                storage_path,
            )
            return None

        return _TelegramOutboundFile(
            file=file_data,
            name=content.name or PurePosixPath(storage_path).name,
            mime_type=content.mime_type,
            file_size=len(file_data),
        )

    async def _telegram_file_for_generated_image(
        self,
        *,
        image: AgentGeneratedImage,
    ) -> _TelegramOutboundFile | None:
        uri = (image.uri or "").strip()
        if uri == "":
            return None

        data_url_file = _telegram_file_for_data_url(url=uri)
        if data_url_file is not None:
            return _TelegramOutboundFile(
                file=data_url_file.file,
                name=_telegram_generated_image_name(
                    mime_type=data_url_file.mime_type or image.mime_type,
                ),
                mime_type=data_url_file.mime_type or image.mime_type,
                file_size=data_url_file.file_size,
            )

        try:
            record = await ImageDatasetClient(self._room.datasets).read_record_from_uri(
                uri,
                fallback_mime_type=image.mime_type,
            )
        except Exception:
            logger.exception("telegram_generated_image_download_failed uri=%s", uri)
            return None

        if record is None:
            logger.warning("telegram_generated_image_ignored uri=%s", uri)
            return None

        return _TelegramOutboundFile(
            file=record.data,
            name=_telegram_generated_image_name(mime_type=record.mime_type),
            mime_type=record.mime_type,
            file_size=len(record.data),
        )

    async def _turn_content_for_telegram_event(
        self,
        *,
        event: _TelegramEvent,
        text: str,
    ) -> list[AgentTextContent | AgentFileContent]:
        content: list[AgentTextContent | AgentFileContent] = []
        if text.strip() != "":
            content.append(AgentTextContent(type="text", text=text.strip()))
        if self._event_has_media(event):
            media_content = await self._agent_content_for_inbound_media(event=event)
            if media_content is not None:
                content.append(media_content)
        content.extend(await self._agent_content_for_replied_message(event=event))
        return content

    async def _agent_content_for_replied_message(
        self,
        *,
        event: _TelegramEvent,
    ) -> list[AgentFileContent | AgentTextContent]:
        reply_message_id = self._event_reply_message_id(event)
        if reply_message_id is None:
            return []

        reply_message = await self._get_replied_telegram_message(
            event=event,
            reply_message_id=reply_message_id,
        )
        if reply_message is None:
            return []

        content: list[AgentFileContent | AgentTextContent] = []
        reply_text = self._event_text(reply_message)
        if reply_text != "":
            content.append(
                AgentTextContent(
                    type="text",
                    text=f"Replied-to Telegram message:\n{reply_text}",
                )
            )

        if self._event_has_media(reply_message):
            replied_media_content = await self._agent_content_for_inbound_media(
                event=reply_message,
                storage_event=event,
                source="reply",
            )
            if replied_media_content is not None:
                content.append(replied_media_content)

        return content

    async def _get_replied_telegram_message(
        self,
        *,
        event: _TelegramEvent,
        reply_message_id: object,
    ) -> Any | None:
        try:
            return await event.get_reply_message()
        except Exception:
            logger.exception(
                "telegram_reply_message_fetch_failed chat_id=%s reply_message_id=%s",
                event.chat_id or event.sender_id or "unknown",
                reply_message_id,
            )
            return None

    async def _agent_content_for_inbound_media(
        self,
        *,
        event: _TelegramEvent,
        storage_event: _TelegramEvent | None = None,
        source: str = "message",
    ) -> AgentFileContent | AgentTextContent | None:
        message_id = event.id or "unknown"
        file_info = event.file
        file_size = self._event_file_size(file_info)
        mime_type = self._event_file_mime_type(file_info)
        name = self._event_file_name(
            file_info=file_info,
            message_id=message_id,
            mime_type=mime_type,
        )
        path = self._inbound_media_storage_path(
            event=storage_event or event,
            message_id=message_id,
            filename=name,
        )
        if await self._room_storage_exists(path=path):
            logger.info(
                "telegram_inbound_media_reused source=%s message_id=%s path=%s",
                source,
                message_id,
                path,
            )
            return AgentFileContent(type="file", url=f"room:///{path}", name=name)

        try:
            self._raise_if_media_too_large(
                message_id=message_id,
                size_bytes=file_size,
                max_bytes=self._inbound_media_max_bytes,
                source="metadata",
            )
            data = await event.download_media(file=bytes)
            if data is None:
                return AgentTextContent(
                    type="text",
                    text="Telegram media could not be downloaded.",
                )
            if not isinstance(data, (bytes, bytearray)):
                raise RuntimeError("Telegram media download did not return bytes.")
            downloaded_data = bytes(data)
            self._raise_if_media_too_large(
                message_id=message_id,
                size_bytes=len(downloaded_data),
                max_bytes=self._inbound_media_max_bytes,
                source="download",
            )
        except TelegramMediaTooLargeError as exc:
            logger.warning(
                "telegram_inbound_media_skipped_oversize message_id=%s "
                "size_bytes=%s max_bytes=%s source=%s",
                exc.message_id,
                exc.size_bytes,
                exc.max_bytes,
                exc.source,
            )
            return AgentTextContent(
                type="text",
                text=(
                    f"Telegram attachment {exc.message_id} was not attached "
                    f"because it is {exc.size_bytes} bytes, which exceeds the "
                    f"configured inbound media limit of {exc.max_bytes} bytes."
                ),
            )
        except Exception:
            logger.exception(
                "telegram_inbound_media_download_failed message_id=%s",
                message_id,
            )
            return AgentTextContent(
                type="text",
                text="Telegram media could not be downloaded.",
            )

        try:
            await self._room.storage.upload(
                path=path,
                data=downloaded_data,
                overwrite=True,
                name=name,
                mime_type=mime_type,
            )
        except Exception:
            logger.exception(
                "telegram_inbound_media_upload_failed message_id=%s path=%s",
                message_id,
                path,
            )
            return AgentTextContent(
                type="text",
                text="Telegram media could not be uploaded to room storage.",
            )
        return AgentFileContent(type="file", url=f"room:///{path}", name=name)

    async def _room_storage_exists(self, *, path: str) -> bool:
        try:
            return bool(await self._room.storage.exists(path=path))
        except Exception:
            logger.exception("telegram_media_storage_exists_failed path=%s", path)
            return False

    def _thread_id_for_chat(self, chat_id: object) -> str:
        uses_dataset_thread_url = (
            self._thread_url_scheme == "dataset://"
            or self._thread_prefix.startswith("dataset://")
        )
        extension = "" if uses_dataset_thread_url else self._thread_path_extension
        path = f"{self._thread_prefix}/{_slug(chat_id)}{extension}"
        if "://" in path:
            return path
        return self._thread_url_for_path(path=path)

    @staticmethod
    def _resolve_thread_prefix(
        *,
        thread_prefix: str | None,
        thread_dir: str | None,
    ) -> str:
        resolved_prefix = thread_prefix
        if resolved_prefix is None:
            resolved_prefix = os.getenv("MESHAGENT_TELEGRAM_THREAD_PREFIX")
        if resolved_prefix is None or resolved_prefix.strip() == "":
            resolved_prefix = thread_dir
        if resolved_prefix is None or resolved_prefix.strip() == "":
            resolved_prefix = DEFAULT_THREAD_PREFIX
        return resolved_prefix.strip().rstrip("/") or DEFAULT_THREAD_PREFIX

    def _is_chat_id_allowed(self, chat_id: object | None) -> bool:
        if self._allowed_chat_ids is None:
            return True
        if chat_id is None:
            return False
        return str(chat_id).strip() in self._allowed_chat_ids

    def _inbound_media_storage_path(
        self,
        *,
        event: _TelegramEvent,
        message_id: object,
        filename: str,
    ) -> str:
        chat_id = event.chat_id or event.sender_id or "unknown"
        return (
            f"{self._media_storage_prefix}/"
            f"{_slug(chat_id)}/"
            f"{_slug(message_id)}-{_slug(filename)}"
        )

    @staticmethod
    def _event_has_media(event: _TelegramEvent) -> bool:
        return event.media is not None or event.file is not None

    @staticmethod
    def _event_text(event: _TelegramEvent) -> str:
        for value in (event.raw_text, event.text):
            if isinstance(value, str) and value.strip() != "":
                return value.strip()
        return ""

    @staticmethod
    def _event_reply_message_id(event: _TelegramEvent) -> object | None:
        reply_message_id = event.reply_to_msg_id
        if reply_message_id is not None:
            return reply_message_id
        return event.reply_to.reply_to_msg_id if event.reply_to is not None else None

    @staticmethod
    def _event_file_size(file_info: _TelegramFileInfo | None) -> int | None:
        size = file_info.size if file_info is not None else None
        return size if isinstance(size, int) and size >= 0 else None

    @staticmethod
    def _event_file_mime_type(file_info: _TelegramFileInfo | None) -> str:
        mime_type = file_info.mime_type if file_info is not None else None
        if isinstance(mime_type, str) and mime_type.strip() != "":
            return mime_type.strip()
        name = file_info.name if file_info is not None else None
        if isinstance(name, str) and name.strip() != "":
            guessed = mimetypes.guess_type(name.strip())[0]
            if guessed is not None:
                return guessed
        return "application/octet-stream"

    @staticmethod
    def _event_file_name(
        *,
        file_info: _TelegramFileInfo | None,
        message_id: object,
        mime_type: str,
    ) -> str:
        name = file_info.name if file_info is not None else None
        if isinstance(name, str) and name.strip() != "":
            return PurePosixPath(name.strip()).name
        extension = file_info.ext if file_info is not None else None
        if not isinstance(extension, str) or extension.strip() == "":
            extension = mimetypes.guess_extension(mime_type) or ".bin"
        if not extension.startswith("."):
            extension = f".{extension}"
        return f"telegram-{_slug(message_id)}{extension}"

    @staticmethod
    def _raise_if_media_too_large(
        *,
        message_id: object,
        size_bytes: int | None,
        max_bytes: int | None,
        source: str,
    ) -> None:
        if max_bytes is None or size_bytes is None:
            return
        if size_bytes > max_bytes:
            raise TelegramMediaTooLargeError(
                message_id=message_id,
                size_bytes=size_bytes,
                max_bytes=max_bytes,
                source=source,
            )

    def _clear_thread_state(self, *, thread_id: str) -> None:
        pending_message_ids = [
            message_id
            for message_id, pending in self._pending_turns_by_message_id.items()
            if self._thread_id_for_chat(pending.chat_id) == thread_id
        ]
        for message_id in pending_message_ids:
            pending = self._pending_turns_by_message_id.pop(message_id)
            if not pending.response.done():
                pending.response.cancel()

        active_turn_ids = [
            turn_id
            for turn_id, active in self._active_turns_by_turn_id.items()
            if self._thread_id_for_chat(active.chat_id) == thread_id
        ]
        for turn_id in active_turn_ids:
            active = self._active_turns_by_turn_id.pop(turn_id)
            if not active.response.done():
                active.response.cancel()

    def _clear_pending_response(
        self,
        *,
        response: asyncio.Future[_TelegramTurnResponse],
    ) -> None:
        for message_id, pending in list(self._pending_turns_by_message_id.items()):
            if pending.response is response:
                self._pending_turns_by_message_id.pop(message_id, None)
        for turn_id, active in list(self._active_turns_by_turn_id.items()):
            if active.response is response:
                self._active_turns_by_turn_id.pop(turn_id, None)

    @staticmethod
    def _log_telegram_task_failure(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("telegram_channel_disconnected", exc_info=exc)


class TelegramWebhookChannel(TelegramChannel):
    def __init__(
        self,
        *,
        room: RoomClient,
        bot_token: str,
        queue_name: str = QUEUE_NAME,
        bot_api_base_url: str | None = None,
        threading_mode: str | None = None,
        thread_dir: str | None = None,
        thread_url_scheme: str | None = None,
        thread_path_extension: str = ".thread",
        thread_list_path: str | None = None,
        llm_adapter: LLMAdapter | None = None,
        thread_prefix: str | None = None,
        media_storage_prefix: str = MEDIA_STORAGE_PREFIX,
        inbound_media_max_bytes: int = DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        allowed_chat_ids: str | Sequence[object] | None = None,
        receive_from_http: bool = False,
    ) -> None:
        super().__init__(
            room=room,
            api_id=1,
            api_hash="0" * 32,
            bot_token=bot_token,
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_url_scheme=thread_url_scheme,
            thread_path_extension=thread_path_extension,
            thread_list_path=thread_list_path,
            llm_adapter=llm_adapter,
            thread_prefix=thread_prefix,
            media_storage_prefix=media_storage_prefix,
            inbound_media_max_bytes=inbound_media_max_bytes,
            allowed_chat_ids=allowed_chat_ids,
        )
        self._queue_name = queue_name.strip() or QUEUE_NAME
        self._receive_from_http = receive_from_http
        self._bot_api_base_url = (
            bot_api_base_url
            or os.getenv(
                "MESHAGENT_TELEGRAM_BOT_API_BASE_URL",
                DEFAULT_TELEGRAM_BOT_API_BASE_URL,
            )
        ).rstrip("/")
        self._queue_task: asyncio.Task[Any] | None = None
        self._liveness_runner: web.AppRunner | None = None
        self._liveness_site: web.TCPSite | None = None

    async def on_start(self) -> None:
        await self.publish_thread_attributes()
        await self.open_thread_list_document()
        if not self._receive_from_http:
            await self._room.queues.open(name=self._queue_name)
            await self._start_liveness_server()
            self._queue_task = asyncio.create_task(self._receive_loop())
            self._queue_task.add_done_callback(self._log_queue_task_failure)
        logger.info("telegram_webhook_channel_started transport=%s", self.transport)

    @property
    def transport(self) -> str:
        return "http" if self._receive_from_http else f"queue:{self._queue_name}"

    async def process_webhook(self, body: str) -> None:
        await self._process_queue_message(body)

    async def on_stop(self) -> None:
        queue_task = self._queue_task
        self._queue_task = None
        if queue_task is not None:
            queue_task.cancel()
            await asyncio.gather(queue_task, return_exceptions=True)
        await self._stop_liveness_server()
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

    async def _receive_loop(self) -> None:
        while True:
            try:
                queued_message = await self._room.queues.receive(
                    name=self._queue_name,
                    create=True,
                    wait=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "telegram_webhook_queue_receive_failed queue=%s",
                    self._queue_name,
                )
                await asyncio.sleep(1)
                continue

            try:
                await self._process_queue_message(queued_message)
            except Exception:
                logger.exception("telegram_webhook_queue_message_failed")

    async def _process_queue_message(
        self, queued_message: dict[str, Any] | str | None
    ) -> None:
        try:
            inbound_message = parse_telegram_webhook_update(queued_message)
        except ValueError as exc:
            logger.warning("telegram_webhook_queue_message_ignored error=%s", exc)
            return

        event = _TelegramWebhookEvent(channel=self, message=inbound_message)
        text = event.raw_text.strip()
        has_media = self._event_has_media(event)
        if text == "" and not has_media:
            return

        chat_id = event.chat_id or event.sender_id or "unknown"
        if not self._is_chat_id_allowed(chat_id):
            logger.warning(
                "telegram_message_denied reason=not_allowlisted chat_id=%s "
                "sender_id=%s message_id=%s has_media=%s "
                "allowed_chat_ids_hint=MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS=%s",
                chat_id,
                event.sender_id,
                event.id,
                has_media,
                chat_id,
            )
            return
        logger.info(
            "telegram_message_allowed chat_id=%s sender_id=%s message_id=%s has_media=%s",
            chat_id,
            event.sender_id,
            event.id,
            has_media,
        )

        lock = self._chat_locks.setdefault(str(chat_id), asyncio.Lock())
        async with lock:
            await self._send_telegram_chat_action(chat_id=chat_id, action="typing")
            stop_action = asyncio.Event()
            action_task = asyncio.create_task(
                self._send_chat_action_until_stopped(
                    chat_id=chat_id,
                    action="typing",
                    stop=stop_action,
                )
            )
            try:
                response = await self._send_telegram_turn(event=event, text=text)
                await self._send_telegram_response(event=event, response=response)
            finally:
                stop_action.set()
                await asyncio.gather(action_task, return_exceptions=True)

    async def _send_chat_action_until_stopped(
        self,
        *,
        chat_id: object,
        action: str,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            await self._send_telegram_chat_action(chat_id=chat_id, action=action)

    async def _send_telegram_chat_action(self, *, chat_id: object, action: str) -> None:
        try:
            await self._send_telegram_json(
                "sendChatAction",
                {"chat_id": chat_id, "action": action},
            )
        except Exception:
            logger.debug(
                "telegram_chat_action_failed chat_id=%s action=%s",
                chat_id,
                action,
                exc_info=True,
            )

    async def _send_telegram_text(self, *, chat_id: object, text: str) -> None:
        await self._send_telegram_json(
            "sendMessage", {"chat_id": chat_id, "text": text}
        )

    async def _send_telegram_file(
        self,
        *,
        event: _TelegramEvent,
        media_file: _TelegramOutboundFile,
    ) -> None:
        chat_id = event.chat_id or event.sender_id or "unknown"
        is_photo = (media_file.mime_type or "").lower().startswith("image/")
        method = "sendPhoto" if is_photo else "sendDocument"
        field_name = "photo" if is_photo else "document"

        if isinstance(media_file.file, bytes):
            form = FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field(
                field_name,
                media_file.file,
                filename=media_file.name or "telegram-file",
                content_type=media_file.mime_type or "application/octet-stream",
            )
            await self._send_telegram_form(method, form)
            return

        await self._send_telegram_json(
            method,
            {"chat_id": chat_id, field_name: media_file.file},
        )

    async def _send_telegram_json(self, method: str, payload: dict[str, object]) -> Any:
        url = self._telegram_bot_api_url(method=method)
        async with new_client_session() as http_session:
            async with http_session.post(url, json=payload) as response:
                return await self._read_telegram_api_response(
                    response=response,
                    method=method,
                )

    async def _send_telegram_form(self, method: str, form: FormData) -> Any:
        url = self._telegram_bot_api_url(method=method)
        async with new_client_session() as http_session:
            async with http_session.post(url, data=form) as response:
                return await self._read_telegram_api_response(
                    response=response,
                    method=method,
                )

    async def _read_telegram_api_response(self, *, response: Any, method: str) -> Any:
        response_text = await response.text()
        if response.status >= 400:
            logger.error(
                "telegram_bot_api_request_failed method=%s status=%s body=%s",
                method,
                response.status,
                response_text[:500],
            )
            response.raise_for_status()

        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Telegram Bot API {method} returned invalid JSON."
            ) from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram Bot API {method} returned ok=false.")
        return payload.get("result")

    async def _download_telegram_bot_file(self, *, file_id: str) -> bytes | None:
        try:
            result = await self._send_telegram_json("getFile", {"file_id": file_id})
            if not isinstance(result, dict):
                raise RuntimeError("Telegram getFile returned an unexpected response.")
            file_path = result.get("file_path")
            if not isinstance(file_path, str) or file_path.strip() == "":
                raise RuntimeError("Telegram getFile response was missing file_path.")
            file_size = result.get("file_size")
            self._raise_if_media_too_large(
                message_id=file_id,
                size_bytes=file_size if isinstance(file_size, int) else None,
                max_bytes=self._inbound_media_max_bytes,
                source="metadata",
            )
            download_url = (
                f"{self._bot_api_base_url}/file/bot{self._bot_token}/{file_path}"
            )
            async with new_client_session() as http_session:
                async with http_session.get(
                    download_url,
                    headers=_telegram_http_download_headers(),
                ) as response:
                    if response.status >= 400:
                        response_text = await response.text()
                        logger.error(
                            "telegram_bot_file_download_failed status=%s file_id=%s body=%s",
                            response.status,
                            file_id,
                            response_text[:500],
                        )
                        response.raise_for_status()
                    data = await response.read()
        except TelegramMediaTooLargeError:
            raise
        except Exception:
            logger.exception("telegram_bot_file_download_failed file_id=%s", file_id)
            return None

        self._raise_if_media_too_large(
            message_id=file_id,
            size_bytes=len(data),
            max_bytes=self._inbound_media_max_bytes,
            source="download",
        )
        return data

    def _telegram_bot_api_url(self, *, method: str) -> str:
        return f"{self._bot_api_base_url}/bot{self._bot_token}/{method}"

    async def _start_liveness_server(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle_liveness_request)
        app.router.add_get("/health", self._handle_liveness_request)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        host = os.getenv("MESHAGENT_HOST", "0.0.0.0")
        port = env_int("MESHAGENT_PORT", default=8000)
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._liveness_runner = runner
        self._liveness_site = site
        logger.info("telegram_webhook_liveness_started host=%s port=%s", host, port)

    async def _stop_liveness_server(self) -> None:
        site = self._liveness_site
        runner = self._liveness_runner
        self._liveness_site = None
        self._liveness_runner = None
        if site is not None:
            await site.stop()
        if runner is not None:
            await runner.cleanup()

    async def _handle_liveness_request(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({"ok": True})

    @staticmethod
    def _log_queue_task_failure(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("telegram_webhook_queue_disconnected", exc_info=exc)


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
) -> TelegramChannel | TelegramWebhookChannel:
    mode = os.getenv("MESHAGENT_TELEGRAM_MODE", "telethon").strip().lower()
    if mode in {"webhook", "queue"}:
        return TelegramWebhookChannel(
            room=room,
            bot_token=required_env("TELEGRAM_BOT_TOKEN"),
            queue_name=os.getenv("MESHAGENT_TELEGRAM_QUEUE_NAME", QUEUE_NAME),
            threading_mode=threading_mode,
            thread_dir=thread_dir,
            thread_url_scheme=thread_url_scheme,
            thread_path_extension=thread_path_extension,
            thread_list_path=thread_list_path,
            llm_adapter=llm_adapter,
            media_storage_prefix=os.getenv(
                "MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX",
                MEDIA_STORAGE_PREFIX,
            ),
            inbound_media_max_bytes=env_int(
                "MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES",
                default=DEFAULT_INBOUND_MEDIA_MAX_BYTES,
            ),
            allowed_chat_ids=os.getenv("MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS"),
            receive_from_http=receive_from_http,
        )

    return TelegramChannel(
        room=room,
        api_id=int(required_env("TELEGRAM_API_ID")),
        api_hash=required_env("TELEGRAM_API_HASH"),
        bot_token=required_env("TELEGRAM_BOT_TOKEN"),
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_url_scheme=thread_url_scheme,
        thread_path_extension=thread_path_extension,
        thread_list_path=thread_list_path,
        llm_adapter=llm_adapter,
        media_storage_prefix=os.getenv(
            "MESHAGENT_TELEGRAM_MEDIA_STORAGE_PREFIX",
            MEDIA_STORAGE_PREFIX,
        ),
        inbound_media_max_bytes=env_int(
            "MESHAGENT_TELEGRAM_INBOUND_MEDIA_MAX_BYTES",
            default=DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        ),
        allowed_chat_ids=os.getenv("MESHAGENT_TELEGRAM_ALLOWED_CHAT_IDS"),
    )
