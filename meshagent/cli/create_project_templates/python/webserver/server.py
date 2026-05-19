from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from aiohttp import web
from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url
from meshagent.tools import FunctionTool, Toolkit, ToolContext
from meshagent.tools.hosting import _start_hosted_toolkit


CONTENT_PATH = Path(__file__).with_name("dev-content.json")
TOOLKIT_NAME = "meshagent.create.python-content"


def default_content() -> dict:
    return {
        "activeId": "hero",
        "items": {
            "hero": {
                "id": "hero",
                "headline": "hello from meshagent create",
                "body": "Run ./scripts/dev.sh to let the local MeshAgent toolkit update this content.",
            }
        },
    }


async def read_content() -> dict:
    try:
        async with aiofiles.open(CONTENT_PATH, encoding="utf-8") as handle:
            return json.loads(await handle.read())
    except Exception:
        return default_content()


async def write_content(content: dict) -> dict:
    await aiofiles.os.makedirs(CONTENT_PATH.parent, exist_ok=True)
    async with aiofiles.open(CONTENT_PATH, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(content, indent=2) + "\n")
    return content


class CreateContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="create",
            title="Create local web content",
            description="Creates a local Python content record rendered by the dev app.",
            input_schema={
                "type": "object",
                "required": ["id", "headline", "body"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        )

    async def execute(
        self, context: ToolContext, *, id: str, headline: str, body: str
    ) -> dict:
        content = await read_content()
        content.setdefault("items", {})[id] = {
            "id": id,
            "headline": headline,
            "body": body,
        }
        content["activeId"] = id
        await write_content(content)
        return {"ok": True, "item": content["items"][id]}


class UpdateContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="update",
            title="Update local web content",
            description="Updates a local Python content record rendered by the dev app.",
            input_schema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        id: str,
        headline: str | None = None,
        body: str | None = None,
    ) -> dict:
        content = await read_content()
        items = content.setdefault("items", {})
        existing = items.get(id, {"id": id, "headline": "", "body": ""})
        if headline is not None:
            existing["headline"] = headline
        if body is not None:
            existing["body"] = body
        items[id] = existing
        content["activeId"] = id
        await write_content(content)
        return {"ok": True, "item": existing}


class SearchContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="search",
            title="Search local web content",
            description="Searches content records currently available to the local Python app.",
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {"query": {"type": "string"}},
            },
        )

    async def execute(self, context: ToolContext, *, query: str) -> dict:
        content = await read_content()
        normalized_query = str(query or "").lower()
        results = [
            item
            for item in content.get("items", {}).values()
            if normalized_query
            in f"{item.get('headline', '')}\n{item.get('body', '')}".lower()
        ]
        return {"ok": True, "results": results}


class PythonContentToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(
            name=TOOLKIT_NAME,
            title="Python Local Content Toolkit",
            description="Local-only create, update, and search tools for Python dev app content.",
            tools=[CreateContentTool(), UpdateContentTool(), SearchContentTool()],
        )


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def index(request: web.Request) -> web.Response:
    content = await read_content()
    active_item = content.get("items", {}).get(content.get("activeId")) or content.get(
        "items", {}
    ).get("hero", {})
    return web.Response(
        text=f"{active_item.get('headline', 'hello from meshagent create')}\n{active_item.get('body', '')}\n",
        content_type="text/plain",
    )


async def status(request: web.Request) -> web.Response:
    return web.Response(text="ready\n", content_type="text/plain")


async def ping(request: web.Request) -> web.Response:
    return web.json_response({"pong": True})


async def run_dev_content_toolkit() -> None:
    probe = os.environ.get("MESHAGENT_CREATE_DEV_PROBE")
    room_name = os.environ.get("MESHAGENT_ROOM")
    token = os.environ.get("MESHAGENT_TOKEN")
    if not probe or not room_name or not token:
        return

    protocol = WebSocketClientProtocol(
        url=websocket_room_url(room_name=room_name),
        token=token,
    )
    async with RoomClient(protocol_factory=protocol.create_factory()) as room:
        hosted_toolkit = await _start_hosted_toolkit(
            room=room,
            toolkit=PythonContentToolkit(),
        )
        try:
            proof_id = "meshagent-create-proof"
            created = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="create",
                input={
                    "id": proof_id,
                    "headline": "Local dev content created through MeshAgent",
                    "body": "This text was created by the local Python content toolkit.",
                },
            )
            print(f"MeshAgent create dev toolkit create: {json.dumps(created.json)}")

            headline = f"MeshAgent local dev proof {probe}"
            updated = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="update",
                input={
                    "id": proof_id,
                    "headline": headline,
                    "body": "The room invoked the local Python toolkit, and the toolkit updated dev-content.json.",
                },
            )
            print(f"MeshAgent create dev toolkit update: {json.dumps(updated.json)}")

            searched = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="search",
                input={"query": probe},
            )
            print(f"MeshAgent create dev toolkit search: {json.dumps(searched.json)}")

            content = await read_content()
            active_item = content.get("items", {}).get(content.get("activeId"))
            search_results = searched.json.get("results", [])
            if active_item.get("headline") != headline or not any(
                item.get("headline") == headline for item in search_results
            ):
                raise RuntimeError("Local Python content toolkit proof failed.")

            print(f"MeshAgent create dev toolkit proof wrote: dev-content.json {probe}")
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


async def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/api/ping", ping)
    app.router.add_get("/", index)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Serving on 0.0.0.0:{port}")
    asyncio.create_task(run_dev_content_toolkit())

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
