import asyncio
import json
import os
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Optional

import typer
from rich import print

from meshagent.api import (
    ApiScope,
    ParticipantToken,
    RoomClient,
    RoomException,
    WebSocketClientProtocol,
)
from meshagent.api.helpers import websocket_room_url
from meshagent.api.messaging import (
    Content,
    EmptyContent,
    ErrorContent,
    FileContent,
    JsonContent,
    TextContent,
    _ControlContent,
)
from meshagent.cli import async_typer
from meshagent.cli.common_options import ProjectIdOption, RoomOption
from meshagent.cli.helper import resolve_key, resolve_project_id, resolve_room
from meshagent.tools import RemoteToolkit, StreamTool, ToolContext

app = async_typer.AsyncTyper(help="Hidden test tools")


def _describe_chunk(chunk: Content) -> str:
    if isinstance(chunk, TextContent):
        return f"text: {chunk.text}"
    if isinstance(chunk, JsonContent):
        return f"json: {json.dumps(chunk.json, ensure_ascii=False)}"
    if isinstance(chunk, FileContent):
        return f"file: {chunk.name} ({chunk.mime_type}, {len(chunk.data)} bytes)"
    if isinstance(chunk, EmptyContent):
        return "empty"
    if isinstance(chunk, ErrorContent):
        return f"error: {chunk.text}"
    if isinstance(chunk, _ControlContent):
        return f"control: {chunk.method}"
    return str(chunk)


