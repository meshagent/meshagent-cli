from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

from aiohttp import web
from meshagent.agents import run_external_channel
from meshagent.api import RoomClient

from channel import WhatsAppChannel, create_channel


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the WhatsApp channel.")
    return value


def create_app(
    *, channel: WhatsAppChannel, app_secret: str, verify_token: str
) -> web.Application:
    app = web.Application()
    tasks: set[asyncio.Task[None]] = set()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def challenge(request: web.Request) -> web.Response:
        supplied = request.query.get("hub.verify_token", "")
        response = request.query.get("hub.challenge", "")
        if (
            request.query.get("hub.mode") != "subscribe"
            or response == ""
            or not hmac.compare_digest(supplied, verify_token)
        ):
            raise web.HTTPForbidden()
        return web.Response(text=response)

    async def webhook(request: web.Request) -> web.Response:
        body = await request.read()
        expected = (
            "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not hmac.compare_digest(
            request.headers.get("X-Hub-Signature-256", ""), expected
        ):
            raise web.HTTPForbidden()
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise web.HTTPBadRequest() from exc
        task = asyncio.create_task(channel.process_webhook(body.decode()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.Response(text="EVENT_RECEIVED")

    async def cleanup(_app: web.Application) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.on_cleanup.append(cleanup)
    app.router.add_get("/health", health)
    app.router.add_get("/", challenge)
    app.router.add_post("/", webhook)
    app.router.add_get("/{tail:.*}", challenge)
    app.router.add_post("/{tail:.*}", webhook)
    return app


async def main() -> None:
    async with RoomClient() as room:
        receive_from_http = os.getenv("MESHAGENT_SAMPLE_QUEUE_MODE") != "1"
        channel = create_channel(room=room, receive_from_http=receive_from_http)
        if not receive_from_http:
            await run_external_channel(channel)
            return
        app = create_app(
            channel=channel,
            app_secret=_required_env("WHATSAPP_APP_SECRET"),
            verify_token=_required_env("WHATSAPP_VERIFY_TOKEN"),
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            os.getenv("MESHAGENT_HOST", "0.0.0.0"),
            int(os.getenv("MESHAGENT_PORT", "8000")),
        )
        await site.start()
        try:
            await run_external_channel(channel)
        finally:
            await runner.cleanup()


__all__ = ["WhatsAppChannel", "create_app", "create_channel"]


if __name__ == "__main__":
    asyncio.run(main())
