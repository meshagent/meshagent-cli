import sys
import tty
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

from meshagent.api import ParticipantToken
from meshagent.cli import async_typer
from meshagent.cli.helper import (
    get_client,
    resolve_project_id,
    resolve_api_key,
)

app = async_typer.AsyncTyper()


@app.async_command("connect")
async def tty_command(
    *,
    project_id: str = None,
    room: Annotated[str, typer.Option()],
    api_key_id: Annotated[Optional[str], typer.Option()] = None,
):
    """Open an interactive websocket‑based TTY."""
    client = await get_client()
    try:
        project_id = await resolve_project_id(project_id=project_id)
        api_key_id = await resolve_api_key(project_id=project_id, api_key_id=api_key_id)

        token = ParticipantToken(
            name="tty", project_id=project_id, api_key_id=api_key_id
        )

        key = (
            await client.decrypt_project_api_key(project_id=project_id, id=api_key_id)
        )["token"]

        token.add_role_grant(role="user")
        token.add_room_grant(room)

        ws_url = (
            websocket_room_url(room_name=room) + f"/tty?token={token.to_jwt(token=key)}"
        )

        print(f"[bold green]Connecting to[/bold green] {room}")

        # Save current terminal settings so we can restore them later.
        old_tty_settings = termios.tcgetattr(sys.stdin)

        async with RoomClient(
            protocol=WebSocketClientProtocol(
                url=websocket_room_url(room_name=room), token=token.to_jwt(token=key)
            )
        ):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url) as websocket:
                        tty.setraw(sys.stdin)
                        send_queue = asyncio.Queue[bytes]()

                        loop = asyncio.get_running_loop()
                        transport, protocol = await loop.connect_write_pipe(
                            asyncio.streams.FlowControlMixin, sys.stdout
                        )
                        writer = asyncio.StreamWriter(transport, protocol, None, loop)

                        async def recv_from_websocket():
                            async for message in websocket:
                                if message.type == aiohttp.WSMsgType.CLOSE:
                                    await websocket.close()

                                elif message.type == aiohttp.WSMsgType.CLOSING:
                                    pass

                                elif message.type == aiohttp.WSMsgType.ERROR:
                                    await websocket.close()

                                data: bytes = message.data
                                writer.write(data)
                                await writer.drain()

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
                            await loop.connect_read_pipe(lambda: protocol, sys.stdin)

                            while True:
                                # Read one character at a time from stdin without blocking the event loop.

                                data = await reader.read(1)
                                if not data:
                                    break

                                if websocket.closed:
                                    break

                                if data == b"\x04":
                                    print("<CTRL-D>\n")
                                    break

                                if data:
                                    send_queue.put_nowait(b"\0" + data)
                                else:
                                    send_queue.shutdown(immediate=True)
                                    await websocket.close(code=1000)
                                    break

                        async def send_to_websocket():
                            while True:
                                data = await send_queue.get()
                                if websocket.closed:
                                    return

                                if data is not None:
                                    await websocket.send_bytes(data)

                        done, pending = await asyncio.wait(
                            [
                                asyncio.create_task(recv_from_websocket()),
                                asyncio.create_task(read_stdin()),
                                asyncio.create_task(send_to_websocket()),
                            ],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        for task in pending:
                            task.cancel()

            finally:
                # Restore original terminal settings even if the coroutine is cancelled.
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty_settings)

    finally:
        await client.close()
