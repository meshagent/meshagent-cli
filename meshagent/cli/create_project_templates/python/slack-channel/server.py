from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable

from aiohttp import web
from meshagent.agents import run_external_channel
from meshagent.api import RoomClient

from channel import SlackChannel, create_channel


MAX_SIGNATURE_AGE_SECONDS = 300


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Slack channel.")
    return value


def _verify_signature(
    *, body: bytes, signature: str, timestamp: str, secret: str, now: int
) -> bool:
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - signed_at) > MAX_SIGNATURE_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def create_app(
    *,
    channel: SlackChannel,
    signing_secret: str,
    clock: Callable[[], float] = time.time,
) -> web.Application:
    app = web.Application()
    tasks: set[asyncio.Task[None]] = set()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def webhook(request: web.Request) -> web.Response:
        body = await request.read()
        if not _verify_signature(
            body=body,
            signature=request.headers.get("X-Slack-Signature", ""),
            timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
            secret=signing_secret,
            now=int(clock()),
        ):
            raise web.HTTPForbidden()
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest() from exc
        if isinstance(payload, dict) and payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or challenge == "":
                raise web.HTTPBadRequest()
            return web.Response(text=challenge)
        task = asyncio.create_task(channel.process_webhook(body.decode()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.Response()

    async def cleanup(_app: web.Application) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.on_cleanup.append(cleanup)
    app.router.add_get("/health", health)
    app.router.add_post("/", webhook)
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
            signing_secret=_required_env("SLACK_SIGNING_SECRET"),
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


__all__ = ["SlackChannel", "create_app", "create_channel"]


if __name__ == "__main__":
    asyncio.run(main())
