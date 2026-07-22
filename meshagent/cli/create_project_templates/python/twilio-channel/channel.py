from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import mimetypes
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.parse import unquote_to_bytes

from aiohttp import BasicAuth, ClientResponse, ClientSession
from meshagent.agents.adapter import LLMAdapter
from meshagent.agents.images_dataset import ImageDatasetClient
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
from meshagent.api import Participant, RoomClient
from meshagent.api.http import new_client_session


logger = logging.getLogger("meshagent.twilio_channel")

QUEUE_NAME = os.getenv("MESHAGENT_TWILIO_QUEUE_NAME", "twilio-inbound")
THREAD_PREFIX = os.getenv("MESHAGENT_TWILIO_THREAD_PREFIX", ".threads/twilio")
MEDIA_STORAGE_PREFIX = os.getenv(
    "MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX",
    ".threads/twilio-media",
)
MAX_TWILIO_MESSAGE_CHARS = 1500
MAX_TWILIO_MEDIA_URLS_PER_MESSAGE = 10
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 25_000_000
RESPONSE_TIMEOUT_SECONDS = float(os.getenv("MESHAGENT_TWILIO_RESPONSE_TIMEOUT", "300"))
TWILIO_API_BASE_URL = os.getenv(
    "MESHAGENT_TWILIO_API_BASE_URL",
    "https://api.twilio.com/2010-04-01",
).rstrip("/")
TEXT_AUTHORED_INLINE_MEDIA_TYPES = frozenset(
    {
        "image/svg+xml",
        "image/x-portable-bitmap",
        "image/x-portable-graymap",
        "image/x-portable-pixmap",
    }
)


class TwilioMediaTooLargeError(ValueError):
    def __init__(
        self,
        *,
        media_url: str,
        size_bytes: int,
        max_bytes: int,
        source: str,
    ) -> None:
        super().__init__(
            f"Twilio media {media_url} is {size_bytes} bytes, "
            f"which exceeds the configured inbound media limit of {max_bytes} bytes."
        )
        self.media_url = media_url
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        self.source = source


@dataclass(frozen=True, slots=True)
class TwilioInboundMedia:
    url: str
    content_type: str | None = None
    index: int = 0


@dataclass(frozen=True, slots=True)
class TwilioInboundMessage:
    message_sid: str
    from_number: str
    to_number: str
    body: str
    sender_name: str
    media: tuple[TwilioInboundMedia, ...] = ()


@dataclass(frozen=True, slots=True)
class _TwilioDownloadedMedia:
    data: bytes
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class _TwilioTurnResponse:
    text: str
    media_urls: tuple[str, ...] = ()


@dataclass(slots=True)
class _PendingTwilioTurn:
    message: TwilioInboundMessage
    response: asyncio.Future[_TwilioTurnResponse]


@dataclass(slots=True)
class _ActiveTwilioTurn:
    message: TwilioInboundMessage
    response: asyncio.Future[_TwilioTurnResponse]
    text_parts: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    media_keys: set[str] = field(default_factory=set)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Twilio channel.")
    return value


def env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
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


def twilio_chunks(text: str) -> list[str]:
    normalized = text.strip()
    if normalized == "":
        return ["The room agent returned an empty response."]
    return [
        normalized[index : index + MAX_TWILIO_MESSAGE_CHARS]
        for index in range(0, len(normalized), MAX_TWILIO_MESSAGE_CHARS)
    ]


def twilio_media_batches(media_urls: Sequence[str]) -> list[tuple[str, ...]]:
    return [
        tuple(media_urls[index : index + MAX_TWILIO_MEDIA_URLS_PER_MESSAGE])
        for index in range(0, len(media_urls), MAX_TWILIO_MEDIA_URLS_PER_MESSAGE)
    ]


