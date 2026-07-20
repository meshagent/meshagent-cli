from __future__ import annotations

import json
import re
from builtins import print as stdout_print
from dataclasses import dataclass, field
from typing import Any, Literal

from rich import print
from rich.table import Table

from meshagent.agents.messages import (
    AgentImageGenerationCompleted,
    AgentMessage,
    AgentTextContentDelta,
    AgentTextContentEnded,
    AgentThreadStatus,
    AgentToolCallEnded,
    AgentToolCallStarted,
    TurnEnded,
    TurnStart,
    TurnStartAccepted,
)
from meshagent.agents.thread_storage import ThreadListPage
from meshagent.api import RoomClient
from meshagent.api.agent_content import AgentFileContent, AgentTextContent

ThreadInspectOutput = Literal["json", "table", "text"]


async def load_thread_agent_messages(
    *,
    room: RoomClient,
    thread_id: str,
):
    normalized_path = thread_id.strip()
    if normalized_path == "" or normalized_path.startswith("tmp://"):
        return []

    if normalized_path.startswith("dataset://"):
        from meshagent.agents.dataset_thread_storage import DatasetThreadStorage

        storage = DatasetThreadStorage(room=room, path=normalized_path)
    else:
        return []
    await storage.start()
    try:
        await storage.wait_until_ready()
        return storage.agent_messages()
    finally:
        await storage.stop()


def print_thread_list(
    *,
    page: ThreadListPage,
    title: str,
    output: ThreadInspectOutput,
) -> None:
    if output == "json":
        print_json(
            [
                {
                    "name": entry.name,
                    "path": entry.path,
                    "created_at": entry.created_at,
                    "modified_at": entry.modified_at,
                }
                for entry in page.threads
            ]
        )
        return
    if page.total == 0:
        print("No threads found.")
        return
    if output == "table":
        table = Table(title=title)
        table.add_column("Name")
        table.add_column("Path")
        table.add_column("Modified")
        for entry in page.threads:
            table.add_row(entry.name, entry.path, entry.modified_at)
        print(table)
        return
    for entry in page.threads:
        print(f"{entry.modified_at}  {entry.name}\n  {entry.path}")


def print_thread_messages(
    *,
    messages: list[AgentMessage],
    output: ThreadInspectOutput,
) -> None:
    if output == "json":
        print_json([message.model_dump(mode="json") for message in messages])
        return
    rows = coalesced_thread_rows(messages)
    if output == "table":
        table = Table(title="Thread messages")
        table.add_column("Role")
        table.add_column("Turn")
        table.add_column("Text")
        for row in rows:
            table.add_row(row.role, row.turn_id or "", row.text)
        print(table)
        return
    for row in rows:
        prefix = row.role
        if row.turn_id is not None:
            prefix = f"{prefix} [{row.turn_id}]"
        print(f"{prefix}: {row.text}")


def grep_thread_messages(
    *,
    messages: list[AgentMessage],
    pattern: str,
    output: ThreadInspectOutput,
    ignore_case: bool = True,
) -> None:
    flags = re.IGNORECASE if ignore_case else 0
    regex = re.compile(pattern, flags)
    rows = [row for row in coalesced_thread_rows(messages) if regex.search(row.text)]
    if output == "json":
        print_json(
            [
                {
                    "role": row.role,
                    "turn_id": row.turn_id,
                    "text": row.text,
                }
                for row in rows
            ]
        )
        return
    if output == "table":
        table = Table(title=f"Thread grep: {pattern}")
        table.add_column("Role")
        table.add_column("Turn")
        table.add_column("Text")
        for row in rows:
            table.add_row(row.role, row.turn_id or "", row.text)
        print(table)
        return
    for row in rows:
        prefix = row.role
        if row.turn_id is not None:
            prefix = f"{prefix} [{row.turn_id}]"
        print(f"{prefix}: {row.text}")


def print_json(value: Any) -> None:
    stdout_print(json.dumps(value, indent=2, ensure_ascii=False))


@dataclass(slots=True)
class CoalescedThreadRow:
    role: str
    text: str
    turn_id: str | None = None


@dataclass(slots=True)
class _TextAccumulator:
    role: str
    turn_id: str | None
    parts: list[str] = field(default_factory=list)

    def append(self, text: str) -> None:
        if text != "":
            self.parts.append(text)

    def row(self) -> CoalescedThreadRow | None:
        text = "".join(self.parts).strip()
        if text == "":
            return None
        return CoalescedThreadRow(role=self.role, turn_id=self.turn_id, text=text)


