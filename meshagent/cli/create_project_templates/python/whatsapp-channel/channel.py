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
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, Sequence, cast
from urllib.parse import unquote, unquote_to_bytes, urlparse

from aiohttp import ClientResponse, ClientSession, FormData
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


logger = logging.getLogger("meshagent.whatsapp_channel")

DEFAULT_INBOUND_MEDIA_MAX_BYTES = 25_000_000
QUEUE_NAME = os.getenv("MESHAGENT_WHATSAPP_QUEUE_NAME", "whatsapp-inbound")
THREAD_PREFIX = os.getenv("MESHAGENT_WHATSAPP_THREAD_PREFIX", ".threads/whatsapp")
MEDIA_STORAGE_PREFIX = os.getenv(
    "MESHAGENT_WHATSAPP_MEDIA_STORAGE_PREFIX",
    ".threads/whatsapp-media",
)
MAX_WHATSAPP_MESSAGE_CHARS = 3900
RESPONSE_TIMEOUT_SECONDS = float(
    os.getenv("MESHAGENT_WHATSAPP_RESPONSE_TIMEOUT", "300")
)
TYPING_REFRESH_SECONDS = float(
    os.getenv("MESHAGENT_WHATSAPP_TYPING_REFRESH_SECONDS", "5")
)
WHATSAPP_GRAPH_API_BASE_URL = os.getenv(
    "MESHAGENT_WHATSAPP_GRAPH_API_BASE_URL",
    "https://graph.facebook.com/v23.0",
).rstrip("/")
WhatsAppMediaKind = Literal["audio", "document", "image", "video"]
WhatsAppInboundMediaKind = Literal["audio", "document", "image", "sticker", "video"]
WHATSAPP_INBOUND_MEDIA_TYPES = frozenset(
    ("audio", "document", "image", "sticker", "video")
)
TEXT_AUTHORED_INLINE_MEDIA_TYPES = frozenset(
    {
        "image/svg+xml",
        "image/x-portable-bitmap",
        "image/x-portable-graymap",
        "image/x-portable-pixmap",
    }
)


@dataclass(frozen=True, slots=True)
class WhatsAppInboundMessage:
    message_id: str
    from_number: str
    body: str
    sender_name: str
    phone_number_id: str | None
    display_phone_number: str | None
    message_type: str = "text"
    interactive_type: str | None = None
    interactive_reply_id: str | None = None
    interactive_reply_title: str | None = None
    interactive_reply_description: str | None = None
    reply_to_message_id: str | None = None
    media: tuple["WhatsAppInboundMedia", ...] = ()


@dataclass(frozen=True, slots=True)
class WhatsAppInboundMedia:
    media_id: str
    kind: WhatsAppInboundMediaKind
    mime_type: str | None = None
    sha256: str | None = None
    caption: str | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class WhatsAppStatusEvent:
    message_id: str
    status: str
    recipient_id: str | None
    phone_number_id: str | None
    display_phone_number: str | None
    timestamp: str | None = None
    conversation_id: str | None = None
    errors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class WhatsAppInteractiveButton:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class WhatsAppInteractiveListRow:
    id: str
    title: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class WhatsAppInteractiveListSection:
    title: str
    rows: tuple[WhatsAppInteractiveListRow, ...]


@dataclass(frozen=True, slots=True)
class WhatsAppOutboundMedia:
    url: str
    kind: WhatsAppMediaKind
    filename: str | None = None
    storage_path: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class WhatsAppOutboundMediaId:
    media_id: str
    kind: WhatsAppMediaKind
    filename: str | None = None
    caption: str | None = None
    storage_path: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class WhatsAppDownloadedMedia:
    media_id: str
    data: bytes
    mime_type: str | None = None
    sha256: str | None = None
    file_size: int | None = None
    url: str | None = None


class WhatsAppMediaTooLargeError(RuntimeError):
    def __init__(
        self,
        *,
        media_id: str,
        size_bytes: int,
        max_bytes: int,
        source: str,
    ) -> None:
        super().__init__(
            f"WhatsApp media {media_id} is {size_bytes} bytes, "
            f"which exceeds the configured {max_bytes} byte limit."
        )
        self.media_id = media_id
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        self.source = source


WhatsAppResponseMedia = WhatsAppOutboundMedia | WhatsAppOutboundMediaId


@dataclass(frozen=True, slots=True)
class _WhatsAppTurnResponse:
    text: str
    media: tuple[WhatsAppResponseMedia, ...] = ()


@dataclass(slots=True)
class _PendingWhatsAppTurn:
    message: WhatsAppInboundMessage
    response: asyncio.Future[_WhatsAppTurnResponse]


@dataclass(slots=True)
class _ActiveWhatsAppTurn:
    message: WhatsAppInboundMessage
    response: asyncio.Future[_WhatsAppTurnResponse]
    text_parts: list[str] = field(default_factory=list)
    media: list[WhatsAppResponseMedia] = field(default_factory=list)
    media_keys: set[str] = field(default_factory=set)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the WhatsApp channel.")
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


def _normalize_whatsapp_phone_number(value: str) -> str:
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
        if (normalized := _normalize_whatsapp_phone_number(str(raw_number))) != ""
    )
    return allowed_numbers or None


def whatsapp_chunks(text: str) -> list[str]:
    normalized = text.strip()
    if normalized == "":
        return ["The room agent returned an empty response."]
    return [
        normalized[index : index + MAX_WHATSAPP_MESSAGE_CHARS]
        for index in range(0, len(normalized), MAX_WHATSAPP_MESSAGE_CHARS)
    ]


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


def is_whatsapp_fetchable_media_url(*, url: str) -> bool:
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


def _whatsapp_decoded_file_content_data(
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


def whatsapp_media_kind_for_mime_type(*, mime_type: str | None) -> WhatsAppMediaKind:
    normalized = (mime_type or "").strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return "document"


def whatsapp_media_kind_for_url(*, url: str) -> WhatsAppMediaKind:
    path = urlparse(url.strip()).path
    mime_type, _ = mimetypes.guess_type(path)
    return whatsapp_media_kind_for_mime_type(mime_type=mime_type)


def whatsapp_media_filename_for_url(*, url: str) -> str | None:
    path = urlparse(url.strip()).path
    filename = PurePosixPath(unquote(path)).name.strip()
    if filename == "" or filename in {".", ".."}:
        return None
    return filename


def whatsapp_outbound_media_from_url(
    *,
    url: str,
    source_url: str | None = None,
    storage_path: str | None = None,
) -> WhatsAppOutboundMedia:
    metadata_url = source_url or url
    kind = whatsapp_media_kind_for_url(url=metadata_url)
    filename = whatsapp_media_filename_for_url(url=metadata_url)
    return WhatsAppOutboundMedia(
        url=url,
        kind=kind,
        filename=filename if kind == "document" else None,
        storage_path=storage_path,
    )


def whatsapp_text_message_payload(*, to_number: str, body: str) -> dict[str, object]:
    normalized_body = body.strip()
    if normalized_body == "":
        raise ValueError("WhatsApp text messages require a non-empty body.")
    return {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": normalized_body},
    }


def whatsapp_media_message_payload(
    *,
    to_number: str,
    media: WhatsAppOutboundMedia,
) -> dict[str, object]:
    media_payload = {"link": media.url}
    if media.kind == "document" and media.filename is not None:
        media_payload["filename"] = media.filename
    return {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": media.kind,
        media.kind: media_payload,
    }


def whatsapp_media_id_message_payload(
    *,
    to_number: str,
    media: WhatsAppOutboundMediaId,
) -> dict[str, object]:
    media_id = media.media_id.strip()
    if media_id == "":
        raise ValueError("WhatsApp media ID messages require a non-empty media_id.")
    media_payload = {"id": media_id}
    if media.kind == "document" and media.filename is not None:
        media_payload["filename"] = media.filename
    if (
        media.caption is not None
        and media.caption.strip() != ""
        and media.kind != "audio"
    ):
        media_payload["caption"] = media.caption.strip()
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": media.kind,
        media.kind: media_payload,
    }