def twilio_message_form_data(
    *,
    from_number: str,
    to_number: str,
    body: str,
    media_urls: Sequence[str] = (),
) -> list[tuple[str, str]]:
    normalized_body = body.strip()
    if normalized_body == "" and len(media_urls) == 0:
        raise ValueError("Twilio messages require body or media_urls.")
    if len(media_urls) > MAX_TWILIO_MEDIA_URLS_PER_MESSAGE:
        raise ValueError(
            "Twilio messages support at most "
            f"{MAX_TWILIO_MEDIA_URLS_PER_MESSAGE} media URLs."
        )

    data = [("From", from_number), ("To", to_number)]
    if normalized_body != "":
        data.append(("Body", normalized_body))
    data.extend(("MediaUrl", media_url) for media_url in media_urls)
    return data


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


def is_twilio_fetchable_media_url(*, url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and parsed.netloc != ""


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


def _data_url_is_text_authored(*, url: str, mime_type: str | None) -> bool:
    normalized_mime_type = (mime_type or "").strip().lower()
    return (
        not _data_url_is_base64(url=url)
        and normalized_mime_type in TEXT_AUTHORED_INLINE_MEDIA_TYPES
    )


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


def _twilio_decoded_file_content_data(
    *,
    data: object,
    mime_type: str | None,
) -> tuple[bytes, str | None] | None:
    decoded = _decode_base64_file_payload(data)
    if decoded is None:
        return None
    text = _ascii_text(data)
    if (
        text is not None
        and text.strip().lower().startswith("data:")
        and _data_url_is_text_authored(url=text, mime_type=decoded[1])
    ):
        return None
    if isinstance(data, str):
        return decoded
    if isinstance(
        data, (bytes, bytearray, memoryview)
    ) and _looks_like_base64_file_data(
        data=bytes(data),
        mime_type=mime_type,
    ):
        return decoded
    return None


def _twilio_generated_image_name(*, mime_type: str | None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ".png"
    return f"generated-image{extension}"


def _twilio_outbound_media_source_key(*, kind: str, value: object) -> str | None:
    normalized = str(value).strip()
    if normalized == "":
        return None
    return f"{kind}:{normalized}"


def _append_unique_twilio_media_url(
    *,
    active: _ActiveTwilioTurn,
    media_url: str,
    source_key: str | None = None,
) -> bool:
    content_key = f"url:{media_url.strip()}"
    candidate_keys = [key for key in (source_key, content_key) if key is not None]
    if any(key in active.media_keys for key in candidate_keys):
        logger.info(
            "twilio_duplicate_media_skipped source_key=%s url=%s",
            source_key,
            media_url,
        )
        return False
    active.media_urls.append(media_url)
    active.media_keys.update(candidate_keys)
    return True


def _slug(value: object) -> str:
    raw = str(value)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return slug or "unknown"


def _normalize_twilio_phone_number(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def _allowed_phone_numbers_from_value(
    value: str | Sequence[str] | None,
) -> frozenset[str] | None:
    if value is None:
        return None
    values = value.split(",") if isinstance(value, str) else value
    allowed_numbers = frozenset(
        normalized
        for raw_number in values
        if (normalized := _normalize_twilio_phone_number(str(raw_number))) != ""
    )
    return allowed_numbers or None


def _first_form_value(fields: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = fields.get(name)
        if values is None or len(values) == 0:
            continue
        value = values[0].strip()
        if value != "":
            return value
    return ""


def _int_form_value(fields: dict[str, list[str]], name: str) -> int:
    raw_value = _first_form_value(fields, name)
    if raw_value == "":
        return 0
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Twilio queue body field {name} must be an integer.") from exc
    if value < 0:
        raise ValueError(f"Twilio queue body field {name} must not be negative.")
    return value


def _int_header_value(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _mime_extension(mime_type: str | None) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip()
    return mimetypes.guess_extension(normalized) or ""


def _inbound_media_from_fields(
    fields: dict[str, list[str]],
) -> tuple[TwilioInboundMedia, ...]:
    media: list[TwilioInboundMedia] = []
    for index in range(_int_form_value(fields, "NumMedia")):
        media_url = _first_form_value(fields, f"MediaUrl{index}")
        if media_url == "":
            continue
        media.append(
            TwilioInboundMedia(
                url=media_url,
                content_type=_first_form_value(fields, f"MediaContentType{index}")
                or None,
                index=index,
            )
        )
    return tuple(media)


def _queue_body_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        raise ValueError("Twilio queue messages must be JSON objects.")
    body = message.get("body")
    if not isinstance(body, str):
        raise ValueError("Twilio queue messages must include a string body.")
    return body


def parse_twilio_inbound_message(message: Any) -> TwilioInboundMessage:
    fields = parse_qs(_queue_body_from_message(message), keep_blank_values=True)
    from_number = _first_form_value(fields, "From")
    to_number = _first_form_value(fields, "To")
    body = _first_form_value(fields, "Body")
    if from_number == "" or to_number == "":
        raise ValueError("Twilio queue body must include From and To fields.")
    if from_number.lower().startswith("whatsapp:") or to_number.lower().startswith(
        "whatsapp:"
    ):
        raise ValueError(
            "Twilio WhatsApp messages are not handled by this SMS channel. "
            "Use the whatsapp-channel example for WhatsApp Cloud API messages."
        )
    media = _inbound_media_from_fields(fields)
    if body == "" and len(media) == 0:
        raise ValueError("Twilio queue body must include Body text or inbound media.")

    message_sid = _first_form_value(
        fields,
        "MessageSid",
        "SmsMessageSid",
        "SmsSid",
    )
    if message_sid == "":
        message_sid = f"local-{uuid.uuid4()}"

    profile_name = _first_form_value(fields, "ProfileName")
    sender_name = profile_name or from_number
    return TwilioInboundMessage(
        message_sid=message_sid,
        from_number=from_number,
        to_number=to_number,
        body=body,
        sender_name=sender_name,
        media=media,
    )


class TwilioChannel(ThreadedChannel):
    def __init__(
        self,
        *,
        room: RoomClient,
        account_sid: str,
        auth_token: str,
        queue_name: str = QUEUE_NAME,
        threading_mode: str | None = None,
        thread_dir: str | None = None,
        thread_url_scheme: str | None = None,
        thread_path_extension: str = ".thread",
        thread_list_path: str | None = None,
        llm_adapter: LLMAdapter | None = None,
        thread_prefix: str = THREAD_PREFIX,
        media_storage_prefix: str = MEDIA_STORAGE_PREFIX,
        inbound_media_max_bytes: int = DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        allowed_phone_numbers: str | Sequence[str] | None = None,
        dry_run: bool = False,
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
        self._queue_name = normalized_queue_name
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._thread_prefix = thread_prefix.rstrip("/") or ".threads/twilio"
        self._media_storage_prefix = (
            media_storage_prefix.strip().strip("/") or ".threads/twilio-media"
        )
        if inbound_media_max_bytes < 0:
            raise ValueError("inbound_media_max_bytes must be 0 or greater")
        self._inbound_media_max_bytes = inbound_media_max_bytes
        self._allowed_phone_numbers = _allowed_phone_numbers_from_value(
            allowed_phone_numbers
        )
        self._dry_run = dry_run
        self._receive_from_http = receive_from_http
        self._http_session: ClientSession | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._pending_turns_by_message_id: dict[str, _PendingTwilioTurn] = {}
        self._active_turns_by_turn_id: dict[str, _ActiveTwilioTurn] = {}
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    def _default_thread_dir_fallback_name(self) -> str:
        return "twilio"

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
        self._http_session = new_client_session(
            auth=BasicAuth(self._account_sid, self._auth_token),
        )
        if not self._receive_from_http:
            await self._room.queues.open(name=self._queue_name)
            self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("twilio_channel_started transport=%s", self.transport)

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
            self._active_turns_by_turn_id[data.turn_id] = _ActiveTwilioTurn(
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
            pending.response.set_result(_TwilioTurnResponse(text=data.error.message))
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
            source_key = _twilio_outbound_media_source_key(
                kind="file-url",
                value=data.url,
            )
            if source_key is not None and source_key in active.media_keys:
                logger.info(
                    "twilio_duplicate_media_skipped source_key=%s",
                    source_key,
                )
                return
            media_url = await self._twilio_media_url_for_agent_file_url(url=data.url)
            if media_url is not None:
                _append_unique_twilio_media_url(
                    active=active,
                    media_url=media_url,
                    source_key=source_key,
                )
            return

        if isinstance(data, AgentImageGenerationCompleted):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is None:
                return
            for image in data.images:
                source_key = _twilio_outbound_media_source_key(
                    kind="generated-image-uri",
                    value=image.uri or "",
                )
                if source_key is not None and source_key in active.media_keys:
                    logger.info(
                        "twilio_duplicate_media_skipped source_key=%s",
                        source_key,
                    )
                    continue
                media_url = await self._twilio_media_url_for_generated_image(
                    image=image,
                )
                if media_url is not None:
                    _append_unique_twilio_media_url(
                        active=active,
                        media_url=media_url,
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
            active.response.set_result(_TwilioTurnResponse(text=data.error.message))
            return
        active.response.set_result(
            _TwilioTurnResponse(
                text="".join(active.text_parts).strip(),
                media_urls=tuple(active.media_urls),
            )
        )

    async def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                queued_message = await self._room.queues.receive(
                    name=self._queue_name,
                    create=True,
                    wait=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._room.is_closed:
                    logger.debug("stopping Twilio receive loop after room close")
                    return
                logger.exception(
                    "twilio_queue_receive_failed queue=%s", self._queue_name
                )
                await asyncio.sleep(1)
                continue

            if queued_message is None:
                continue

            task = asyncio.create_task(self._process_queue_message(queued_message))
            self._message_tasks.add(task)
            task.add_done_callback(self._message_tasks.discard)
            task.add_done_callback(self._log_message_task_failure)

    async def _process_queue_message(self, queued_message: Any) -> None:
        try:
            twilio_message = parse_twilio_inbound_message(queued_message)
        except ValueError as exc:
            logger.warning("twilio_queue_message_ignored error=%s", exc)
            return

        if not self._is_phone_number_allowed(twilio_message.from_number):
            logger.warning(
                "twilio_message_denied reason=not_allowlisted from=%s to=%s "
                "message_sid=%s num_media=%s "
                "allowed_from_numbers_hint=MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS=%s",
                twilio_message.from_number,
                twilio_message.to_number,
                twilio_message.message_sid,
                len(twilio_message.media),
                twilio_message.from_number,
            )
            return

        logger.info(
            "twilio_message_allowed from=%s to=%s message_sid=%s num_media=%s",
            twilio_message.from_number,
            twilio_message.to_number,
            twilio_message.message_sid,
            len(twilio_message.media),
        )

        conversation_key = f"{twilio_message.to_number}:{twilio_message.from_number}"
        lock = self._conversation_locks.setdefault(
            conversation_key,
            asyncio.Lock(),
        )
        async with lock:
            response = await self._send_twilio_turn(message=twilio_message)
            text_chunks = (
                twilio_chunks(response.text)
                if response.text.strip() != "" or len(response.media_urls) == 0
                else []
            )
            for chunk in text_chunks:
                await self._send_twilio_message(
                    from_number=twilio_message.to_number,
                    to_number=twilio_message.from_number,
                    body=chunk,
                )
            for media_urls in twilio_media_batches(response.media_urls):
                await self._send_twilio_message(
                    from_number=twilio_message.to_number,
                    to_number=twilio_message.from_number,
                    body="",
                    media_urls=media_urls,
                )

    async def _send_twilio_turn(
        self,
        *,
        message: TwilioInboundMessage,
    ) -> _TwilioTurnResponse:
        participant_attributes = {
            "name": message.sender_name,
            "role": "user",
            "twilio.channel": "sms",
            "twilio.from": message.from_number,
            "twilio.to": message.to_number,
            "twilio.message_sid": message.message_sid,
        }
        if len(message.media) > 0:
            participant_attributes["twilio.num_media"] = str(len(message.media))
        participant = Participant(
            id=f"twilio:{_slug(message.from_number)}",
            attributes=participant_attributes,
        )
        thread_id = self._thread_id_for_conversation(message=message)
        self.bump_thread(path=thread_id, name=message.sender_name)
        content = await self._turn_content_for_twilio_message(message=message)
        if len(content) == 0:
            return _TwilioTurnResponse(
                text="The Twilio message did not include text or downloadable media."
            )

        response: asyncio.Future[_TwilioTurnResponse] = (
            asyncio.get_running_loop().create_future()
        )
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=thread_id,
            content=content,
        )
        self._pending_turns_by_message_id[turn_start.message_id] = _PendingTwilioTurn(
            message=message,
            response=response,
        )
        self.emit(sender=participant, payload=turn_start)

        try:
            return await asyncio.wait_for(response, timeout=RESPONSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._clear_pending_response(response=response)
            logger.warning(
                "twilio_turn_response_timed_out message_sid=%s",
                message.message_sid,
            )
            return _TwilioTurnResponse(
                text="The room agent did not answer before the Twilio channel timed out."
            )
        except Exception:
            self._clear_pending_response(response=response)
            logger.exception("twilio_turn_failed message_sid=%s", message.message_sid)
            return _TwilioTurnResponse(
                text="The room agent could not answer that message."
            )

    async def _turn_content_for_twilio_message(
        self,
        *,
        message: TwilioInboundMessage,
    ) -> list[AgentTextContent | AgentFileContent]:
        content: list[AgentTextContent | AgentFileContent] = []
        if message.body.strip() != "":
            content.append(AgentTextContent(type="text", text=message.body))
        for media in message.media:
            media_content = await self._agent_content_for_inbound_media(
                message=message,
                media=media,
            )
            if media_content is not None:
                content.append(media_content)
        return content

    async def _agent_content_for_inbound_media(
        self,
        *,
        message: TwilioInboundMessage,
        media: TwilioInboundMedia,
    ) -> AgentFileContent | AgentTextContent | None:
        try:
            downloaded = await self._download_twilio_media(
                media=media,
                max_bytes=self._inbound_media_max_bytes,
            )
        except TwilioMediaTooLargeError as exc:
            logger.warning(
                "twilio_inbound_media_too_large url=%s size=%s max=%s source=%s",
                exc.media_url,
                exc.size_bytes,
                exc.max_bytes,
                exc.source,
            )
            return AgentTextContent(
                type="text",
                text=(
                    f"Twilio attachment {media.index} was not attached because it is "
                    f"{exc.size_bytes} bytes, which exceeds the configured inbound "
                    f"media limit of {exc.max_bytes} bytes."
                ),
            )
        except Exception:
            logger.exception(
                "twilio_inbound_media_download_failed url=%s",
                media.url,
            )
            return None

        mime_type = media.content_type or downloaded.content_type
        filename = self._default_inbound_media_filename(
            media=media,
            mime_type=mime_type,
        )
        path = self._inbound_media_storage_path(
            message=message,
            media=media,
            filename=filename,
            mime_type=mime_type,
        )
        try:
            await self._room.storage.upload(
                path=path,
                data=downloaded.data,
                overwrite=True,
                name=filename,
                mime_type=mime_type,
            )
        except Exception:
            logger.exception(
                "twilio_inbound_media_upload_failed url=%s path=%s",
                media.url,
                path,
            )
            return None
        return AgentFileContent(type="file", url=f"room:///{path}", name=filename)

    async def _download_twilio_media(
        self,
        *,
        media: TwilioInboundMedia,
        max_bytes: int | None,
    ) -> _TwilioDownloadedMedia:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be 0 or greater")
        http_session = self._http_session
        if http_session is None:
            raise RuntimeError("Twilio HTTP session is not open.")

        async with http_session.get(media.url) as response:
            if response.status < 400:
                content_length = _int_header_value(
                    response.headers.get("content-length")
                )
                self._raise_if_media_too_large(
                    media=media,
                    size_bytes=content_length,
                    max_bytes=max_bytes,
                    source="content-length",
                )
                data = await self._read_response_bytes(
                    response=response,
                    media=media,
                    max_bytes=max_bytes,
                )
                return _TwilioDownloadedMedia(
                    data=data,
                    content_type=response.headers.get("content-type"),
                )
            response_text = await response.text()
            logger.error(
                "twilio_media_download_failed status=%s body=%s",
                response.status,
                response_text[:500],
            )
            response.raise_for_status()
        raise RuntimeError(f"Twilio media download failed for {media.url}.")

    @staticmethod
    def _raise_if_media_too_large(
        *,
        media: TwilioInboundMedia,
        size_bytes: int | None,
        max_bytes: int | None,
        source: str,
    ) -> None:
        if max_bytes is not None and size_bytes is not None and size_bytes > max_bytes:
            raise TwilioMediaTooLargeError(
                media_url=media.url,
                size_bytes=size_bytes,
                max_bytes=max_bytes,
                source=source,
            )

    async def _read_response_bytes(
        self,
        *,
        response: ClientResponse,
        media: TwilioInboundMedia,
        max_bytes: int | None,
    ) -> bytes:
        if max_bytes is not None:
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                self._raise_if_media_too_large(
                    media=media,
                    size_bytes=len(data),
                    max_bytes=max_bytes,
                    source="download",
                )
            return bytes(data)

        data = await response.read()
        self._raise_if_media_too_large(
            media=media,
            size_bytes=len(data),
            max_bytes=max_bytes,
            source="download",
        )
        return data

    def _inbound_media_storage_path(
        self,
        *,
        message: TwilioInboundMessage,
        media: TwilioInboundMedia,
        filename: str,
        mime_type: str | None,
    ) -> str:
        safe_filename = _slug(filename)
        if PurePosixPath(safe_filename).suffix == "":
            extension = _mime_extension(mime_type)
            safe_filename = f"{safe_filename}{extension}"
        return (
            f"{self._media_storage_prefix}/"
            f"{_slug(message.from_number)}/"
            f"{_slug(message.message_sid)}-{media.index}-{safe_filename}"
        )

    @staticmethod
    def _default_inbound_media_filename(
        *,
        media: TwilioInboundMedia,
        mime_type: str | None,
    ) -> str:
        parsed_name = PurePosixPath(urlparse(media.url).path).name.strip()
        if parsed_name != "":
            if PurePosixPath(parsed_name).suffix == "":
                return f"{parsed_name}{_mime_extension(mime_type)}"
            return parsed_name
        extension = _mime_extension(mime_type)
        return f"twilio-media-{media.index}{extension}"

    async def _twilio_media_url_for_agent_file_url(self, *, url: str) -> str | None:
        normalized_url = url.strip()
        if is_twilio_fetchable_media_url(url=normalized_url):
            return normalized_url

        data_url = _decode_data_url_payload(normalized_url)
        if data_url is not None:
            data, mime_type = data_url
            if _data_url_is_text_authored(url=normalized_url, mime_type=mime_type):
                logger.warning(
                    "twilio_media_data_url_ignored_text_authored mime_type=%s",
                    mime_type,
                )
                return None
            return await self._twilio_media_url_for_outbound_bytes(
                data=data,
                mime_type=mime_type,
                filename="twilio-file",
            )

        storage_path = room_storage_path_from_agent_file_url(url=normalized_url)
        if storage_path is None:
            logger.warning("twilio_media_url_ignored url=%s", normalized_url)
            return None

        decoded_media_url = await self._twilio_decoded_room_file_media_url(
            storage_path=storage_path,
        )
        if decoded_media_url is not None:
            return decoded_media_url

        try:
            media_url = await self._room.storage.download_url(path=storage_path)
        except Exception:
            logger.exception(
                "twilio_media_download_url_failed path=%s",
                storage_path,
            )
            return None

        if not is_twilio_fetchable_media_url(url=media_url):
            logger.warning(
                "twilio_media_download_url_ignored path=%s url=%s",
                storage_path,
                media_url,
            )
            return None
        return media_url

    async def _twilio_decoded_room_file_media_url(
        self,
        *,
        storage_path: str,
    ) -> str | None:
        try:
            content = await self._room.storage.download(path=storage_path)
        except Exception:
            return None

        decoded = _twilio_decoded_file_content_data(
            data=content.data,
            mime_type=content.mime_type,
        )
        if decoded is None:
            return None

        data, decoded_mime_type = decoded
        return await self._twilio_media_url_for_outbound_bytes(
            data=data,
            mime_type=decoded_mime_type or content.mime_type,
            filename=content.name or PurePosixPath(storage_path).name,
        )

    async def _twilio_media_url_for_generated_image(
        self,
        *,
        image: AgentGeneratedImage,
    ) -> str | None:
        uri = (image.uri or "").strip()
        if uri == "":
            return None

        data_url = _decode_data_url_payload(uri)
        if data_url is not None:
            data, mime_type = data_url
            resolved_mime_type = mime_type or image.mime_type
            if _data_url_is_text_authored(url=uri, mime_type=resolved_mime_type):
                logger.warning(
                    "twilio_generated_image_data_url_ignored_text_authored "
                    "mime_type=%s",
                    resolved_mime_type,
                )
                return None
            return await self._twilio_media_url_for_outbound_bytes(
                data=data,
                mime_type=resolved_mime_type,
                filename=_twilio_generated_image_name(mime_type=resolved_mime_type),
            )

        try:
            record = await ImageDatasetClient(self._room.datasets).read_record_from_uri(
                uri,
                fallback_mime_type=image.mime_type,
            )
        except Exception:
            logger.exception("twilio_generated_image_download_failed uri=%s", uri)
            return None

        if record is not None:
            return await self._twilio_media_url_for_outbound_bytes(
                data=record.data,
                mime_type=record.mime_type,
                filename=_twilio_generated_image_name(mime_type=record.mime_type),
            )

        if is_twilio_fetchable_media_url(url=uri):
            downloaded = await self._download_twilio_generated_http_media_url(
                url=uri,
                fallback_mime_type=image.mime_type,
            )
            if downloaded is None:
                return None
            mime_type = downloaded.content_type or image.mime_type
            return await self._twilio_media_url_for_outbound_bytes(
                data=downloaded.data,
                mime_type=mime_type,
                filename=_twilio_generated_image_name(mime_type=mime_type),
            )

        logger.warning("twilio_generated_image_ignored uri=%s", uri)
        return None

    async def _download_twilio_generated_http_media_url(
        self,
        *,
        url: str,
        fallback_mime_type: str | None,
    ) -> _TwilioDownloadedMedia | None:
        media = TwilioInboundMedia(url=url, content_type=fallback_mime_type)
        try:
            async with new_client_session() as http_session:
                async with http_session.get(url) as response:
                    if response.status >= 400:
                        response_text = await response.text()
                        logger.error(
                            "twilio_generated_image_http_download_failed "
                            "status=%s url=%s body=%s",
                            response.status,
                            url,
                            response_text[:500],
                        )
                        response.raise_for_status()
                    content_length = _int_header_value(
                        response.headers.get("content-length")
                    )
                    self._raise_if_media_too_large(
                        media=media,
                        size_bytes=content_length,
                        max_bytes=self._inbound_media_max_bytes,
                        source="content-length",
                    )
                    data = await self._read_response_bytes(
                        response=response,
                        media=media,
                        max_bytes=self._inbound_media_max_bytes,
                    )
                    content_type = (
                        response.headers.get("content-type") or fallback_mime_type
                    )
                    if content_type is not None:
                        content_type = content_type.split(";", 1)[0].strip()
                    return _TwilioDownloadedMedia(
                        data=data,
                        content_type=content_type or fallback_mime_type,
                    )
        except TwilioMediaTooLargeError as exc:
            logger.warning(
                "twilio_generated_image_http_download_too_large url=%s size=%s max=%s",
                exc.media_url,
                exc.size_bytes,
                exc.max_bytes,
            )
            return None
        except Exception:
            logger.exception("twilio_generated_image_http_download_failed url=%s", url)
            return None

    async def _twilio_media_url_for_outbound_bytes(
        self,
        *,
        data: bytes,
        mime_type: str | None,
        filename: str,
    ) -> str | None:
        extension = mimetypes.guess_extension(mime_type or "") or ""
        resolved_filename = filename.strip() or "twilio-file"
        if PurePosixPath(resolved_filename).suffix == "" and extension != "":
            resolved_filename = f"{resolved_filename}{extension}"
        digest = hashlib.sha256(data).hexdigest()[:16]
        path = (
            f"{self._media_storage_prefix}/outbound/{digest}-{_slug(resolved_filename)}"
        )
        try:
            await self._room.storage.upload(
                path=path,
                data=data,
                overwrite=True,
                name=resolved_filename,
                mime_type=mime_type,
            )
            media_url = await self._room.storage.download_url(path=path)
        except Exception:
            logger.exception("twilio_outbound_media_upload_failed path=%s", path)
            return None

        if not is_twilio_fetchable_media_url(url=media_url):
            logger.warning(
                "twilio_outbound_media_download_url_ignored path=%s url=%s",
                path,
                media_url,
            )
            return None
        return media_url

    async def _send_twilio_message(
        self,
        *,
        from_number: str,
        to_number: str,
        body: str,
        media_urls: Sequence[str] = (),
    ) -> None:
        if not self._is_phone_number_allowed(to_number):
            raise PermissionError(
                "Twilio recipient is not in MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS."
            )

        if self._dry_run:
            logger.info(
                "twilio_dry_run_message from=%s to=%s body=%s media_urls=%s",
                from_number,
                to_number,
                body,
                list(media_urls),
            )
            return

        http_session = self._http_session
        if http_session is None:
            raise RuntimeError("Twilio HTTP session is not open.")

        url = f"{TWILIO_API_BASE_URL}/Accounts/{self._account_sid}/Messages.json"
        async with http_session.post(
            url,
            data=twilio_message_form_data(
                from_number=from_number,
                to_number=to_number,
                body=body,
                media_urls=media_urls,
            ),
        ) as response:
            if response.status < 400:
                logger.info("twilio_message_sent to=%s", to_number)
                return
            response_text = await response.text()
            logger.error(
                "twilio_message_send_failed status=%s body=%s",
                response.status,
                response_text[:500],
            )
            response.raise_for_status()

    def _is_phone_number_allowed(self, phone_number: str | None) -> bool:
        if self._allowed_phone_numbers is None:
            return True
        if phone_number is None:
            return False
        normalized_phone_number = _normalize_twilio_phone_number(phone_number)
        return normalized_phone_number in self._allowed_phone_numbers

    def _thread_id_for_conversation(self, *, message: TwilioInboundMessage) -> str:
        uses_dataset_thread_url = (
            self._thread_url_scheme == "dataset://"
            or self._thread_prefix.startswith("dataset://")
        )
        extension = "" if uses_dataset_thread_url else self._thread_path_extension
        path = (
            f"{self._thread_prefix}/"
            f"{_slug(message.to_number)}-{_slug(message.from_number)}{extension}"
        )
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
        self,
        *,
        response: asyncio.Future[_TwilioTurnResponse],
    ) -> None:
        for message_id, pending in list(self._pending_turns_by_message_id.items()):
            if pending.response is response:
                self._pending_turns_by_message_id.pop(message_id, None)
        for turn_id, active in list(self._active_turns_by_turn_id.items()):
            if active.response is response:
                self._active_turns_by_turn_id.pop(turn_id, None)

    @staticmethod
    def _log_message_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("twilio_message_task_failed", exc_info=exc)


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
) -> TwilioChannel:
    return TwilioChannel(
        room=room,
        account_sid=required_env("TWILIO_ACCOUNT_SID"),
        auth_token=required_env("TWILIO_AUTH_TOKEN"),
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_url_scheme=thread_url_scheme,
        thread_path_extension=thread_path_extension,
        thread_list_path=thread_list_path,
        llm_adapter=llm_adapter,
        media_storage_prefix=os.getenv(
            "MESHAGENT_TWILIO_MEDIA_STORAGE_PREFIX",
            MEDIA_STORAGE_PREFIX,
        ),
        inbound_media_max_bytes=env_int(
            "MESHAGENT_TWILIO_INBOUND_MEDIA_MAX_BYTES",
            default=DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        ),
        allowed_phone_numbers=os.getenv("MESHAGENT_TWILIO_ALLOWED_FROM_NUMBERS"),
        dry_run=env_flag("MESHAGENT_TWILIO_DRY_RUN"),
        receive_from_http=receive_from_http,
    )