class _StreamToolController:
    def __init__(self) -> None:
        self._log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._state_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._sessions: dict[str, "_StreamSession"] = {}
        self._session_order: list[str] = []
        self._selected_session_id: Optional[str] = None
        self._next_session_number = 1
        self._lock = asyncio.Lock()

    @property
    def log_queue(self) -> asyncio.Queue[str]:
        return self._log_queue

    @property
    def state_queue(self) -> asyncio.Queue[None]:
        return self._state_queue

    async def log(self, message: str) -> None:
        await self._log_queue.put(message)

    def _notify_state_changed(self) -> None:
        if self._state_queue.full():
            return
        self._state_queue.put_nowait(None)

    def _active_session_ids_locked(self) -> list[str]:
        return [
            session_id
            for session_id in self._session_order
            if session_id in self._sessions
        ]

    def _select_fallback_session_locked(self) -> None:
        if (
            self._selected_session_id is not None
            and self._selected_session_id in self._sessions
        ):
            return

        active_session_ids = self._active_session_ids_locked()
        if len(active_session_ids) == 0:
            self._selected_session_id = None
            return
        self._selected_session_id = active_session_ids[0]

    async def snapshot_sessions(
        self,
    ) -> tuple[Optional[str], list["_StreamSessionSnapshot"]]:
        async with self._lock:
            selected_session_id = self._selected_session_id
            snapshots: list[_StreamSessionSnapshot] = []
            for session_id in self._active_session_ids_locked():
                session = self._sessions[session_id]
                snapshots.append(
                    _StreamSessionSnapshot(
                        session_id=session_id,
                        caller_label=session.caller_label,
                        request_closed=session.request_closed,
                    )
                )

        return selected_session_id, snapshots

    async def open_stream(
        self, *, request_stream: AsyncIterable[Content], caller_label: str
    ) -> AsyncIterable[Content]:
        async with self._lock:
            session_id = f"call-{self._next_session_number}"
            self._next_session_number += 1
            output_queue: asyncio.Queue[Optional[Content]] = asyncio.Queue()
            self._sessions[session_id] = _StreamSession(
                session_id=session_id,
                caller_label=caller_label,
                output_queue=output_queue,
            )
            self._session_order.append(session_id)
            self._select_fallback_session_locked()
            self._notify_state_changed()

        await self.log(f"[session:{session_id}] opened (caller={caller_label})")

        async def consume_request_stream() -> None:
            try:
                async for incoming in request_stream:
                    await self.log(f"[in:{session_id}] {_describe_chunk(incoming)}")
            except Exception as ex:
                await self.log(f"[in:{session_id}] error: {ex}")
            finally:
                async with self._lock:
                    session = self._sessions.get(session_id, None)
                    if session is not None:
                        session.request_closed = True
                        self._notify_state_changed()
                await self.log(f"[in:{session_id}] EOF")

        consume_task = asyncio.create_task(consume_request_stream())

        async def output_stream() -> AsyncIterator[Content]:
            try:
                while True:
                    next_chunk = await output_queue.get()
                    if next_chunk is None:
                        await self.log(f"[out:{session_id}] EOF")
                        return
                    await self.log(f"[out:{session_id}] {_describe_chunk(next_chunk)}")
                    yield next_chunk
            finally:
                if not consume_task.done():
                    consume_task.cancel()
                    await asyncio.gather(consume_task, return_exceptions=True)
                async with self._lock:
                    self._sessions.pop(session_id, None)
                    self._session_order = [
                        active_id
                        for active_id in self._session_order
                        if active_id != session_id
                    ]
                    self._select_fallback_session_locked()
                    self._notify_state_changed()
                await self.log(f"[session:{session_id}] closed")

        return output_stream()

    def _parse_output_line(self, line: str) -> Content:
        trimmed = line.strip()
        if trimmed.startswith("json:"):
            raw_json = trimmed[len("json:") :].strip()
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return JsonContent(json=parsed)
            raise RoomException("json: lines must decode to a JSON object")
        if trimmed.startswith("text:"):
            return TextContent(text=trimmed[len("text:") :].lstrip())
        return TextContent(text=line)

    async def send_line(self, line: str) -> None:
        async with self._lock:
            self._select_fallback_session_locked()
            selected_session_id = self._selected_session_id
            if selected_session_id is None:
                output_queue = None
            else:
                selected_session = self._sessions.get(selected_session_id, None)
                output_queue = (
                    selected_session.output_queue
                    if selected_session is not None
                    else None
                )

        if output_queue is None:
            await self.log("[ui] no active stream")
            return

        chunk = self._parse_output_line(line)
        await output_queue.put(chunk)

    async def end_output_stream(self) -> None:
        async with self._lock:
            self._select_fallback_session_locked()
            selected_session_id = self._selected_session_id
            if selected_session_id is None:
                output_queue = None
            else:
                selected_session = self._sessions.get(selected_session_id, None)
                output_queue = (
                    selected_session.output_queue
                    if selected_session is not None
                    else None
                )

        if output_queue is None:
            await self.log("[ui] no active stream to end")
            return

        await output_queue.put(None)

    async def select_session(self, *, session_id: str) -> bool:
        async with self._lock:
            if session_id not in self._sessions:
                return False
            self._selected_session_id = session_id
            self._notify_state_changed()
            return True

    async def select_next_session(self) -> None:
        async with self._lock:
            active_session_ids = self._active_session_ids_locked()
            if len(active_session_ids) == 0:
                self._selected_session_id = None
            elif self._selected_session_id not in active_session_ids:
                self._selected_session_id = active_session_ids[0]
            else:
                current_index = active_session_ids.index(self._selected_session_id)
                self._selected_session_id = active_session_ids[
                    (current_index + 1) % len(active_session_ids)
                ]
            self._notify_state_changed()

    async def select_previous_session(self) -> None:
        async with self._lock:
            active_session_ids = self._active_session_ids_locked()
            if len(active_session_ids) == 0:
                self._selected_session_id = None
            elif self._selected_session_id not in active_session_ids:
                self._selected_session_id = active_session_ids[0]
            else:
                current_index = active_session_ids.index(self._selected_session_id)
                self._selected_session_id = active_session_ids[
                    (current_index - 1) % len(active_session_ids)
                ]
            self._notify_state_changed()


@dataclass
class _StreamSession:
    session_id: str
    caller_label: str
    output_queue: asyncio.Queue[Optional[Content]]
    request_closed: bool = False


@dataclass(frozen=True)
class _StreamSessionSnapshot:
    session_id: str
    caller_label: str
    request_closed: bool