def whatsapp_media_size_limit_bytes(*, mime_type: str) -> int | None:
    normalized_mime_type = mime_type.strip().lower()
    if "/" not in normalized_mime_type:
        return None
    major_type, sub_type = normalized_mime_type.split("/", 1)
    if major_type in {"audio", "video"}:
        return 16_000_000
    if major_type == "image":
        if sub_type in {"jpeg", "jpg", "png"}:
            return 5_000_000
        if sub_type == "webp":
            return 100_000
        return None
    if major_type in {"application", "text"}:
        return 100_000_000
    return None


def validate_whatsapp_media_size(*, size_bytes: int, mime_type: str) -> bool:
    limit = whatsapp_media_size_limit_bytes(mime_type=mime_type)
    if limit is None:
        return True
    return 0 <= size_bytes <= limit


def whatsapp_read_receipt_payload(
    *,
    message_id: str,
    typing: bool = False,
) -> dict[str, object]:
    normalized_message_id = message_id.strip()
    if normalized_message_id == "":
        raise ValueError("WhatsApp read receipts require a non-empty message_id.")
    payload: dict[str, object] = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": normalized_message_id,
    }
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    return payload


def _interactive_button_payload(
    button: WhatsAppInteractiveButton | Mapping[str, str],
) -> dict[str, object]:
    if isinstance(button, WhatsAppInteractiveButton):
        button_id = button.id.strip()
        title = button.title.strip()
    else:
        button_id = str(button.get("id", "")).strip()
        title = str(button.get("title", "")).strip()
    if button_id == "" or title == "":
        raise ValueError("WhatsApp interactive buttons require id and title.")
    return {
        "type": "reply",
        "reply": {
            "id": button_id,
            "title": title,
        },
    }


def whatsapp_interactive_button_message_payload(
    *,
    to_number: str,
    body: str,
    buttons: Sequence[WhatsAppInteractiveButton | Mapping[str, str]],
    header_text: str | None = None,
    footer_text: str | None = None,
) -> dict[str, object]:
    normalized_body = body.strip()
    if normalized_body == "":
        raise ValueError("WhatsApp interactive button messages require body text.")
    button_payloads = [_interactive_button_payload(button) for button in buttons]
    if len(button_payloads) == 0 or len(button_payloads) > 3:
        raise ValueError("WhatsApp interactive button messages require 1 to 3 buttons.")

    interactive: dict[str, object] = {
        "type": "button",
        "body": {"text": normalized_body},
        "action": {"buttons": button_payloads},
    }
    if header_text is not None and header_text.strip() != "":
        interactive["header"] = {"type": "text", "text": header_text.strip()}
    if footer_text is not None and footer_text.strip() != "":
        interactive["footer"] = {"text": footer_text.strip()}

    return {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "interactive",
        "interactive": interactive,
    }


def _interactive_list_row_payload(
    row: WhatsAppInteractiveListRow | Mapping[str, str],
) -> dict[str, str]:
    if isinstance(row, WhatsAppInteractiveListRow):
        row_id = row.id.strip()
        title = row.title.strip()
        description = row.description.strip() if row.description is not None else None
    else:
        row_id = str(row.get("id", "")).strip()
        title = str(row.get("title", "")).strip()
        raw_description = row.get("description")
        description = (
            str(raw_description).strip() if raw_description is not None else None
        )
    if row_id == "" or title == "":
        raise ValueError("WhatsApp interactive list rows require id and title.")
    payload = {
        "id": row_id,
        "title": title,
    }
    if description is not None and description != "":
        payload["description"] = description
    return payload


def _interactive_list_section_payload(
    section: WhatsAppInteractiveListSection | Mapping[str, Any],
) -> dict[str, object]:
    if isinstance(section, WhatsAppInteractiveListSection):
        title = section.title.strip()
        rows = section.rows
    else:
        title = str(section.get("title", "")).strip()
        raw_rows = section.get("rows", ())
        rows = (
            raw_rows
            if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes))
            else ()
        )
    row_payloads = [_interactive_list_row_payload(row) for row in rows]
    if title == "" or len(row_payloads) == 0:
        raise ValueError("WhatsApp interactive list sections require title and rows.")
    return {
        "title": title,
        "rows": row_payloads,
    }


def whatsapp_interactive_list_message_payload(
    *,
    to_number: str,
    body: str,
    button_text: str,
    sections: Sequence[WhatsAppInteractiveListSection | Mapping[str, Any]],
    header_text: str | None = None,
    footer_text: str | None = None,
) -> dict[str, object]:
    normalized_body = body.strip()
    normalized_button_text = button_text.strip()
    if normalized_body == "":
        raise ValueError("WhatsApp interactive list messages require body text.")
    if normalized_button_text == "":
        raise ValueError("WhatsApp interactive list messages require button_text.")
    section_payloads = [
        _interactive_list_section_payload(section) for section in sections
    ]
    if len(section_payloads) == 0:
        raise ValueError("WhatsApp interactive list messages require sections.")

    interactive: dict[str, object] = {
        "type": "list",
        "body": {"text": normalized_body},
        "action": {
            "button": normalized_button_text,
            "sections": section_payloads,
        },
    }
    if header_text is not None and header_text.strip() != "":
        interactive["header"] = {"type": "text", "text": header_text.strip()}
    if footer_text is not None and footer_text.strip() != "":
        interactive["footer"] = {"text": footer_text.strip()}

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": interactive,
    }


def whatsapp_template_message_payload(
    *,
    to_number: str,
    template_name: str,
    language_code: str,
    components: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    normalized_template_name = template_name.strip()
    normalized_language_code = language_code.strip()
    if normalized_template_name == "":
        raise ValueError("WhatsApp template messages require a template_name.")
    if normalized_language_code == "":
        raise ValueError("WhatsApp template messages require a language_code.")

    template: dict[str, object] = {
        "name": normalized_template_name,
        "language": {"code": normalized_language_code},
    }
    if components is not None:
        template["components"] = [dict(component) for component in components]

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": template,
    }


def whatsapp_image_header_template_message_payload(
    *,
    to_number: str,
    template_name: str,
    language_code: str,
    image_url: str,
) -> dict[str, object]:
    return whatsapp_template_message_payload(
        to_number=to_number,
        template_name=template_name,
        language_code=language_code,
        components=[
            {
                "type": "header",
                "parameters": [
                    {
                        "type": "image",
                        "image": {"link": image_url},
                    }
                ],
            }
        ],
    )


def whatsapp_limited_time_offer_template_payload(
    *,
    to_number: str,
    template_name: str,
    language_code: str,
    image_url: str,
    offer_code: str,
    expiration_time_ms: int,
    button_index: int = 0,
) -> dict[str, object]:
    return whatsapp_template_message_payload(
        to_number=to_number,
        template_name=template_name,
        language_code=language_code,
        components=[
            {
                "type": "header",
                "parameters": [
                    {
                        "type": "image",
                        "image": {"link": image_url},
                    }
                ],
            },
            {
                "type": "limited_time_offer",
                "parameters": [
                    {
                        "type": "limited_time_offer",
                        "limited_time_offer": {
                            "expiration_time_ms": expiration_time_ms,
                        },
                    }
                ],
            },
            {
                "type": "button",
                "sub_type": "copy_code",
                "index": button_index,
                "parameters": [
                    {
                        "type": "coupon_code",
                        "coupon_code": offer_code,
                    }
                ],
            },
        ],
    )


def whatsapp_media_card_carousel_template_payload(
    *,
    to_number: str,
    template_name: str,
    language_code: str,
    image_urls: Sequence[str],
) -> dict[str, object]:
    return whatsapp_template_message_payload(
        to_number=to_number,
        template_name=template_name,
        language_code=language_code,
        components=[
            {
                "type": "carousel",
                "cards": [
                    {
                        "card_index": index,
                        "components": [
                            {
                                "type": "header",
                                "parameters": [
                                    {
                                        "type": "image",
                                        "image": {"link": image_url},
                                    }
                                ],
                            }
                        ],
                    }
                    for index, image_url in enumerate(image_urls)
                ],
            }
        ],
    )


