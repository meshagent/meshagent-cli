from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from urllib.parse import parse_qsl

from aiohttp import web
from meshagent.agents import run_external_channel
from meshagent.api import RoomClient

from channel import TwilioChannel, create_channel


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value == "":
        raise RuntimeError(f"Set {name} before starting the Twilio channel.")
    return value


def _external_url(request: web.Request) -> str:
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0]
    host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0]
    return f"{scheme.strip()}://{host.strip()}{request.rel_url}"


def _expected_signature(
    *, url: str, body: bytes, content_type: str, secret: str
) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        signed = url + body.decode("utf-8")
    else:
        pairs = sorted(parse_qsl(request_query(url), keep_blank_values=True))
        pairs.extend(sorted(parse_qsl(body.decode("utf-8"), keep_blank_values=True)))
        signed = url + "".join(f"{name}{value}" for name, value in pairs)
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def request_query(url: str) -> str:
    return url.partition("?")[2]


def create_app(*, channel: TwilioChannel, auth_token: str) -> web.Application:
    app = web.Application()
    tasks: set[asyncio.Task[None]] = set()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def webhook(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            expected = _expected_signature(
                url=_external_url(request),
                body=body,
                content_type=request.headers.get("Content-Type", ""),
                secret=auth_token,
            )
        except UnicodeDecodeError as exc:
            raise web.HTTPBadRequest() from exc
        if not hmac.compare_digest(
            request.headers.get("X-Twilio-Signature", ""), expected
        ):
            raise web.HTTPForbidden()
        task = asyncio.create_task(channel.process_webhook(body.decode()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return web.Response(
            text="<Response></Response>", content_type="application/xml"
        )

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
            auth_token=_required_env("TWILIO_AUTH_TOKEN"),
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


__all__ = ["TwilioChannel", "create_app", "create_channel"]


if __name__ == "__main__":
    asyncio.run(main())