def coalesced_thread_rows(messages: list[AgentMessage]) -> list[CoalescedThreadRow]:
    rows: list[CoalescedThreadRow] = []
    active_text: dict[tuple[str | None, str], _TextAccumulator] = {}
    rendered_input_source_message_ids: set[str] = set()
    rendered_image_item_ids: set[tuple[str | None, str]] = set()
    for message in messages:
        if isinstance(message, TurnStart):
            text = _input_content_text(message.content).strip()
            if text != "":
                rows.append(
                    CoalescedThreadRow(
                        role=message.sender_name or "user",
                        turn_id=message.turn_id,
                        text=text,
                    )
                )
                rendered_input_source_message_ids.add(message.message_id)
            continue
        if isinstance(message, TurnStartAccepted):
            text = _input_content_text(message.content).strip()
            if (
                text != ""
                and message.source_message_id not in rendered_input_source_message_ids
            ):
                rows.append(
                    CoalescedThreadRow(
                        role=message.sender_name or "user",
                        turn_id=message.turn_id,
                        text=text,
                    )
                )
                rendered_input_source_message_ids.add(message.source_message_id)
            continue
        if isinstance(message, AgentTextContentDelta):
            key = (message.turn_id, message.item_id)
            accumulator = active_text.get(key)
            if accumulator is None:
                accumulator = _TextAccumulator(
                    role=message.sender_name or "assistant",
                    turn_id=message.turn_id,
                )
                active_text[key] = accumulator
            accumulator.append(message.text)
            continue
        if isinstance(message, AgentTextContentEnded):
            accumulator = active_text.pop((message.turn_id, message.item_id), None)
            if accumulator is not None:
                row = accumulator.row()
                if row is not None:
                    rows.append(row)
            continue
        if isinstance(message, AgentImageGenerationCompleted):
            item_key = (message.turn_id, message.item_id)
            if item_key not in rendered_image_item_ids:
                rendered_image_item_ids.add(item_key)
                rows.extend(_image_generation_rows(message))
            continue
        if isinstance(message, AgentToolCallStarted):
            label = f"{message.toolkit}.{message.tool}"
            if message.arguments is not None:
                label = f"{label} {json.dumps(message.arguments, ensure_ascii=False)}"
            rows.append(
                CoalescedThreadRow(
                    role="tool",
                    turn_id=message.turn_id,
                    text=label,
                )
            )
            continue
        if isinstance(message, AgentToolCallEnded):
            if message.error is not None:
                rows.append(
                    CoalescedThreadRow(
                        role="tool",
                        turn_id=message.turn_id,
                        text=f"error: {message.error.message}",
                    )
                )
            continue
        if isinstance(message, AgentThreadStatus):
            if message.status is not None and message.status.strip() != "":
                rows.append(
                    CoalescedThreadRow(
                        role="status",
                        turn_id=message.turn_id,
                        text=message.status.strip(),
                    )
                )
            continue
        if isinstance(message, TurnEnded) and message.error is not None:
            rows.append(
                CoalescedThreadRow(
                    role="error",
                    turn_id=message.turn_id,
                    text=message.error.message,
                )
            )
    rows.extend(
        row
        for accumulator in active_text.values()
        for row in [accumulator.row()]
        if row is not None
    )
    return rows


def _input_content_text(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content:
        if isinstance(item, AgentTextContent):
            parts.append(item.text)
        elif isinstance(item, AgentFileContent):
            parts.append(item.name or item.url)
    return "\n".join(parts)


def _image_generation_rows(
    message: AgentImageGenerationCompleted,
) -> list[CoalescedThreadRow]:
    if len(message.images) == 0:
        return [
            CoalescedThreadRow(
                role=message.sender_name or "assistant",
                turn_id=message.turn_id,
                text="image:",
            )
        ]

    return [
        CoalescedThreadRow(
            role=message.sender_name or "assistant",
            turn_id=message.turn_id,
            text=_image_generation_text(image),
        )
        for image in message.images
    ]


def _image_generation_text(image: Any) -> str:
    parts = ["image:"]
    uri = image.uri if isinstance(image.uri, str) and image.uri.strip() != "" else None
    if uri is not None:
        parts.append(_display_image_uri(uri))
    details = [
        value
        for value in [
            image.mime_type,
            _image_dimensions(image),
            image.status,
        ]
        if isinstance(value, str) and value.strip() != ""
    ]
    if len(details) > 0:
        parts.append(f"({', '.join(details)})")
    return " ".join(parts)


def _display_image_uri(uri: str) -> str:
    if uri.startswith("data:"):
        metadata = uri.split(",", 1)[0]
        return f"{metadata},..."
    return uri


def _image_dimensions(image: Any) -> str | None:
    width = _image_dimension_value(image.width)
    height = _image_dimension_value(image.height)
    if width is None or height is None:
        return None
    return f"{width}x{height}"


def _image_dimension_value(value: Any) -> str | None:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return str(value)
    return None
