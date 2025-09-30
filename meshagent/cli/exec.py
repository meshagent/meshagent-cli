import sys
import os
from meshagent.api.websocket_protocol import WebSocketClientProtocol
from meshagent.api import RoomClient
from meshagent.api.helpers import websocket_room_url
from typing import Annotated, Optional
from meshagent.cli.common_options import ProjectIdOption, RoomOption
import asyncio
import typer
from rich import print
import aiohttp
import struct
import signal
import shutil
import json
from urllib.parse import quote
import threading
import time


import logging

from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_room,
)

if os.name == "nt":
    import msvcrt
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32
    _ENABLE_ECHO_INPUT = 0x0004
    _ENABLE_LINE_INPUT = 0x0002

    def set_raw(f):
        """Disable line and echo mode for the given file handle."""
        handle = msvcrt.get_osfhandle(f.fileno())
        original_mode = wintypes.DWORD()
        if not _kernel32.GetConsoleMode(handle, ctypes.byref(original_mode)):
            return None
        new_mode = original_mode.value & ~(_ENABLE_ECHO_INPUT | _ENABLE_LINE_INPUT)
        _kernel32.SetConsoleMode(handle, new_mode)
        return handle, original_mode.value

    def restore(f, state):
        if state is None:
            return None
        handle, mode = state
        _kernel32.SetConsoleMode(handle, mode)
        return None

else:
    import termios
    import tty as _tty

    def set_raw(fd):
        old = termios.tcgetattr(fd)
        _tty.setraw(fd)
        return old

    def restore(fd, old_settings):
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class _StdWriter:
    """Simple asyncio-friendly wrapper for standard streams on Windows."""

    def __init__(self, file):
        self._file = file

    def write(self, data: bytes) -> None:
        self._file.buffer.write(data)

    async def drain(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._file.flush)


