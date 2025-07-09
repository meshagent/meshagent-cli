import sys
import tty
import termios
from meshagent.api.helpers import websocket_room_url
from typing import Annotated, Optional

import asyncio
import typer
from rich import print
import aiohttp

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
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as websocket:
                    print(f"[bold green]Connected to[/bold green] {room}")

                    tty.setraw(sys.stdin)

                    async def recv_from_websocket():
                        async for message in websocket:
                            if message.type == aiohttp.WSMsgType.CLOSE:
                                await websocket.close()

                            elif message.type == aiohttp.WSMsgType.ERROR:
                                await websocket.close()

                            data: bytes = message.data
                            sys.stdout.write(data.decode("utf-8"))
                            sys.stdout.flush()

                    async def send_to_websocket():
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

                            if data == b"\x03":
                                print("<CTRL-C>\n")
                                break

                            if data:
                                await websocket.send_bytes(data)
                            else:
                                await websocket.close(code=1000)
                                break

                    done, pending = await asyncio.wait(
                        [
                            asyncio.create_task(recv_from_websocket()),
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