class _TestStreamTool(StreamTool):
    def __init__(self, *, controller: _StreamToolController, name: str):
        self._controller = controller
        super().__init__(
            name=name,
            title=name,
            description=(
                "Streaming test tool. Requires request-stream input and emits streamed output."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    async def execute(
        self,
        *,
        context: ToolContext,
        request_stream: AsyncIterable[Content],
    ) -> Content | AsyncIterable[Content]:
        caller_name = context.caller.get_attribute("name")
        if isinstance(caller_name, str) and caller_name.strip() != "":
            caller_label = caller_name
        else:
            caller_label = context.caller.id

        return await self._controller.open_stream(
            request_stream=request_stream,
            caller_label=caller_label,
        )


@app.async_command(
    "stream-tool",
    help="Host a hidden streaming test tool with a textual UI",
    hidden=True,
)
async def stream_tool(
    *,
    project_id: ProjectIdOption,
    room: RoomOption,
    name: Annotated[
        str,
        typer.Option("--name", help="Participant name"),
    ] = "stream-tool-test",
    role: Annotated[
        str,
        typer.Option("--role", help="Participant role"),
    ] = "tool",
    toolkit: Annotated[
        str,
        typer.Option("--toolkit", help="Toolkit name"),
    ] = "test-stream-toolkit",
    tool: Annotated[
        str,
        typer.Option("--tool", help="Tool name"),
    ] = "stream",
    key: Annotated[
        Optional[str],
        typer.Option("--key", help="API key for signing a participant token"),
    ] = None,
) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, VerticalScroll
        from textual.widgets import Static, TextArea
    except ImportError as exc:
        print(
            "[bold red]Textual is required for this command. Install meshagent-cli dependencies and retry.[/bold red]"
        )
        raise typer.Exit(1) from exc

    key = await resolve_key(project_id=project_id, key=key)
    await resolve_project_id(project_id=project_id)
    room_name = resolve_room(room)
    if room_name is None:
        print("[red]Room name is required[/red]")
        raise typer.Exit(1)

    jwt = os.getenv("MESHAGENT_TOKEN")
    if jwt is None or room_name != os.getenv("MESHAGENT_ROOM"):
        token = ParticipantToken(name=name)
        token.add_role_grant(role=role)
        token.add_room_grant(room_name)
        token.add_api_grant(ApiScope.full())
        jwt = token.to_jwt(api_key=key)

    class _StreamToolTextualApp(App[None]):
        CSS = """
        Screen {
            layout: grid;
            grid-size: 1 4;
            grid-rows: auto auto 1fr auto;
            padding: 0 1;
        }
        #header {
            padding: 1 0 1 0;
        }
        #calls {
            border: solid $primary;
            padding: 1;
        }
        #log-scroll {
            height: 1fr;
            border: solid $primary;
            padding: 1;
        }
        #log {
            width: 100%;
        }
        #input-row {
            margin: 0;
            background: #2f2f2f;
            padding: 1 0 1 0;
        }
        #prompt {
            width: 2;
            content-align: center top;
            color: $text-muted;
            background: #2f2f2f;
        }
        #line-input {
            width: 1fr;
            height: 1;
            min-height: 1;
            max-height: 6;
            border: none;
            outline: none;
            padding: 0;
            margin: 0;
            color: white;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #line-input:focus {
            border: none;
            background: #2f2f2f;
            background-tint: 0%;
        }
        #line-input .text-area--cursor-line {
            background: #2f2f2f;
        }
        #line-input .text-area--gutter {
            background: #2f2f2f;
        }
        #line-input .text-area--cursor-gutter {
            background: #2f2f2f;
        }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit_without_close", "Quit", priority=True),
            Binding("enter", "send_line", "Send", priority=True),
            Binding("ctrl+d", "end_output_stream", "End Output", priority=True),
            Binding("ctrl+n", "select_next_call", "Next Call", priority=True),
            Binding("ctrl+p", "select_previous_call", "Prev Call", priority=True),
        ]

        def __init__(self, *, controller: _StreamToolController) -> None:
            super().__init__()
            self._controller = controller
            self._log_lines: list[str] = []
            self._calls_view: Optional[Static] = None
            self._log_view: Optional[Static] = None
            self._log_scroll: Optional[VerticalScroll] = None
            self._input_view: Optional[TextArea] = None
            self._input_height = 1
            self._log_task: Optional[asyncio.Task[None]] = None
            self._state_task: Optional[asyncio.Task[None]] = None

        def compose(self) -> ComposeResult:
            yield Static(
                (
                    f"[bold]Toolkit[/bold]: {toolkit}  [bold]Tool[/bold]: {tool}\n"
                    "Type a line and press Enter to stream it to the caller.\n"
                    "Use Ctrl-N / Ctrl-P to switch calls, or /select <call-id>.\n"
                    "Use 'json: {...}' for JSON chunks, 'text: ...' for explicit text.\n"
                    "Ctrl-D ends the output stream. Ctrl-C exits without closing."
                ),
                id="header",
            )
            yield Static("", id="calls")
            with VerticalScroll(id="log-scroll"):
                yield Static("", id="log")
            with Horizontal(id="input-row"):
                yield Static("›", id="prompt")
                yield TextArea(
                    "",
                    id="line-input",
                    soft_wrap=True,
                    show_line_numbers=False,
                )

        async def on_mount(self) -> None:
            self._calls_view = self.query_one("#calls", Static)
            self._log_view = self.query_one("#log", Static)
            self._log_scroll = self.query_one("#log-scroll", VerticalScroll)
            self._input_view = self.query_one("#line-input", TextArea)
            self._input_view.focus()
            self._resize_input(self._input_view)
            await self._refresh_calls()
            self._log_task = asyncio.create_task(self._consume_logs())
            self._state_task = asyncio.create_task(self._consume_state())

        async def on_unmount(self) -> None:
            if self._log_task is not None and not self._log_task.done():
                self._log_task.cancel()
                await asyncio.gather(self._log_task, return_exceptions=True)
                self._log_task = None
            if self._state_task is not None and not self._state_task.done():
                self._state_task.cancel()
                await asyncio.gather(self._state_task, return_exceptions=True)
                self._state_task = None

        async def _consume_logs(self) -> None:
            try:
                while True:
                    message = await self._controller.log_queue.get()
                    self._append_log_line(message)
            except asyncio.CancelledError:
                return

        async def _consume_state(self) -> None:
            try:
                while True:
                    await self._controller.state_queue.get()
                    await self._refresh_calls()
            except asyncio.CancelledError:
                return

        async def _refresh_calls(self) -> None:
            selected_session_id, sessions = await self._controller.snapshot_sessions()
            lines = ["[bold]In Progress Calls[/bold]"]
            if len(sessions) == 0:
                lines.append("none")
            else:
                for session in sessions:
                    marker = ">" if session.session_id == selected_session_id else " "
                    status = (
                        "request closed" if session.request_closed else "request open"
                    )
                    lines.append(
                        f"{marker} {session.session_id} ({session.caller_label}) - {status}"
                    )

            if self._calls_view is not None:
                self._calls_view.update("\n".join(lines))

        def _ensure_input_focus(self) -> None:
            if self._input_view is not None and self.focused is not self._input_view:
                self._input_view.focus()

        def _append_log_line(self, message: str) -> None:
            self._log_lines.append(message)
            if len(self._log_lines) > 500:
                self._log_lines = self._log_lines[-500:]
            if self._log_view is not None:
                self._log_view.update("\n".join(self._log_lines))
            if self._log_scroll is not None:
                self._log_scroll.scroll_end(animate=False)
            self._ensure_input_focus()

        async def action_send_line(self) -> None:
            if self._input_view is None:
                return
            line = self._input_view.text.strip()
            self._input_view.load_text("")
            self._resize_input(self._input_view)
            self._input_view.focus()
            if line == "":
                return
            if line.startswith("/select "):
                session_id = line[len("/select ") :].strip()
                if session_id == "":
                    await self._controller.log("[ui] usage: /select <call-id>")
                    return
                selected = await self._controller.select_session(session_id=session_id)
                if not selected:
                    await self._controller.log(f"[ui] unknown call '{session_id}'")
                else:
                    await self._controller.log(f"[ui] selected '{session_id}'")
                return
            try:
                await self._controller.send_line(line)
            except Exception as ex:
                await self._controller.log(f"[ui] error: {ex}")

        async def action_end_output_stream(self) -> None:
            await self._controller.end_output_stream()

        async def action_select_next_call(self) -> None:
            await self._controller.select_next_session()

        async def action_select_previous_call(self) -> None:
            await self._controller.select_previous_session()

        async def action_quit_without_close(self) -> None:
            await self._controller.log(
                "[ui] exiting without sending output stream close"
            )
            self.exit()

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if self._input_view is None or event.text_area is not self._input_view:
                return
            self._resize_input(event.text_area)

        def _resize_input(self, input_view: TextArea) -> None:
            target_height = max(1, min(6, input_view.virtual_size.height))
            if target_height == self._input_height:
                return
            self._input_height = target_height
            input_view.styles.height = target_height

    controller = _StreamToolController()
    await controller.log(
        f"[ready] connecting to room '{room_name}' as participant '{name}'"
    )

    print("[bold green]Connecting to room...[/bold green]")
    async with RoomClient(
        protocol=WebSocketClientProtocol(
            url=websocket_room_url(room_name=room_name),
            token=jwt,
        )
    ) as client:
        remote_toolkit = RemoteToolkit(
            name=toolkit,
            tools=[_TestStreamTool(controller=controller, name=tool)],
            title="test stream toolkit",
            description="hidden stream-tool testing toolkit",
        )
        await remote_toolkit.start(room=client)
        await controller.log(f"[ready] hosting {toolkit}/{tool} in room '{room_name}'")
        try:
            app_instance = _StreamToolTextualApp(controller=controller)
            try:
                await app_instance.run_async()
            except KeyboardInterrupt:
                pass
            except LookupError as ex:
                if "active_app" not in str(ex):
                    raise
        finally:
            await remote_toolkit.stop()