def _slug(value: object) -> str:
    raw = str(value)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return slug or "unknown"


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _int_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _whatsapp_generated_image_name(*, mime_type: str | None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ".png"
    return f"generated-image{extension}"


def _whatsapp_outbound_media_source_key(*, kind: str, value: object) -> str | None:
    normalized = str(value).strip()
    if normalized == "":
        return None
    return f"{kind}:{normalized}"


def _whatsapp_response_media_key(media: WhatsAppResponseMedia) -> str:
    if isinstance(media, WhatsAppOutboundMediaId):
        return f"media-id:{media.media_id.strip()}"
    return f"url:{media.url.strip()}"


def _append_unique_whatsapp_media(
    *,
    active: _ActiveWhatsAppTurn,
    media: WhatsAppResponseMedia,
    source_key: str | None = None,
) -> bool:
    content_key = _whatsapp_response_media_key(media)
    candidate_keys = [key for key in (source_key, content_key) if key is not None]
    if any(key in active.media_keys for key in candidate_keys):
        logger.info(
            "whatsapp_duplicate_media_skipped source_key=%s media_key=%s",
            source_key,
            content_key,
        )
        return False
    active.media.append(media)
    active.media_keys.update(candidate_keys)
    return True


def _sent_whatsapp_message_id(response: dict[str, Any] | None) -> str | None:
    if not isinstance(response, dict):
        return None
    messages = response.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = _string_value(message.get("id"))
        if message_id != "":
            return message_id
    return None


def _webhook_entries_from_data(data: dict[str, Any]) -> list[Any]:
    entries = data.get("entry")
    if not isinstance(entries, list):
        raise ValueError("WhatsApp webhook body must include an entry list.")
    return entries


def _interactive_reply_details(
    raw_message: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None, str | None]:
    interactive = raw_message.get("interactive")
    if not isinstance(interactive, dict):
        return "", None, None, None, None

    for reply_type in ("button_reply", "list_reply"):
        reply = interactive.get(reply_type)
        if not isinstance(reply, dict):
            continue
        reply_id = _string_value(reply.get("id")) or None
        title = _string_value(reply.get("title")) or None
        description = _string_value(reply.get("description")) or None
        body = title or reply_id or ""
        return body, reply_type, reply_id, title, description

    return "", None, None, None, None


def _media_from_raw_message(
    *,
    message_type: str,
    raw_message: dict[str, Any],
) -> tuple[str, WhatsAppInboundMedia] | None:
    if message_type not in WHATSAPP_INBOUND_MEDIA_TYPES:
        return None
    raw_media = raw_message.get(message_type)
    if not isinstance(raw_media, dict):
        return None
    media_id = _string_value(raw_media.get("id"))
    if media_id == "":
        return None
    caption = _string_value(raw_media.get("caption")) or None
    filename = _string_value(raw_media.get("filename")) or None
    media = WhatsAppInboundMedia(
        media_id=media_id,
        kind=cast(WhatsAppInboundMediaKind, message_type),
        mime_type=_string_value(raw_media.get("mime_type")) or None,
        sha256=_string_value(raw_media.get("sha256")) or None,
        caption=caption,
        filename=filename,
    )
    fallback = f"WhatsApp {message_type} message"
    return caption or filename or fallback, media


def _reply_to_message_id_from_raw_message(raw_message: dict[str, Any]) -> str | None:
    context = raw_message.get("context")
    if not isinstance(context, dict):
        return None
    return _string_value(context.get("id")) or None


def _queue_body_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        raise ValueError("WhatsApp queue messages must be JSON objects.")
    body = message.get("body")
    if not isinstance(body, str):
        raise ValueError("WhatsApp queue messages must include a string body.")
    return body


def _queue_json_from_message(message: Any) -> dict[str, Any]:
    try:
        data = json.loads(_queue_body_from_message(message))
    except json.JSONDecodeError as exc:
        raise ValueError("WhatsApp queue body must be JSON.") from exc
    if not isinstance(data, dict):
        raise ValueError("WhatsApp queue body must be a JSON object.")
    return data


def _contact_names_by_wa_id(value: dict[str, Any]) -> dict[str, str]:
    contacts = value.get("contacts")
    if not isinstance(contacts, list):
        return {}

    names: dict[str, str] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        wa_id = _string_value(contact.get("wa_id"))
        if wa_id == "":
            continue
        profile = contact.get("profile")
        if not isinstance(profile, dict):
            continue
        name = _string_value(profile.get("name"))
        if name != "":
            names[wa_id] = name
    return names


def _parse_whatsapp_inbound_messages_from_data(
    data: dict[str, Any],
) -> list[WhatsAppInboundMessage]:
    entries = _webhook_entries_from_data(data)
    inbound_messages: list[WhatsAppInboundMessage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            phone_number_id = _string_value(metadata.get("phone_number_id")) or None
            display_phone_number = (
                _string_value(metadata.get("display_phone_number")) or None
            )
            contact_names = _contact_names_by_wa_id(value)
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for raw_message in messages:
                if not isinstance(raw_message, dict):
                    continue
                message_type = _string_value(raw_message.get("type"))
                from_number = _string_value(raw_message.get("from"))
                media: tuple[WhatsAppInboundMedia, ...] = ()
                interactive_type: str | None = None
                interactive_reply_id: str | None = None
                interactive_reply_title: str | None = None
                interactive_reply_description: str | None = None
                reply_to_message_id = _reply_to_message_id_from_raw_message(raw_message)
                if message_type == "text":
                    text = raw_message.get("text")
                    if not isinstance(text, dict):
                        continue
                    body = _string_value(text.get("body"))
                elif message_type == "interactive":
                    (
                        body,
                        interactive_type,
                        interactive_reply_id,
                        interactive_reply_title,
                        interactive_reply_description,
                    ) = _interactive_reply_details(raw_message)
                else:
                    media_details = _media_from_raw_message(
                        message_type=message_type,
                        raw_message=raw_message,
                    )
                    if media_details is None:
                        continue
                    body, inbound_media = media_details
                    media = (inbound_media,)
                if from_number == "" or body == "":
                    continue
                message_id = _string_value(raw_message.get("id"))
                if message_id == "":
                    message_id = f"local-{uuid.uuid4()}"
                inbound_messages.append(
                    WhatsAppInboundMessage(
                        message_id=message_id,
                        from_number=from_number,
                        body=body,
                        sender_name=contact_names.get(from_number, from_number),
                        phone_number_id=phone_number_id,
                        display_phone_number=display_phone_number,
                        message_type=message_type,
                        interactive_type=interactive_type,
                        interactive_reply_id=interactive_reply_id,
                        interactive_reply_title=interactive_reply_title,
                        interactive_reply_description=interactive_reply_description,
                        reply_to_message_id=reply_to_message_id,
                        media=media,
                    )
                )
    return inbound_messages


def parse_whatsapp_inbound_messages(message: Any) -> list[WhatsAppInboundMessage]:
    return _parse_whatsapp_inbound_messages_from_data(_queue_json_from_message(message))


def parse_whatsapp_inbound_message(message: Any) -> WhatsAppInboundMessage:
    messages = parse_whatsapp_inbound_messages(message)
    if len(messages) == 0:
        raise ValueError("WhatsApp webhook body did not include a supported message.")
    return messages[0]


def _parse_whatsapp_status_events_from_data(
    data: dict[str, Any],
) -> list[WhatsAppStatusEvent]:
    entries = _webhook_entries_from_data(data)
    status_events: list[WhatsAppStatusEvent] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            phone_number_id = _string_value(metadata.get("phone_number_id")) or None
            display_phone_number = (
                _string_value(metadata.get("display_phone_number")) or None
            )
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for raw_status in statuses:
                if not isinstance(raw_status, dict):
                    continue
                message_id = _string_value(raw_status.get("id"))
                status = _string_value(raw_status.get("status"))
                if message_id == "" or status == "":
                    continue
                conversation = raw_status.get("conversation")
                conversation_id = None
                if isinstance(conversation, dict):
                    conversation_id = _string_value(conversation.get("id")) or None
                raw_errors = raw_status.get("errors")
                errors: tuple[dict[str, Any], ...] = ()
                if isinstance(raw_errors, list):
                    errors = tuple(
                        dict(error) for error in raw_errors if isinstance(error, dict)
                    )
                status_events.append(
                    WhatsAppStatusEvent(
                        message_id=message_id,
                        status=status,
                        recipient_id=_string_value(raw_status.get("recipient_id"))
                        or None,
                        phone_number_id=phone_number_id,
                        display_phone_number=display_phone_number,
                        timestamp=_string_value(raw_status.get("timestamp")) or None,
                        conversation_id=conversation_id,
                        errors=errors,
                    )
                )
    return status_events


def parse_whatsapp_status_events(message: Any) -> list[WhatsAppStatusEvent]:
    return _parse_whatsapp_status_events_from_data(_queue_json_from_message(message))


class WhatsAppChannel(ThreadedChannel):
    def __init__(
        self,
        *,
        room: RoomClient,
        access_token: str,
        phone_number_id: str,
        queue_name: str = QUEUE_NAME,
        graph_api_base_url: str = WHATSAPP_GRAPH_API_BASE_URL,
        threading_mode: str | None = None,
        thread_dir: str | None = None,
        thread_url_scheme: str | None = None,
        thread_path_extension: str = ".thread",
        thread_list_path: str | None = None,
        llm_adapter: LLMAdapter | None = None,
        thread_prefix: str = THREAD_PREFIX,
        media_storage_prefix: str = MEDIA_STORAGE_PREFIX,
        dry_run: bool = False,
        send_read_receipts: bool = True,
        send_typing_indicators: bool = True,
        typing_refresh_seconds: float = TYPING_REFRESH_SECONDS,
        inbound_media_max_bytes: int = DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        allowed_phone_numbers: str | Sequence[str] | None = None,
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
        normalized_phone_number_id = phone_number_id.strip()
        if normalized_phone_number_id == "":
            raise ValueError("phone_number_id must not be empty")
        if inbound_media_max_bytes < 0:
            raise ValueError("inbound_media_max_bytes must be 0 or greater")
        if typing_refresh_seconds <= 0:
            raise ValueError("typing_refresh_seconds must be greater than 0")
        self._queue_name = normalized_queue_name
        self._access_token = access_token
        self._phone_number_id = normalized_phone_number_id
        self._graph_api_base_url = graph_api_base_url.rstrip("/")
        self._thread_prefix = thread_prefix.rstrip("/") or ".threads/whatsapp"
        self._media_storage_prefix = (
            media_storage_prefix.strip().strip("/") or ".threads/whatsapp-media"
        )
        self._dry_run = dry_run
        self._send_read_receipts = send_read_receipts
        self._send_typing_indicators = send_typing_indicators
        self._typing_refresh_seconds = typing_refresh_seconds
        self._inbound_media_max_bytes = inbound_media_max_bytes
        self._allowed_phone_numbers = _allowed_phone_numbers_from_value(
            allowed_phone_numbers
        )
        self._receive_from_http = receive_from_http
        self._http_session: ClientSession | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._pending_turns_by_message_id: dict[str, _PendingWhatsAppTurn] = {}
        self._active_turns_by_turn_id: dict[str, _ActiveWhatsAppTurn] = {}
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    def _default_thread_dir_fallback_name(self) -> str:
        return "whatsapp"

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
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        if not self._receive_from_http:
            await self._room.queues.open(name=self._queue_name)
            self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info("whatsapp_channel_started transport=%s", self.transport)

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
            self._active_turns_by_turn_id[data.turn_id] = _ActiveWhatsAppTurn(
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
            pending.response.set_result(_WhatsAppTurnResponse(text=data.error.message))
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
            source_key = _whatsapp_outbound_media_source_key(
                kind="file-url",
                value=data.url,
            )
            if source_key is not None and source_key in active.media_keys:
                logger.info(
                    "whatsapp_duplicate_media_skipped source_key=%s",
                    source_key,
                )
                return
            media = await self._whatsapp_media_for_agent_file_url(url=data.url)
            if media is not None:
                _append_unique_whatsapp_media(
                    active=active,
                    media=media,
                    source_key=source_key,
                )
            return

        if isinstance(data, AgentImageGenerationCompleted):
            active = self._active_turns_by_turn_id.get(data.turn_id)
            if active is None:
                return
            for image in data.images:
                source_key = _whatsapp_outbound_media_source_key(
                    kind="generated-image-uri",
                    value=image.uri or "",
                )
                if source_key is not None and source_key in active.media_keys:
                    logger.info(
                        "whatsapp_duplicate_media_skipped source_key=%s",
                        source_key,
                    )
                    continue
                media = await self._whatsapp_media_for_generated_image(image=image)
                if media is not None:
                    _append_unique_whatsapp_media(
                        active=active,
                        media=media,
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
            active.response.set_result(_WhatsAppTurnResponse(text=data.error.message))
            return
        active.response.set_result(
            _WhatsAppTurnResponse(
                text="".join(active.text_parts).strip(),
                media=tuple(active.media),
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
                    logger.debug("stopping WhatsApp receive loop after room close")
                    return
                logger.exception(
                    "whatsapp_queue_receive_failed queue=%s", self._queue_name
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
            webhook_data = _queue_json_from_message(queued_message)
        except ValueError as exc:
            logger.warning("whatsapp_queue_message_ignored error=%s", exc)
            return

        try:
            status_events = _parse_whatsapp_status_events_from_data(webhook_data)
            whatsapp_messages = _parse_whatsapp_inbound_messages_from_data(webhook_data)
        except ValueError as exc:
            logger.warning("whatsapp_queue_message_ignored error=%s", exc)
            return

        for status_event in status_events:
            if not self._is_phone_number_allowed(status_event.recipient_id):
                logger.warning(
                    "whatsapp_status_event_rejected_not_allowlisted "
                    "recipient=%s message_id=%s",
                    status_event.recipient_id,
                    status_event.message_id,
                )
                continue
            await self._handle_whatsapp_status_event(status_event)

        if len(whatsapp_messages) == 0:
            if len(status_events) == 0:
                logger.debug("whatsapp_queue_message_ignored no supported events")
            return

        for whatsapp_message in whatsapp_messages:
            if not self._is_phone_number_allowed(whatsapp_message.from_number):
                logger.warning(
                    "whatsapp_message_denied reason=not_allowlisted from=%s "
                    "message_id=%s type=%s num_media=%s "
                    "allowed_from_numbers_hint=MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS=%s",
                    whatsapp_message.from_number,
                    whatsapp_message.message_id,
                    whatsapp_message.message_type,
                    len(whatsapp_message.media),
                    whatsapp_message.from_number,
                )
                continue
            logger.info(
                "whatsapp_message_allowed from=%s message_id=%s type=%s num_media=%s",
                whatsapp_message.from_number,
                whatsapp_message.message_id,
                whatsapp_message.message_type,
                len(whatsapp_message.media),
            )
            conversation_key = whatsapp_message.from_number
            lock = self._conversation_locks.setdefault(
                conversation_key,
                asyncio.Lock(),
            )
            async with lock:
                typing_task = await self._start_typing_indicator(
                    message=whatsapp_message
                )
                try:
                    response = await self._send_whatsapp_turn(message=whatsapp_message)
                    text_chunks = (
                        whatsapp_chunks(response.text)
                        if response.text.strip() != "" or len(response.media) == 0
                        else []
                    )
                    for chunk in text_chunks:
                        await self._send_whatsapp_message(
                            to_number=whatsapp_message.from_number,
                            body=chunk,
                        )
                    for media in response.media:
                        send_result = await self._send_whatsapp_media_message(
                            to_number=whatsapp_message.from_number,
                            media=media,
                        )
                        await self._index_sent_whatsapp_media(
                            to_number=whatsapp_message.from_number,
                            media=media,
                            send_result=send_result,
                        )
                finally:
                    await self._stop_typing_indicator(typing_task)

    async def _send_whatsapp_turn(
        self,
        *,
        message: WhatsAppInboundMessage,
    ) -> _WhatsAppTurnResponse:
        participant_attributes = {
            "name": message.sender_name,
            "role": "user",
            "whatsapp.from": message.from_number,
            "whatsapp.message_id": message.message_id,
        }
        if message.phone_number_id is not None:
            participant_attributes["whatsapp.phone_number_id"] = message.phone_number_id
        if message.display_phone_number is not None:
            participant_attributes["whatsapp.display_phone_number"] = (
                message.display_phone_number
            )
        if message.message_type != "":
            participant_attributes["whatsapp.message_type"] = message.message_type
        if message.interactive_type is not None:
            participant_attributes["whatsapp.interactive.type"] = (
                message.interactive_type
            )
        if message.interactive_reply_id is not None:
            participant_attributes["whatsapp.interactive.reply_id"] = (
                message.interactive_reply_id
            )
        if message.interactive_reply_title is not None:
            participant_attributes["whatsapp.interactive.reply_title"] = (
                message.interactive_reply_title
            )
        if message.interactive_reply_description is not None:
            participant_attributes["whatsapp.interactive.reply_description"] = (
                message.interactive_reply_description
            )
        if message.reply_to_message_id is not None:
            participant_attributes["whatsapp.reply_to_message_id"] = (
                message.reply_to_message_id
            )

        participant = Participant(
            id=f"whatsapp:{_slug(message.from_number)}",
            attributes=participant_attributes,
        )
        thread_id = self._thread_id_for_conversation(message=message)
        self.bump_thread(path=thread_id, name=message.sender_name)

        response: asyncio.Future[_WhatsAppTurnResponse] = (
            asyncio.get_running_loop().create_future()
        )
        content = await self._turn_content_for_whatsapp_message(message=message)
        turn_start = TurnStart(
            type=AGENT_MESSAGE_TURN_START,
            thread_id=thread_id,
            content=content,
        )
        self._pending_turns_by_message_id[turn_start.message_id] = _PendingWhatsAppTurn(
            message=message,
            response=response,
        )
        self.emit(sender=participant, payload=turn_start)

        try:
            return await asyncio.wait_for(response, timeout=RESPONSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._clear_pending_response(response=response)
            logger.warning(
                "whatsapp_turn_response_timed_out message_id=%s",
                message.message_id,
            )
            return _WhatsAppTurnResponse(
                text="The room agent did not answer before the WhatsApp channel timed out."
            )
        except Exception:
            self._clear_pending_response(response=response)
            logger.exception("whatsapp_turn_failed message_id=%s", message.message_id)
            return _WhatsAppTurnResponse(
                text="The room agent could not answer that message."
            )

    async def _whatsapp_media_for_agent_file_url(
        self,
        *,
        url: str,
    ) -> WhatsAppResponseMedia | None:
        normalized_url = url.strip()
        if is_whatsapp_fetchable_media_url(url=normalized_url):
            return whatsapp_outbound_media_from_url(url=normalized_url)

        data_url = _decode_data_url_payload(normalized_url)
        if data_url is not None:
            data, mime_type = data_url
            if _data_url_is_text_authored(url=normalized_url, mime_type=mime_type):
                logger.warning(
                    "whatsapp_media_data_url_ignored_text_authored mime_type=%s",
                    mime_type,
                )
                return None
            return await self._whatsapp_media_id_for_outbound_bytes(
                data=data,
                mime_type=mime_type,
                filename="whatsapp-file",
            )

        storage_path = room_storage_path_from_agent_file_url(url=normalized_url)
        if storage_path is None:
            logger.warning("whatsapp_media_url_ignored url=%s", normalized_url)
            return None

        uploaded_media = await self._whatsapp_decoded_room_file_media(
            storage_path=storage_path,
        )
        if uploaded_media is not None:
            return uploaded_media

        try:
            media_url = await self._room.storage.download_url(path=storage_path)
        except Exception:
            logger.exception(
                "whatsapp_media_download_url_failed path=%s",
                storage_path,
            )
            return None

        if not is_whatsapp_fetchable_media_url(url=media_url):
            logger.warning(
                "whatsapp_media_download_url_ignored path=%s url=%s",
                storage_path,
                media_url,
            )
            return None
        return whatsapp_outbound_media_from_url(
            url=media_url,
            source_url=storage_path,
            storage_path=storage_path,
        )

    async def _whatsapp_decoded_room_file_media(
        self,
        *,
        storage_path: str,
    ) -> WhatsAppOutboundMediaId | None:
        try:
            content = await self._room.storage.download(path=storage_path)
        except Exception:
            return None

        decoded = _whatsapp_decoded_file_content_data(
            data=content.data,
            mime_type=content.mime_type,
        )
        if decoded is None:
            return None

        data, decoded_mime_type = decoded
        return await self._whatsapp_media_id_for_outbound_bytes(
            data=data,
            mime_type=decoded_mime_type or content.mime_type,
            filename=content.name or PurePosixPath(storage_path).name,
        )

    async def _whatsapp_media_for_generated_image(
        self,
        *,
        image: AgentGeneratedImage,
    ) -> WhatsAppOutboundMediaId | WhatsAppOutboundMedia | None:
        uri = (image.uri or "").strip()
        if uri == "":
            return None

        data_url = _decode_data_url_payload(uri)
        if data_url is not None:
            data, mime_type = data_url
            resolved_mime_type = mime_type or image.mime_type
            if _data_url_is_text_authored(url=uri, mime_type=resolved_mime_type):
                logger.warning(
                    "whatsapp_generated_image_data_url_ignored_text_authored "
                    "mime_type=%s",
                    resolved_mime_type,
                )
                return None
            return await self._whatsapp_media_id_for_outbound_bytes(
                data=data,
                mime_type=resolved_mime_type,
                filename=_whatsapp_generated_image_name(
                    mime_type=resolved_mime_type,
                ),
            )

        try:
            record = await ImageDatasetClient(self._room.datasets).read_record_from_uri(
                uri,
                fallback_mime_type=image.mime_type,
            )
        except Exception:
            logger.exception("whatsapp_generated_image_download_failed uri=%s", uri)
            return None

        if record is not None:
            return await self._whatsapp_media_id_for_outbound_bytes(
                data=record.data,
                mime_type=record.mime_type,
                filename=_whatsapp_generated_image_name(mime_type=record.mime_type),
            )

        if is_whatsapp_fetchable_media_url(url=uri):
            downloaded = await self._download_whatsapp_generated_http_media_url(
                url=uri,
                fallback_mime_type=image.mime_type,
            )
            if downloaded is None:
                return None
            data, mime_type = downloaded
            return await self._whatsapp_media_id_for_outbound_bytes(
                data=data,
                mime_type=mime_type or image.mime_type,
                filename=_whatsapp_generated_image_name(mime_type=mime_type),
            )

        logger.warning("whatsapp_generated_image_ignored uri=%s", uri)
        return None

    async def _download_whatsapp_generated_http_media_url(
        self,
        *,
        url: str,
        fallback_mime_type: str | None,
    ) -> tuple[bytes, str | None] | None:
        try:
            async with new_client_session() as http_session:
                async with http_session.get(url) as response:
                    if response.status >= 400:
                        response_text = await response.text()
                        logger.error(
                            "whatsapp_generated_image_http_download_failed "
                            "status=%s url=%s body=%s",
                            response.status,
                            url,
                            response_text[:500],
                        )
                        response.raise_for_status()
                    mime_type = (
                        response.headers.get("content-type") or fallback_mime_type
                    )
                    if mime_type is not None:
                        mime_type = mime_type.split(";", 1)[0].strip()
                    media_limit = (
                        whatsapp_media_size_limit_bytes(mime_type=mime_type)
                        if mime_type is not None
                        else None
                    )
                    content_length = _int_value(response.headers.get("content-length"))
                    self._raise_if_media_too_large(
                        media_id=url,
                        size_bytes=content_length,
                        max_bytes=media_limit,
                        source="content-length",
                    )
                    data = await self._read_response_bytes(
                        response=response,
                        media_id=url,
                        max_bytes=media_limit,
                    )
                    return data, mime_type or fallback_mime_type
        except WhatsAppMediaTooLargeError as exc:
            logger.warning(
                "whatsapp_generated_image_http_download_too_large "
                "url=%s size=%s max=%s",
                exc.media_id,
                exc.size_bytes,
                exc.max_bytes,
            )
            return None
        except Exception:
            logger.exception(
                "whatsapp_generated_image_http_download_failed url=%s", url
            )
            return None

    async def _whatsapp_media_id_for_outbound_bytes(
        self,
        *,
        data: bytes,
        mime_type: str | None,
        filename: str,
    ) -> WhatsAppOutboundMediaId | None:
        normalized_mime_type = (mime_type or "application/octet-stream").strip()
        resolved_filename = filename.strip() or "whatsapp-file"
        extension = mimetypes.guess_extension(normalized_mime_type) or ""
        if PurePosixPath(resolved_filename).suffix == "" and extension != "":
            resolved_filename = f"{resolved_filename}{extension}"
        try:
            media_id = await self.upload_media(
                data=data,
                mime_type=normalized_mime_type,
                filename=resolved_filename,
            )
        except Exception:
            digest = hashlib.sha256(data).hexdigest()[:16]
            logger.exception(
                "whatsapp_outbound_media_upload_failed digest=%s mime_type=%s",
                digest,
                normalized_mime_type,
            )
            return None

        kind = whatsapp_media_kind_for_mime_type(mime_type=normalized_mime_type)
        storage_path = await self._store_outbound_media_bytes(
            data=data,
            mime_type=normalized_mime_type,
            filename=resolved_filename,
            media_id=media_id,
        )
        return WhatsAppOutboundMediaId(
            media_id=media_id,
            kind=kind,
            filename=resolved_filename if kind == "document" else None,
            storage_path=storage_path,
        )

    async def _turn_content_for_whatsapp_message(
        self,
        *,
        message: WhatsAppInboundMessage,
    ) -> list[AgentTextContent | AgentFileContent]:
        content: list[AgentTextContent | AgentFileContent] = [
            AgentTextContent(type="text", text=message.body),
        ]
        content.extend(await self._agent_content_for_referenced_media(message=message))
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
        message: WhatsAppInboundMessage,
        media: WhatsAppInboundMedia,
    ) -> AgentFileContent | AgentTextContent | None:
        mime_type = media.mime_type
        name = media.filename or self._default_inbound_media_filename(
            media=media,
            mime_type=mime_type,
        )
        path = self._inbound_media_storage_path(
            message=message,
            media=media,
            filename=name,
            mime_type=mime_type,
        )
        if (await self._room_storage_exists(path=path)) is True:
            await self._write_message_media_index(
                from_number=message.from_number,
                message_id=message.message_id,
                files=(
                    {
                        "path": path,
                        "name": name,
                    },
                ),
            )
            logger.info(
                "whatsapp_inbound_media_reused message_id=%s media_id=%s path=%s",
                message.message_id,
                media.media_id,
                path,
            )
            return AgentFileContent(type="file", url=f"room:///{path}", name=name)

        try:
            downloaded = await self.download_media(
                media_id=media.media_id,
                max_bytes=self._inbound_media_max_bytes,
            )
        except WhatsAppMediaTooLargeError as exc:
            logger.warning(
                "whatsapp_inbound_media_skipped_oversize media_id=%s "
                "size_bytes=%s max_bytes=%s source=%s",
                exc.media_id,
                exc.size_bytes,
                exc.max_bytes,
                exc.source,
            )
            return AgentTextContent(
                type="text",
                text=(
                    f"WhatsApp {media.kind} attachment {media.media_id} "
                    f"was not attached because it is {exc.size_bytes} bytes, "
                    f"which exceeds the configured inbound media limit of "
                    f"{exc.max_bytes} bytes."
                ),
            )
        except Exception:
            logger.exception(
                "whatsapp_inbound_media_download_failed media_id=%s",
                media.media_id,
            )
            return None

        mime_type = downloaded.mime_type or media.mime_type
        name = media.filename or self._default_inbound_media_filename(
            media=media,
            mime_type=mime_type,
        )
        path = self._inbound_media_storage_path(
            message=message,
            media=media,
            filename=name,
            mime_type=mime_type,
        )
        try:
            await self._room.storage.upload(
                path=path,
                data=downloaded.data,
                overwrite=True,
                name=name,
                mime_type=mime_type,
            )
        except Exception:
            logger.exception(
                "whatsapp_inbound_media_upload_failed media_id=%s path=%s",
                media.media_id,
                path,
            )
            return None
        await self._write_message_media_index(
            from_number=message.from_number,
            message_id=message.message_id,
            files=(
                {
                    "path": path,
                    "name": name,
                },
            ),
        )
        return AgentFileContent(type="file", url=f"room:///{path}", name=name)

    async def _agent_content_for_referenced_media(
        self,
        *,
        message: WhatsAppInboundMessage,
    ) -> list[AgentFileContent]:
        reply_to_message_id = (message.reply_to_message_id or "").strip()
        if reply_to_message_id == "":
            return []

        files = await self._read_message_media_index(
            from_number=message.from_number,
            message_id=reply_to_message_id,
        )
        if len(files) == 0:
            logger.info(
                "whatsapp_referenced_media_not_found from=%s message_id=%s",
                message.from_number,
                reply_to_message_id,
            )
            return []

        content: list[AgentFileContent] = []
        for file_info in files:
            path = str(file_info.get("path") or "").strip()
            if path == "":
                continue
            exists = await self._room_storage_exists(path=path)
            if exists is False:
                logger.warning(
                    "whatsapp_referenced_media_missing path=%s message_id=%s",
                    path,
                    reply_to_message_id,
                )
                continue
            name = str(file_info.get("name") or PurePosixPath(path).name).strip()
            content.append(
                AgentFileContent(
                    type="file",
                    url=f"room:///{path}",
                    name=name or PurePosixPath(path).name,
                )
            )
        return content

    async def _store_outbound_media_bytes(
        self,
        *,
        data: bytes,
        mime_type: str | None,
        filename: str,
        media_id: str,
    ) -> str | None:
        path = self._outbound_media_storage_path(
            media_id=media_id,
            filename=filename,
        )
        try:
            await self._room.storage.upload(
                path=path,
                data=data,
                overwrite=True,
                name=filename,
                mime_type=mime_type,
            )
        except Exception:
            logger.exception(
                "whatsapp_outbound_media_storage_upload_failed media_id=%s path=%s",
                media_id,
                path,
            )
            return None
        return path

    async def _index_sent_whatsapp_media(
        self,
        *,
        to_number: str,
        media: WhatsAppResponseMedia,
        send_result: dict[str, Any] | None,
    ) -> None:
        storage_path = media.storage_path
        if not isinstance(storage_path, str) or storage_path.strip() == "":
            return
        message_id = _sent_whatsapp_message_id(send_result)
        if message_id is None:
            logger.info(
                "whatsapp_sent_media_index_skipped_missing_message_id path=%s",
                storage_path,
            )
            return
        await self._write_message_media_index(
            from_number=to_number,
            message_id=message_id,
            files=(
                {
                    "path": storage_path,
                    "name": PurePosixPath(storage_path).name,
                },
            ),
        )

    async def _write_message_media_index(
        self,
        *,
        from_number: str,
        message_id: str,
        files: Sequence[Mapping[str, object]],
    ) -> None:
        entries = [
            {
                "path": path,
                "name": str(file_info.get("name") or PurePosixPath(path).name),
            }
            for file_info in files
            if (path := str(file_info.get("path") or "").strip()) != ""
        ]
        if len(entries) == 0:
            return

        index_path = self._message_media_index_path(
            from_number=from_number,
            message_id=message_id,
        )
        payload = {
            "version": 1,
            "message_id": message_id,
            "files": entries,
        }
        try:
            await self._room.storage.upload(
                path=index_path,
                data=json.dumps(payload, sort_keys=True).encode("utf-8"),
                overwrite=True,
                name=PurePosixPath(index_path).name,
                mime_type="application/json",
            )
        except Exception:
            logger.exception(
                "whatsapp_message_media_index_upload_failed message_id=%s path=%s",
                message_id,
                index_path,
            )

    async def _read_message_media_index(
        self,
        *,
        from_number: str,
        message_id: str,
    ) -> list[dict[str, object]]:
        index_path = self._message_media_index_path(
            from_number=from_number,
            message_id=message_id,
        )
        try:
            content = await self._room.storage.download(path=index_path)
        except Exception:
            return []

        raw_data = content.data
        if isinstance(raw_data, (bytes, bytearray, memoryview)):
            raw_text = bytes(raw_data).decode("utf-8", errors="replace")
        elif isinstance(raw_data, str):
            raw_text = raw_data
        else:
            return []

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "whatsapp_message_media_index_invalid_json path=%s", index_path
            )
            return []
        if not isinstance(payload, dict):
            return []
        files = payload.get("files")
        if not isinstance(files, list):
            return []
        return [file_info for file_info in files if isinstance(file_info, dict)]

    async def _room_storage_exists(self, *, path: str) -> bool | None:
        try:
            return bool(await self._room.storage.exists(path=path))
        except Exception:
            logger.exception("whatsapp_media_storage_exists_failed path=%s", path)
            return None

    def _message_media_index_path(self, *, from_number: str, message_id: str) -> str:
        return (
            f"{self._media_storage_prefix}/"
            f"{_slug(from_number)}/"
            f".message-index/{_slug(message_id)}.json"
        )

    def _outbound_media_storage_path(self, *, media_id: str, filename: str) -> str:
        return (
            f"{self._media_storage_prefix}/outbound/{_slug(media_id)}-{_slug(filename)}"
        )

    def _inbound_media_storage_path(
        self,
        *,
        message: WhatsAppInboundMessage,
        media: WhatsAppInboundMedia,
        filename: str,
        mime_type: str | None,
    ) -> str:
        extension = PurePosixPath(filename).suffix
        if extension == "" and mime_type is not None:
            extension = mimetypes.guess_extension(mime_type) or ""
        return (
            f"{self._media_storage_prefix}/"
            f"{_slug(message.from_number)}/"
            f"{_slug(message.message_id)}-{_slug(media.media_id)}{extension}"
        )

    @staticmethod
    def _default_inbound_media_filename(
        *,
        media: WhatsAppInboundMedia,
        mime_type: str | None,
    ) -> str:
        extension = mimetypes.guess_extension(mime_type or "") or ""
        return f"{media.kind}-{_slug(media.media_id)}{extension}"

    async def _handle_whatsapp_status_event(
        self,
        status_event: WhatsAppStatusEvent,
    ) -> None:
        logger.info(
            "whatsapp_status_event message_id=%s status=%s recipient=%s",
            status_event.message_id,
            status_event.status,
            status_event.recipient_id,
        )

    async def _start_typing_indicator(
        self,
        *,
        message: WhatsAppInboundMessage,
    ) -> asyncio.Task[None] | None:
        if not self._send_read_receipts:
            return None
        await self._send_read_receipt(
            message=message,
            typing=self._send_typing_indicators,
        )
        if not self._send_typing_indicators:
            return None
        return asyncio.create_task(self._refresh_typing_indicator(message=message))

    async def _refresh_typing_indicator(
        self,
        *,
        message: WhatsAppInboundMessage,
    ) -> None:
        while True:
            await asyncio.sleep(self._typing_refresh_seconds)
            await self._send_read_receipt(message=message, typing=True)

    async def _stop_typing_indicator(
        self,
        typing_task: asyncio.Task[None] | None,
    ) -> None:
        if typing_task is None:
            return
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    async def _send_read_receipt(
        self,
        *,
        message: WhatsAppInboundMessage,
        typing: bool,
    ) -> None:
        try:
            await self.send_read_receipt(
                message_id=message.message_id,
                typing=typing,
            )
        except Exception:
            logger.warning(
                "whatsapp_read_receipt_failed message_id=%s",
                message.message_id,
                exc_info=True,
            )

    def _require_http_session(self) -> ClientSession:
        http_session = self._http_session
        if http_session is None:
            raise RuntimeError("WhatsApp HTTP session is not open.")
        return http_session

    def _graph_url(self, path: str) -> str:
        return f"{self._graph_api_base_url}/{path.strip('/')}"

    @staticmethod
    async def _read_json_response(response: Any) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            response_data = await response.json(content_type=None)
            if isinstance(response_data, dict):
                return response_data
        return {}

    async def _request_graph_json(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, object] | None = None,
        data: Any = None,
    ) -> dict[str, Any]:
        http_session = self._require_http_session()
        kwargs: dict[str, object] = {}
        if json_payload is not None:
            kwargs["json"] = dict(json_payload)
        if data is not None:
            kwargs["data"] = data
        async with http_session.request(
            method.upper(), self._graph_url(path), **kwargs
        ) as response:
            if response.status < 400:
                return await self._read_json_response(response)
            response_text = await response.text()
            logger.error(
                "whatsapp_graph_request_failed method=%s path=%s status=%s body=%s",
                method.upper(),
                path,
                response.status,
                response_text[:500],
            )
            response.raise_for_status()
        return {}

    async def get_media_info(self, *, media_id: str) -> dict[str, Any]:
        normalized_media_id = media_id.strip()
        if normalized_media_id == "":
            raise ValueError("media_id must not be empty")
        return await self._request_graph_json(method="get", path=normalized_media_id)

    async def get_media_url(self, *, media_id: str) -> str:
        info = await self.get_media_info(media_id=media_id)
        media_url = info.get("url")
        if not isinstance(media_url, str) or media_url.strip() == "":
            raise RuntimeError("WhatsApp media info response did not include a URL.")
        return media_url

    async def download_media(
        self,
        *,
        media_id: str,
        max_bytes: int | None = None,
    ) -> WhatsAppDownloadedMedia:
        normalized_media_id = media_id.strip()
        if normalized_media_id == "":
            raise ValueError("media_id must not be empty")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be 0 or greater")
        info = await self.get_media_info(media_id=normalized_media_id)
        media_url = info.get("url")
        if not isinstance(media_url, str) or media_url.strip() == "":
            raise RuntimeError("WhatsApp media info response did not include a URL.")
        file_size = _int_value(info.get("file_size"))
        self._raise_if_media_too_large(
            media_id=normalized_media_id,
            size_bytes=file_size,
            max_bytes=max_bytes,
            source="metadata",
        )

        http_session = self._require_http_session()
        async with http_session.get(media_url) as response:
            if response.status < 400:
                content_length = _int_value(response.headers.get("content-length"))
                self._raise_if_media_too_large(
                    media_id=normalized_media_id,
                    size_bytes=content_length,
                    max_bytes=max_bytes,
                    source="content-length",
                )
                data = await self._read_response_bytes(
                    response=response,
                    media_id=normalized_media_id,
                    max_bytes=max_bytes,
                )
                return WhatsAppDownloadedMedia(
                    media_id=normalized_media_id,
                    data=data,
                    mime_type=_string_value(info.get("mime_type"))
                    or response.headers.get("content-type"),
                    sha256=_string_value(info.get("sha256")) or None,
                    file_size=file_size,
                    url=media_url,
                )
            response_text = await response.text()
            logger.error(
                "whatsapp_media_download_failed media_id=%s status=%s body=%s",
                normalized_media_id,
                response.status,
                response_text[:500],
            )
            response.raise_for_status()
        raise RuntimeError("WhatsApp media download failed.")

    @staticmethod
    def _raise_if_media_too_large(
        *,
        media_id: str,
        size_bytes: int | None,
        max_bytes: int | None,
        source: str,
    ) -> None:
        if max_bytes is None or size_bytes is None:
            return
        if size_bytes > max_bytes:
            raise WhatsAppMediaTooLargeError(
                media_id=media_id,
                size_bytes=size_bytes,
                max_bytes=max_bytes,
                source=source,
            )

    async def _read_response_bytes(
        self,
        *,
        response: ClientResponse,
        media_id: str,
        max_bytes: int | None,
    ) -> bytes:
        if max_bytes is not None:
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                self._raise_if_media_too_large(
                    media_id=media_id,
                    size_bytes=len(body),
                    max_bytes=max_bytes,
                    source="response-body",
                )
            return bytes(body)

        data = await response.read()
        self._raise_if_media_too_large(
            media_id=media_id,
            size_bytes=len(data),
            max_bytes=max_bytes,
            source="response-body",
        )
        return data

    async def upload_media(
        self,
        *,
        data: bytes,
        mime_type: str,
        filename: str | None = None,
    ) -> str:
        normalized_mime_type = mime_type.strip()
        if normalized_mime_type == "":
            raise ValueError("mime_type must not be empty")
        if not validate_whatsapp_media_size(
            size_bytes=len(data),
            mime_type=normalized_mime_type,
        ):
            limit = whatsapp_media_size_limit_bytes(mime_type=normalized_mime_type)
            raise ValueError(
                f"WhatsApp media exceeds size limit for {normalized_mime_type}: {limit}"
            )

        resolved_filename = (
            filename or f"upload{mimetypes.guess_extension(mime_type) or ''}"
        )
        form = FormData()
        form.add_field("messaging_product", "whatsapp")
        form.add_field("type", normalized_mime_type)
        form.add_field(
            "file",
            data,
            filename=resolved_filename,
            content_type=normalized_mime_type,
        )
        response_data = await self._request_graph_json(
            method="post",
            path=f"{self._phone_number_id}/media",
            data=form,
        )
        media_id = response_data.get("id")
        if not isinstance(media_id, str) or media_id.strip() == "":
            raise RuntimeError("WhatsApp media upload response did not include an ID.")
        return media_id

    async def delete_media(self, *, media_id: str) -> dict[str, Any]:
        normalized_media_id = media_id.strip()
        if normalized_media_id == "":
            raise ValueError("media_id must not be empty")
        return await self._request_graph_json(method="delete", path=normalized_media_id)

    async def _send_whatsapp_message(self, *, to_number: str, body: str) -> None:
        await self.send_text_message(to_number=to_number, body=body)

    async def _send_whatsapp_media_message(
        self,
        *,
        to_number: str,
        media: WhatsAppResponseMedia,
    ) -> dict[str, Any] | None:
        if isinstance(media, WhatsAppOutboundMediaId):
            return await self.send_media_id_message(to_number=to_number, media=media)
        return await self.send_media_message(to_number=to_number, media=media)

    async def send_text_message(
        self,
        *,
        to_number: str,
        body: str,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_text_message_payload(to_number=to_number, body=body),
        )

    async def send_media_message(
        self,
        *,
        to_number: str,
        media: WhatsAppOutboundMedia,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_media_message_payload(to_number=to_number, media=media),
        )

    async def send_media_id_message(
        self,
        *,
        to_number: str,
        media: WhatsAppOutboundMediaId,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_media_id_message_payload(to_number=to_number, media=media),
        )

    async def send_interactive_buttons(
        self,
        *,
        to_number: str,
        body: str,
        buttons: Sequence[WhatsAppInteractiveButton | Mapping[str, str]],
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_interactive_button_message_payload(
                to_number=to_number,
                body=body,
                buttons=buttons,
                header_text=header_text,
                footer_text=footer_text,
            ),
        )

    async def send_interactive_list(
        self,
        *,
        to_number: str,
        body: str,
        button_text: str,
        sections: Sequence[WhatsAppInteractiveListSection | Mapping[str, Any]],
        header_text: str | None = None,
        footer_text: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_interactive_list_message_payload(
                to_number=to_number,
                body=body,
                button_text=button_text,
                sections=sections,
                header_text=header_text,
                footer_text=footer_text,
            ),
        )

    async def send_template_message(
        self,
        *,
        to_number: str,
        template_name: str,
        language_code: str,
        components: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=to_number,
            payload=whatsapp_template_message_payload(
                to_number=to_number,
                template_name=template_name,
                language_code=language_code,
                components=components,
            ),
        )

    async def send_read_receipt(
        self,
        *,
        message_id: str,
        typing: bool = False,
    ) -> dict[str, Any] | None:
        return await self._send_whatsapp_payload(
            log_target=message_id,
            payload=whatsapp_read_receipt_payload(
                message_id=message_id,
                typing=typing,
            ),
        )

    async def _send_whatsapp_payload(
        self,
        *,
        log_target: str,
        payload: dict[str, object],
    ) -> dict[str, Any] | None:
        to_number = payload.get("to")
        if isinstance(to_number, str) and not self._is_phone_number_allowed(to_number):
            raise PermissionError(
                f"WhatsApp number {to_number} is not in the allowed phone list."
            )
        if self._dry_run:
            logger.info(
                "whatsapp_dry_run_message target=%s payload=%s",
                log_target,
                payload,
            )
            return None

        http_session = self._require_http_session()

        async with http_session.post(
            self._graph_url(f"{self._phone_number_id}/messages"),
            json=payload,
        ) as response:
            if response.status < 400:
                logger.info("whatsapp_message_sent target=%s", log_target)
                return await self._read_json_response(response)
            response_text = await response.text()
            logger.error(
                "whatsapp_message_send_failed status=%s body=%s",
                response.status,
                response_text[:500],
            )
            response.raise_for_status()

    def _is_phone_number_allowed(self, phone_number: str | None) -> bool:
        if self._allowed_phone_numbers is None:
            return True
        if phone_number is None:
            return False
        normalized_phone_number = _normalize_whatsapp_phone_number(phone_number)
        return normalized_phone_number in self._allowed_phone_numbers

    def _thread_id_for_conversation(self, *, message: WhatsAppInboundMessage) -> str:
        phone_number_id = message.phone_number_id or self._phone_number_id
        uses_dataset_thread_url = (
            self._thread_url_scheme == "dataset://"
            or self._thread_prefix.startswith("dataset://")
        )
        extension = "" if uses_dataset_thread_url else self._thread_path_extension
        path = (
            f"{self._thread_prefix}/"
            f"{_slug(phone_number_id)}-{_slug(message.from_number)}{extension}"
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
        response: asyncio.Future[_WhatsAppTurnResponse],
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
            logger.error("whatsapp_message_task_failed", exc_info=exc)


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
) -> WhatsAppChannel:
    return WhatsAppChannel(
        room=room,
        access_token=required_env("WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=required_env("WHATSAPP_PHONE_NUMBER_ID"),
        threading_mode=threading_mode,
        thread_dir=thread_dir,
        thread_url_scheme=thread_url_scheme,
        thread_path_extension=thread_path_extension,
        thread_list_path=thread_list_path,
        llm_adapter=llm_adapter,
        media_storage_prefix=os.getenv(
            "MESHAGENT_WHATSAPP_MEDIA_STORAGE_PREFIX",
            MEDIA_STORAGE_PREFIX,
        ),
        inbound_media_max_bytes=env_int(
            "MESHAGENT_WHATSAPP_INBOUND_MEDIA_MAX_BYTES",
            default=DEFAULT_INBOUND_MEDIA_MAX_BYTES,
        ),
        allowed_phone_numbers=os.getenv("MESHAGENT_WHATSAPP_ALLOWED_FROM_NUMBERS"),
        dry_run=env_flag("MESHAGENT_WHATSAPP_DRY_RUN"),
        send_read_receipts=env_flag(
            "MESHAGENT_WHATSAPP_SEND_READ_RECEIPTS",
            default=True,
        ),
        send_typing_indicators=env_flag(
            "MESHAGENT_WHATSAPP_SEND_TYPING_INDICATOR",
            default=True,
        ),
        typing_refresh_seconds=float(
            os.getenv(
                "MESHAGENT_WHATSAPP_TYPING_REFRESH_SECONDS",
                str(TYPING_REFRESH_SECONDS),
            )
        ),
        receive_from_http=receive_from_http,
    )
