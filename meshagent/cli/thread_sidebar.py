from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from meshagent.agents.messages import (
    AGENT_EVENT_THREAD_CREATED,
    AGENT_EVENT_THREAD_DELETED,
    AGENT_EVENT_THREAD_UPDATED,
    AgentThreadListEntry,
    ThreadCreated,
    ThreadDeleted,
    ThreadUpdated,
)
from meshagent.agents.thread_storage import ThreadListEntry, ThreadListEvent


class ThreadSidebar:
    def __init__(
        self,
        *,
        list_threads: Callable[[], Awaitable[list[ThreadListEntry]]],
        subscribe_thread_events: Callable[
            [Callable[[ThreadListEvent], None]], Callable[[], None]
        ]
        | None = None,
        current_thread_path: Callable[[], str | None],
        switch_thread: Callable[[str], Awaitable[None]],
        delete_thread: Callable[[str], Awaitable[None]],
        rename_thread: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._list_threads = list_threads
        self._subscribe_thread_events = subscribe_thread_events
        self._unsubscribe_thread_events: Callable[[], None] | None = None
        self._current_thread_path = current_thread_path
        self._switch_thread = switch_thread
        self._delete_thread = delete_thread
        self._rename_thread = rename_thread
        self._entries: list[ThreadListEntry] = []
        self._selected_index = 0
        self._message: str | None = None
        self._confirm_delete_path: str | None = None
        self._rename_path: str | None = None
        self._rename_value = ""
        self._refresh_task: asyncio.Task[None] | None = None
        self._scroll_offset = 0
        self._last_entry_capacity: int | None = None

    async def start(self) -> None:
        self._refresh_task = asyncio.create_task(self.refresh())
        if self._subscribe_thread_events is not None:
            self._unsubscribe_thread_events = self._subscribe_thread_events(
                self._apply_thread_list_event
            )

    async def close(self) -> None:
        if self._unsubscribe_thread_events is not None:
            self._unsubscribe_thread_events()
        self._unsubscribe_thread_events = None
        tasks = [
            task
            for task in (self._refresh_task,)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if len(tasks) > 0:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None

    async def refresh(self) -> None:
        try:
            self._entries = await self._list_threads()
            self._sync_selection()
            if self._message is not None and self._message.startswith(
                "Unable to load threads:"
            ):
                self._message = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._message = f"Unable to load threads: {exc}"

    def _apply_thread_list_event(self, event: ThreadListEvent) -> None:
        if event.type == "deleted":
            self._entries = [
                entry for entry in self._entries if entry.path != event.path
            ]
            if self._confirm_delete_path == event.path:
                self._confirm_delete_path = None
            if self._rename_path == event.path:
                self._rename_path = None
                self._rename_value = ""
            self._sync_selection()
            return

        entry = event.entry
        if entry is None:
            return
        entries_by_path = {existing.path: existing for existing in self._entries}
        entries_by_path[entry.path] = entry
        self._entries = sort_thread_entries(entries_by_path.values())
        if self._message is not None and self._message.startswith(
            "Thread watch stopped:"
        ):
            self._message = None
        self._sync_selection()

    def _sync_selection(self) -> None:
        current_path = self._current_thread_path()
        if current_path is not None:
            for index, entry in enumerate(self._entries):
                if entry.path == current_path:
                    self._selected_index = index
                    return
        if len(self._entries) == 0:
            self._selected_index = 0
        else:
            self._selected_index = min(self._selected_index, len(self._entries) - 1)

    def render(
        self, focused: bool, *, width: int | None = None, height: int | None = None
    ) -> Any:
        from rich.text import Text

        if not focused:
            self._sync_selection()
        name_width = max((width or 32) - 1, 8)
        text = Text()
        title_style = "bold #7dd3fc" if focused else "bold #9aa5b8"
        text.append("Threads", style=title_style)
        text.append("\n")
        if self._message is not None:
            text.append(self._message, style="#9aa5b8")
            text.append("\n")
        if self._rename_path is not None:
            text.append("Rename: ", style="bold #e5e7eb")
            text.append(self._rename_value or " ", style="#cfd3dc")
            text.append("\n")
        elif self._confirm_delete_path is not None:
            text.append("Backspace again to delete", style="bold #fca5a5")
            text.append("\n")
        if len(self._entries) == 0:
            if self._refresh_task is not None and not self._refresh_task.done():
                text.append("Loading threads...", style="#9aa5b8")
                return text
            text.append("No threads", style="#9aa5b8")
            return text

        current_path = self._current_thread_path()
        entry_capacity = self._entry_capacity(height=height)
        self._last_entry_capacity = entry_capacity
        self._sync_scroll_offset(entry_capacity=entry_capacity)
        visible_entries = self._entries[
            self._scroll_offset : self._scroll_offset + entry_capacity
        ]
        for visible_index, entry in enumerate(visible_entries):
            index = self._scroll_offset + visible_index
            selected = focused and index == self._selected_index
            current = current_path == entry.path
            style = "reverse bold #e5e7eb" if selected else "#cfd3dc"
            if current and not selected:
                style = "#7dd3fc"
            name = " ".join(entry.name.split()) or entry.path
            available_name_width = max(name_width - (2 if current else 0), 8)
            if len(name) > available_name_width:
                name = f"{name[: max(available_name_width - 3, 1)].rstrip()}..."
            text.append(name, style=style)
            if current:
                text.append(" *", style="#7dd3fc")
            text.append("\n")
        return text

    async def handle_key(self, key: str, character: str | None) -> bool:
        if self._rename_path is not None:
            return await self._handle_rename_key(key=key, character=character)
        if key == "up":
            self._move_selection(-1)
            return True
        if key == "down":
            self._move_selection(1)
            return True
        if key == "scroll_up":
            self._scroll_entries(-1)
            return True
        if key == "scroll_down":
            self._scroll_entries(1)
            return True
        if key == "enter":
            await self._open_selected_thread()
            return True
        if key == "backspace":
            await self._confirm_or_delete_selected_thread()
            return True
        if key == "r":
            self._begin_rename_selected_thread()
            return True
        return False

    async def handle_click(self, x: int, y: int) -> bool:
        del x
        visible_entries = self._entries[
            self._scroll_offset : self._scroll_offset
            + (self._last_entry_capacity or len(self._entries))
        ]
        entry_index = y - self._entry_line_offset()
        if entry_index < 0 or entry_index >= len(visible_entries):
            return False
        self._selected_index = min(
            self._scroll_offset + entry_index, len(self._entries) - 1
        )
        self._confirm_delete_path = None
        await self._open_selected_thread()
        return True

    def _entry_line_offset(self) -> int:
        offset = 1
        if self._message is not None:
            offset += 1
        if self._rename_path is not None or self._confirm_delete_path is not None:
            offset += 1
        return offset

    def _entry_capacity(self, *, height: int | None) -> int:
        if height is None:
            return min(len(self._entries), 100)
        return max(1, min(height - self._entry_line_offset(), 100))

    def _sync_scroll_offset(self, *, entry_capacity: int | None = None) -> None:
        if len(self._entries) == 0:
            self._scroll_offset = 0
            return
        capacity = entry_capacity or self._last_entry_capacity or len(self._entries)
        max_offset = max(0, len(self._entries) - capacity)
        self._scroll_offset = max(0, min(self._scroll_offset, max_offset))
        if self._selected_index < self._scroll_offset:
            self._scroll_offset = self._selected_index
        elif self._selected_index >= self._scroll_offset + capacity:
            self._scroll_offset = min(max_offset, self._selected_index - capacity + 1)

    def _scroll_entries(self, delta: int) -> None:
        if len(self._entries) == 0:
            return
        capacity = self._last_entry_capacity or len(self._entries)
        max_offset = max(0, len(self._entries) - capacity)
        self._scroll_offset = max(0, min(max_offset, self._scroll_offset + delta))
        if self._selected_index < self._scroll_offset:
            self._selected_index = self._scroll_offset
        elif self._selected_index >= self._scroll_offset + capacity:
            self._selected_index = self._scroll_offset + capacity - 1

    def _move_selection(self, delta: int) -> None:
        if len(self._entries) == 0:
            return
        self._selected_index = max(
            0,
            min(len(self._entries) - 1, self._selected_index + delta),
        )
        self._confirm_delete_path = None
        self._sync_scroll_offset()

    def _selected_entry(self) -> ThreadListEntry | None:
        if len(self._entries) == 0:
            return None
        if self._selected_index < 0 or self._selected_index >= len(self._entries):
            return None
        return self._entries[self._selected_index]

    async def _open_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._confirm_delete_path = None
        await self._switch_thread(entry.path)
        self._message = None

    async def _confirm_or_delete_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if self._confirm_delete_path != entry.path:
            self._confirm_delete_path = entry.path
            self._message = None
            return
        deleted_current = self._current_thread_path() == entry.path
        await self._delete_thread(entry.path)
        self._confirm_delete_path = None
        self._message = None
        self._entries = [
            existing for existing in self._entries if existing.path != entry.path
        ]
        self._sync_selection()
        next_entry = self._selected_entry()
        if deleted_current and next_entry is not None:
            await self._switch_thread(next_entry.path)

    def _begin_rename_selected_thread(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._rename_path = entry.path
        self._rename_value = entry.name
        self._confirm_delete_path = None

    async def _handle_rename_key(self, *, key: str, character: str | None) -> bool:
        if key == "escape":
            self._rename_path = None
            self._rename_value = ""
            return True
        if key == "enter":
            await self._commit_rename()
            return True
        if key == "backspace":
            self._rename_value = self._rename_value[:-1]
            return True
        if character is not None and character.isprintable():
            self._rename_value += character
            return True
        if key == "space":
            self._rename_value += " "
            return True
        return True

    async def _commit_rename(self) -> None:
        if self._rename_path is None:
            return
        name = " ".join(self._rename_value.split())
        if name == "":
            self._message = "Thread name cannot be empty"
            return
        rename_path = self._rename_path
        await self._rename_thread(rename_path, name)
        self._message = None
        self._entries = [
            (
                ThreadListEntry(
                    path=entry.path,
                    name=name,
                    created_at=entry.created_at,
                    modified_at=entry.modified_at,
                )
                if entry.path == rename_path
                else entry
            )
            for entry in self._entries
        ]
        self._rename_path = None
        self._rename_value = ""
        self._sync_selection()


def thread_list_entry_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw == "":
        return datetime.min.replace(tzinfo=timezone.utc)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def sort_thread_entries(
    entries: Iterable[ThreadListEntry],
) -> list[ThreadListEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            thread_list_entry_datetime(entry.modified_at),
            thread_list_entry_datetime(entry.created_at),
            entry.path,
        ),
        reverse=True,
    )


def thread_list_entry_from_agent_entry(
    entry: AgentThreadListEntry,
) -> ThreadListEntry:
    return ThreadListEntry(
        path=entry.path,
        name=entry.name,
        created_at=entry.created_at,
        modified_at=entry.modified_at,
    )


def thread_list_event_from_agent_payload(
    payload: dict[str, Any],
) -> ThreadListEvent | None:
    payload_type = payload.get("type")
    try:
        if payload_type == AGENT_EVENT_THREAD_CREATED:
            created = ThreadCreated.model_validate(payload)
            entry = thread_list_entry_from_agent_entry(created.thread)
            return ThreadListEvent(
                type="upserted",
                path=entry.path,
                entry=entry,
            )
        if payload_type == AGENT_EVENT_THREAD_UPDATED:
            updated = ThreadUpdated.model_validate(payload)
            entry = thread_list_entry_from_agent_entry(updated.thread)
            return ThreadListEvent(
                type="upserted",
                path=entry.path,
                entry=entry,
            )
        if payload_type == AGENT_EVENT_THREAD_DELETED:
            deleted = ThreadDeleted.model_validate(payload)
            return ThreadListEvent(
                type="deleted",
                path=deleted.path,
                entry=None,
            )
    except Exception:
        return None
    return None
