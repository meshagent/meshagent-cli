from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aiohttp import BasicAuth, web
from aiohttp.test_utils import TestClient, TestServer
from meshagent.api.http import new_client_session


TEMPLATES = Path(__file__).parent / "create_project_templates" / "python"


def _load_sample(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, ModuleType]:
    root = TEMPLATES / f"{provider}-channel"
    channel_name = f"_meshagent_sample_{provider}_channel"
    server_name = f"_meshagent_sample_{provider}_server"
    channel_spec = importlib.util.spec_from_file_location(
        channel_name, root / "channel.py"
    )
    assert channel_spec is not None and channel_spec.loader is not None
    channel_module = importlib.util.module_from_spec(channel_spec)
    sys.modules[channel_name] = channel_module
    channel_spec.loader.exec_module(channel_module)

    monkeypatch.setitem(sys.modules, "channel", channel_module)
    server_spec = importlib.util.spec_from_file_location(
        server_name, root / "server.py"
    )
    assert server_spec is not None and server_spec.loader is not None
    server_module = importlib.util.module_from_spec(server_spec)
    sys.modules[server_name] = server_module
    server_spec.loader.exec_module(server_module)
    return channel_module, server_module


class _FakeChannel:
    def __init__(self) -> None:
        self.bodies: list[str] = []
        self.received = __import__("asyncio").Event()

    async def process_webhook(self, body: str) -> None:
        self.bodies.append(body)
        self.received.set()


@pytest.mark.asyncio
async def test_slack_sample_verifies_http_signature_and_returns_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel, server = _load_sample("slack", monkeypatch)
    fake = _FakeChannel()
    now = int(time.time())
    body = json.dumps({"type": "url_verification", "challenge": "verified"}).encode()
    signature = (
        "v0="
        + hmac.new(
            b"signing-secret", f"v0:{now}:".encode() + body, hashlib.sha256
        ).hexdigest()
    )
    async with TestClient(
        TestServer(
            server.create_app(
                channel=fake, signing_secret="signing-secret", clock=lambda: now
            )
        )
    ) as client:
        response = await client.post(
            "/slack/events",
            data=body,
            headers={
                "X-Slack-Request-Timestamp": str(now),
                "X-Slack-Signature": signature,
            },
        )
        assert response.status == 200
        assert await response.text() == "verified"
        rejected = await client.post(
            "/slack/events",
            data=body,
            headers={
                "X-Slack-Request-Timestamp": str(now),
                "X-Slack-Signature": "v0=invalid",
            },
        )
        assert rejected.status == 403


@pytest.mark.asyncio
async def test_telegram_sample_verifies_secret_and_acknowledges_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel, server = _load_sample("telegram", monkeypatch)
    fake = _FakeChannel()
    body = json.dumps({"update_id": 1, "message": {"message_id": 2}})
    async with TestClient(
        TestServer(server.create_app(channel=fake, webhook_secret="webhook-secret"))
    ) as client:
        response = await client.post(
            "/telegram/webhook",
            data=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )
        assert response.status == 200
        assert await response.json() == {"ok": True}
        await fake.received.wait()
        assert fake.bodies == [body]
        rejected = await client.post(
            "/telegram/webhook",
            data=body,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert rejected.status == 403


@pytest.mark.asyncio
async def test_twilio_sample_verifies_signature_and_returns_twiml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel, server = _load_sample("twilio", monkeypatch)
    fake = _FakeChannel()
    body = b"MessageSid=SM1&From=%2B15550001&To=%2B15550002&Body=Hello"
    url = "https://provider.example/twilio"
    signed = url + "".join(
        f"{name}{value}" for name, value in sorted(server.parse_qsl(body.decode()))
    )
    signature = base64.b64encode(
        hmac.new(b"auth-token", signed.encode(), hashlib.sha1).digest()
    ).decode()
    async with TestClient(
        TestServer(server.create_app(channel=fake, auth_token="auth-token"))
    ) as client:
        response = await client.post(
            "/twilio",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "provider.example",
                "X-Twilio-Signature": signature,
            },
        )
        assert response.status == 200
        assert response.content_type == "application/xml"
        assert await response.text() == "<Response></Response>"
        await fake.received.wait()
        rejected = await client.post(
            "/twilio",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "provider.example",
                "X-Twilio-Signature": "invalid",
            },
        )
        assert rejected.status == 403


@pytest.mark.asyncio
async def test_whatsapp_sample_verifies_challenge_and_payload_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _channel, server = _load_sample("whatsapp", monkeypatch)
    fake = _FakeChannel()
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    async with TestClient(
        TestServer(
            server.create_app(
                channel=fake,
                app_secret="app-secret",
                verify_token="verify-token",
            )
        )
    ) as client:
        challenge = await client.get(
            "/whatsapp?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=42"
        )
        assert challenge.status == 200
        assert await challenge.text() == "42"
        response = await client.post(
            "/whatsapp", data=body, headers={"X-Hub-Signature-256": signature}
        )
        assert response.status == 200
        assert await response.text() == "EVENT_RECEIVED"
        await fake.received.wait()
        rejected = await client.post(
            "/whatsapp",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=invalid"},
        )
        assert rejected.status == 403