def register(app: typer.Typer):
    @app.async_command("exec")
    async def exec_command(
        *,
        project_id: ProjectIdOption = None,
        room: RoomOption,
        name: Annotated[Optional[str], typer.Option()] = None,
        image: Annotated[Optional[str], typer.Option()] = None,
        command: Annotated[list[str], typer.Argument(...)] = None,
        tty: bool = False,
        room_storage_path: str = "/data",
    ):
        """Open an interactive websocket‑based TTY."""
        client = await get_client()
        try:
            project_id = await resolve_project_id(project_id=project_id)
            room = resolve_room(room)

            connection = await client.connect_room(project_id=project_id, room=room)

            ws_url = (
                websocket_room_url(room_name=room) + f"/exec?token={connection.jwt}"
            )

            if image:
                ws_url += f"&image={quote(' '.join(image))}"

            if name:
                ws_url += f"&name={quote(' '.join(name))}"

            if command and len(command) != 0:
                ws_url += f"&command={quote(' '.join(command))}"

            if room_storage_path:
                room_storage_path += (
                    f"&room_storage_path={quote(' '.join(room_storage_path))}"
                )

            if tty:
                if not sys.stdin.isatty():
                    print("[red]TTY requested but process is not a TTY[/red]")
                    raise typer.Exit(1)

                ws_url += "&tty=true"

            else:
                if command is None:
                    print("[red]TTY required when not executing a command[/red]")
                    raise typer.Exit(1)

                ws_url += "&tty=false"

            if tty:
                # Save current terminal settings so we can restore them later.
                old_tty_settings = set_raw(sys.stdin)

            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
                    token=connection.jwt,
                )
            ):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(ws_url) as websocket:
                            send_queue = asyncio.Queue[bytes]()

                            loop = asyncio.get_running_loop()
                            if os.name == "nt":
                                stdout_writer = _StdWriter(sys.stdout)
                                stderr_writer = _StdWriter(sys.stderr)
                            else:
                                (
                                    stdout_transport,
                                    stdout_protocol,
                                ) = await loop.connect_write_pipe(
                                    asyncio.streams.FlowControlMixin, sys.stdout
                                )
                                stdout_writer = asyncio.StreamWriter(
                                    stdout_transport, stdout_protocol, None, loop
                                )

                                (
                                    stderr_transport,
                                    stderr_protocol,
                                ) = await loop.connect_write_pipe(
                                    asyncio.streams.FlowControlMixin, sys.stderr
                                )
                                stderr_writer = asyncio.StreamWriter(
                                    stderr_transport, stderr_protocol, None, loop
                                )

                            async def recv_from_websocket():
                                while True:
                                    done, pending = await asyncio.wait(
                                        [asyncio.create_task(websocket.receive())],
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )

                                    first = done.pop()

                                    if first == read_stdin_task:
                                        break

                                    message = first.result()

                                    if websocket.closed:
                                        break

                                    if message.type == aiohttp.WSMsgType.CLOSE:
                                        break

                                    elif message.type == aiohttp.WSMsgType.CLOSING:
                                        pass

                                    elif message.type == aiohttp.WSMsgType.ERROR:
                                        break

                                    if not message.data:
                                        break

                                    data: bytes = message.data
                                    if len(data) > 0:
                                        if data[0] == 1:
                                            stderr_writer.write(data)
                                            await stderr_writer.drain()
                                        elif data[0] == 0:
                                            stdout_writer.write(data)
                                            await stdout_writer.drain()
                                        else:
                                            raise ValueError(
                                                f"Invalid channel received {data[0]}"
                                            )

                            last_size = None

                            async def send_resize(rows, cols):
                                nonlocal last_size

                                size = (cols, rows)
                                if size == last_size:
                                    return

                                last_size = size

                                resize_json = json.dumps(
                                    {"Width": cols, "Height": rows}
                                ).encode("utf-8")
                                payload = struct.pack("B", 4) + resize_json
                                send_queue.put_nowait(payload)
                                await asyncio.sleep(5)

                            cols, rows = shutil.get_terminal_size(fallback=(24, 80))
                            if tty:
                                await send_resize(rows, cols)

                            def on_sigwinch():
                                cols, rows = shutil.get_terminal_size(fallback=(24, 80))
                                task = asyncio.create_task(send_resize(rows, cols))

                                def on_done(t: asyncio.Task):
                                    t.result()

                                task.add_done_callback(on_done)

                            if hasattr(signal, "SIGWINCH"):
                                loop.add_signal_handler(signal.SIGWINCH, on_sigwinch)

                            async def read_stdin():
                                loop = asyncio.get_running_loop()

                                if os.name == "nt":
                                    queue: asyncio.Queue[bytes] = asyncio.Queue()
                                    stop_event = threading.Event()

                                    if sys.stdin.isatty():

                                        def reader() -> None:
                                            try:
                                                while not stop_event.is_set():
                                                    if msvcrt.kbhit():
                                                        data = msvcrt.getch()
                                                        loop.call_soon_threadsafe(
                                                            queue.put_nowait, data
                                                        )
                                                    else:
                                                        time.sleep(0.01)
                                            finally:
                                                loop.call_soon_threadsafe(
                                                    queue.put_nowait, b""
                                                )
                                    else:

                                        def reader() -> None:
                                            try:
                                                while not stop_event.is_set():
                                                    data = sys.stdin.buffer.read(1)
                                                    loop.call_soon_threadsafe(
                                                        queue.put_nowait, data
                                                    )
                                                    if not data:
                                                        break
                                            finally:
                                                loop.call_soon_threadsafe(
                                                    queue.put_nowait, b""
                                                )

                                    thread = threading.Thread(target=reader)
                                    thread.start()

                                    async def reader_task() -> bytes:
                                        return await queue.get()
                                else:
                                    reader = asyncio.StreamReader()
                                    protocol = asyncio.StreamReaderProtocol(reader)
                                    await loop.connect_read_pipe(
                                        lambda: protocol, sys.stdin
                                    )

                                    async def reader_task():
                                        return await reader.read(1)

                                try:
                                    while True:
                                        # Read one character at a time from stdin without blocking the event loop.
                                        done, pending = await asyncio.wait(
                                            [
                                                asyncio.create_task(reader_task()),
                                                websocket_recv_task,
                                            ],
                                            return_when=asyncio.FIRST_COMPLETED,
                                        )

                                        first = done.pop()
                                        if first == websocket_recv_task:
                                            break

                                        data = first.result()
                                        if not data:
                                            break

                                        if websocket.closed:
                                            break

                                        if tty:
                                            if data == b"\x04":
                                                break

                                        if data:
                                            send_queue.put_nowait(b"\0" + data)
                                        else:
                                            break
                                finally:
                                    if os.name == "nt":
                                        stop_event.set()
                                        thread.join()

                                send_queue.put_nowait(b"\0")

                            websocket_recv_task = asyncio.create_task(
                                recv_from_websocket()
                            )
                            read_stdin_task = asyncio.create_task(read_stdin())

                            async def send_to_websocket():
                                while True:
                                    try:
                                        data = await send_queue.get()
                                        if websocket.closed:
                                            break

                                        if data is not None:
                                            await websocket.send_bytes(data)

                                        else:
                                            break
                                    except asyncio.QueueShutDown:
                                        break

                            send_to_websocket_task = asyncio.create_task(
                                send_to_websocket()
                            )
                            await asyncio.gather(
                                websocket_recv_task,
                                read_stdin_task,
                            )

                            send_queue.shutdown()
                            await send_to_websocket_task

                finally:
                    if not sys.stdin.closed and tty:
                        # Restore original terminal settings even if the coroutine is cancelled.
                        restore(sys.stdin, old_tty_settings)

        except Exception as e:
            print(f"[red]{e}[/red]")
            logging.error("failed", exc_info=e)
            raise typer.Exit(1)
        finally:
            await client.close()
