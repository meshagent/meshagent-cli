import sys
import tty as _tty
import termios
from meshagent.api.websocket_protocol import WebSocketClientProtocol
from meshagent.api import RoomClient
from meshagent.api.helpers import websocket_room_url
from typing import Annotated, Optional
import asyncio
import typer
from rich import print
import aiohttp
import struct
import signal
import shutil
import json
from urllib.parse import quote

from meshagent.api import ParticipantToken

import logging

from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
)


def register(app: typer.Typer):
    @app.async_command("exec")
    async def exec_command(
        *,
        project_id: str = None,
        room: Annotated[str, typer.Option()],
        name: Annotated[Optional[str], typer.Option()] = None,
        image: Annotated[Optional[str], typer.Option()] = None,
        api_key_id: Annotated[Optional[str], typer.Option()] = None,
        command: Annotated[list[str], typer.Argument(...)] = None,
        tty: bool = False,
    ):
        """Open an interactive websocket‑based TTY."""
        client = await get_client()
        try:
            project_id = await resolve_project_id(project_id=project_id)
            api_key_id = await resolve_api_key(
                project_id=project_id, api_key_id=api_key_id
            )

            token = ParticipantToken(
                name="tty", project_id=project_id, api_key_id=api_key_id
            )

            key = (
                await client.decrypt_project_api_key(
                    project_id=project_id, id=api_key_id
                )
            )["token"]

            token.add_role_grant(role="user")
            token.add_room_grant(room)

            ws_url = (
                websocket_room_url(room_name=room)
                + f"/exec?token={token.to_jwt(token=key)}"
            )

            if image:
                ws_url += f"&image={quote(' '.join(image))}"

            if name:
                ws_url += f"&name={quote(' '.join(name))}"

            if command and len(command) != 0:
                ws_url += f"&command={quote(' '.join(command))}"

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
                old_tty_settings = termios.tcgetattr(sys.stdin)
                _tty.setraw(sys.stdin)

            async with RoomClient(
                protocol=WebSocketClientProtocol(
                    url=websocket_room_url(room_name=room),
                    token=token.to_jwt(token=key),
                )
            ):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(ws_url) as websocket:
                            send_queue = asyncio.Queue[bytes]()

                            loop = asyncio.get_running_loop()
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

                            loop.add_signal_handler(signal.SIGWINCH, on_sigwinch)

                            async def read_stdin():
                                loop = asyncio.get_running_loop()

                                reader = asyncio.StreamReader()
                                protocol = asyncio.StreamReaderProtocol(reader)
                                await loop.connect_read_pipe(
                                    lambda: protocol, sys.stdin
                                )

                                while True:
                                    # Read one character at a time from stdin without blocking the event loop.
                                    done, pending = await asyncio.wait(
                                        [
                                            asyncio.create_task(reader.read(1)),
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
                        termios.tcsetattr(
                            sys.stdin, termios.TCSADRAIN, old_tty_settings
                        )

        except Exception as e:
            print(f"[red]{e}[/red]")
            logging.error("failed", exc_info=e)
            raise typer.Exit(1)
        finally:
            await client.close()