async def _start_provider(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_sample_channels_post_provider_specific_outbound_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, Any, str]] = []

    async def slack(request: web.Request) -> web.Response:
        requests.append(
            (
                "slack",
                request.path,
                await request.json(),
                request.headers["Authorization"],
            )
        )
        return web.json_response({"ok": True, "ts": "1"})

    async def telegram(request: web.Request) -> web.Response:
        requests.append(("telegram", request.path, await request.json(), ""))
        return web.json_response({"ok": True, "result": {"message_id": 1}})

    async def twilio(request: web.Request) -> web.Response:
        requests.append(
            (
                "twilio",
                request.path,
                dict(await request.post()),
                request.headers["Authorization"],
            )
        )
        return web.json_response({"sid": "SM2"})

    async def whatsapp(request: web.Request) -> web.Response:
        requests.append(
            (
                "whatsapp",
                request.path,
                await request.json(),
                request.headers["Authorization"],
            )
        )
        return web.json_response({"messages": [{"id": "wamid.1"}]})

    app = web.Application()
    app.router.add_post("/slack/chat.postMessage", slack)
    app.router.add_post("/telegram/botbot-token/sendMessage", telegram)
    app.router.add_post("/twilio/Accounts/AC1/Messages.json", twilio)
    app.router.add_post("/whatsapp/phone-id/messages", whatsapp)
    runner, base_url = await _start_provider(app)
    try:
        slack_module, _ = _load_sample("slack", monkeypatch)
        slack_channel = slack_module.SlackChannel(
            room=object(),
            bot_token="bot-token",
            api_base_url=f"{base_url}/slack",
            receive_from_http=True,
        )
        slack_channel._http_session = new_client_session(
            headers={"Authorization": "Bearer bot-token"}
        )
        await slack_channel._send_slack_message(
            channel="C1", text="Slack reply", thread_ts="100.1"
        )
        await slack_channel._http_session.close()

        telegram_module, _ = _load_sample("telegram", monkeypatch)
        telegram_channel = telegram_module.TelegramWebhookChannel(
            room=object(),
            bot_token="bot-token",
            bot_api_base_url=f"{base_url}/telegram",
            receive_from_http=True,
        )
        await telegram_channel._send_telegram_text(chat_id=7, text="Telegram reply")

        twilio_module, _ = _load_sample("twilio", monkeypatch)
        monkeypatch.setattr(twilio_module, "TWILIO_API_BASE_URL", f"{base_url}/twilio")
        twilio_channel = twilio_module.TwilioChannel(
            room=object(),
            account_sid="AC1",
            auth_token="auth-token",
            receive_from_http=True,
        )
        twilio_channel._http_session = new_client_session(
            auth=BasicAuth("AC1", "auth-token")
        )
        await twilio_channel._send_twilio_message(
            from_number="+15550002", to_number="+15550001", body="Twilio reply"
        )
        await twilio_channel._http_session.close()

        whatsapp_module, _ = _load_sample("whatsapp", monkeypatch)
        whatsapp_channel = whatsapp_module.WhatsAppChannel(
            room=object(),
            access_token="access-token",
            phone_number_id="phone-id",
            graph_api_base_url=f"{base_url}/whatsapp",
            receive_from_http=True,
        )
        whatsapp_channel._http_session = new_client_session(
            headers={"Authorization": "Bearer access-token"}
        )
        await whatsapp_channel.send_text_message(
            to_number="15550001", body="WhatsApp reply"
        )
        await whatsapp_channel._http_session.close()
    finally:
        await runner.cleanup()

    assert requests[0] == (
        "slack",
        "/slack/chat.postMessage",
        {"channel": "C1", "text": "Slack reply", "thread_ts": "100.1"},
        "Bearer bot-token",
    )
    assert requests[1][0:3] == (
        "telegram",
        "/telegram/botbot-token/sendMessage",
        {"chat_id": 7, "text": "Telegram reply"},
    )
    assert requests[2][0:3] == (
        "twilio",
        "/twilio/Accounts/AC1/Messages.json",
        {"From": "+15550002", "To": "+15550001", "Body": "Twilio reply"},
    )
    assert requests[2][3].startswith("Basic ")
    assert requests[3] == (
        "whatsapp",
        "/whatsapp/phone-id/messages",
        {
            "messaging_product": "whatsapp",
            "to": "15550001",
            "type": "text",
            "text": {"body": "WhatsApp reply"},
        },
        "Bearer access-token",
    )
