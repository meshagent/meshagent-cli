from __future__ import annotations

import asyncio
import hmac
import os

from aiohttp import web
from meshagent.agents import run_external_channel
from meshagent.api import RoomClient

from channel import TelegramChannel, TelegramWebhookChannel, create_channel


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Telegram channel.")
    return value


def create_app(
    *, channel: TelegramWebhookChannel, webhook_secret: str
) -> web.Application:
    app = web.Application()
    tasks: set[asyncio.Task[None]] = set()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def webhook(request: web.Request) -> web.Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, webhook_secret):
            raise web.HTTPForbidden()
        body = await request.read()
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise web.HTTPBadRequest() from exc
        task = asyncio.create_task(channel.process_webhook(body.decode()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.json_response({"ok": True})

    async def cleanup(_app: web.Application) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.on_cleanup.append(cleanup)
    app.router.add_get("/health", health)
    app.router.add_post("/telegram/webhook", webhook)
    return app


async def main() -> None:
    async with RoomClient() as room:
        channel = create_channel(room=room, receive_from_http=True)
        if not isinstance(channel, TelegramWebhookChannel):
            await run_external_channel(channel)
            return
        app = create_app(
            channel=channel,
            webhook_secret=_required_env("TELEGRAM_WEBHOOK_SECRET"),
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


__all__ = [
    "TelegramChannel",
    "TelegramWebhookChannel",
    "create_app",
    "create_channel",
]


if __name__ == "__main__":
    asyncio.run(main())
