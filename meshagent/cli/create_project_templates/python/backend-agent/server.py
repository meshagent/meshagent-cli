from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url
from meshagent.tools import FunctionTool, Toolkit, ToolContext
from meshagent.tools.hosting import start_hosted_toolkit


PROOF_PATH = Path(__file__).with_name("agent-proof.json")
TOOLKIT_NAME = "meshagent.create.python-agent"


class PingTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="ping",
            title="Ping local agent",
            description="Checks that the local Python backend agent toolkit is reachable.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext) -> dict:
        return {"pong": True}


class StatusTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="status",
            title="Read local agent status",
            description="Returns a minimal status payload from the local Python backend agent.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext) -> dict:
        return {"ready": True, "language": "python", "focus": "backend-agent"}


class EchoTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            title="Echo a message",
            description="Echoes a message through the local Python backend agent.",
            input_schema={
                "type": "object",
                "required": ["message"],
                "additionalProperties": False,
                "properties": {"message": {"type": "string"}},
            },
        )

    async def execute(self, context: ToolContext, *, message: str) -> dict:
        return {"echo": message}


class PythonAgentToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(
            name=TOOLKIT_NAME,
            title="Python Local Agent Toolkit",
            description="Local-only ping, status, and echo tools for the Python backend agent.",
            tools=[PingTool(), StatusTool(), EchoTool()],
        )


async def write_agent_proof(probe: str, echo: str) -> None:
    payload = {
        "probe": probe,
        "echo": echo,
        "tools": ["ping", "status", "echo"],
    }
    await aiofiles.os.makedirs(PROOF_PATH.parent, exist_ok=True)
    async with aiofiles.open(PROOF_PATH, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(payload, indent=2) + "\n")


async def main() -> None:
    room_name = os.environ.get("MESHAGENT_ROOM")
    token = os.environ.get("MESHAGENT_TOKEN")
    if not room_name or not token:
        print("MeshAgent room environment is not set; waiting for deployment env.")
        await asyncio.Event().wait()
        return

    protocol = WebSocketClientProtocol(
        url=websocket_room_url(room_name=room_name),
        token=token,
    )
    async with RoomClient(protocol_factory=protocol.create_factory()) as room:
        print(f"Connected to MeshAgent room: {room.room_name}")
        await run_agent_toolkit_proof(room)


async def run_agent_toolkit_proof(room: RoomClient) -> None:
    probe = os.environ.get("MESHAGENT_CREATE_DEV_PROBE")
    hosted_toolkit = await start_hosted_toolkit(
        room=room,
        toolkit=PythonAgentToolkit(),
    )
    try:
        if not probe:
            await room.wait_for_close()
            return

        pinged = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="ping",
            input={},
        )
        print(f"MeshAgent create dev toolkit ping: {json.dumps(pinged.json)}")

        status = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="status",
            input={},
        )
        print(f"MeshAgent create dev toolkit status: {json.dumps(status.json)}")

        message = f"MeshAgent local dev proof {probe}"
        echoed = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="echo",
            input={"message": message},
        )
        print(f"MeshAgent create dev toolkit echo: {json.dumps(echoed.json)}")
        if echoed.json.get("echo") != message:
            raise RuntimeError("Local Python agent toolkit proof failed.")

        await write_agent_proof(probe, message)
        print(f"MeshAgent create dev toolkit proof wrote: agent-proof.json {probe}")
        hold_seconds = float(
            os.environ.get("MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS") or "0"
        )
        if hold_seconds > 0:
            print(
                f"MeshAgent create dev toolkit holding registration for {hold_seconds}s"
            )
            await asyncio.sleep(hold_seconds)
    finally:
        await hosted_toolkit.stop()


if __name__ == "__main__":
    asyncio.run(main())
