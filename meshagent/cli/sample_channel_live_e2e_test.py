from __future__ import annotations

import os

import pytest
from aiohttp import BasicAuth
from meshagent.api.http import new_client_session


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MESHAGENT_CHANNEL_E2E") != "1",
    reason="set RUN_MESHAGENT_CHANNEL_E2E=1 to call live channel providers",
)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


@pytest.mark.asyncio
async def test_slack_credentials_authenticate_with_live_api() -> None:
    token = _required_env("SLACK_BOT_TOKEN")
    signing_secret = _required_env("SLACK_SIGNING_SECRET")
    async with new_client_session(
        headers={"Authorization": f"Bearer {token}"}
    ) as session:
        response = await session.post("https://slack.com/api/auth.test")
        assert response.status == 200
        payload = await response.json()

    assert payload.get("ok") is True, payload.get("error", "Slack auth failed")
    assert payload.get("bot_id")
    assert payload.get("team_id")
    assert len(signing_secret) >= 16


@pytest.mark.asyncio
async def test_telegram_credentials_authenticate_with_live_api() -> None:
    token = _required_env("TELEGRAM_BOT_TOKEN")
    async with new_client_session() as session:
        response = await session.get(f"https://api.telegram.org/bot{token}/getMe")
        assert response.status == 200
        payload = await response.json()

    assert payload.get("ok") is True, "Telegram rejected the bot token"
    assert payload.get("result", {}).get("is_bot") is True
    assert payload.get("result", {}).get("id")


@pytest.mark.asyncio
async def test_twilio_credentials_authenticate_with_live_api() -> None:
    account_sid = _required_env("TWILIO_ACCOUNT_SID")
    api_key_sid = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    if api_key_sid or api_key_secret:
        assert api_key_sid and api_key_secret, (
            "TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET must be configured together"
        )
        auth = BasicAuth(api_key_sid, api_key_secret)
    else:
        auth = BasicAuth(account_sid, _required_env("TWILIO_AUTH_TOKEN"))

    async with new_client_session(auth=auth) as session:
        response = await session.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            params={"PageSize": "1"},
        )
        assert response.status == 200
        payload = await response.json()

    assert isinstance(payload.get("messages"), list)


@pytest.mark.asyncio
async def test_whatsapp_credentials_authenticate_with_live_api() -> None:
    access_token = _required_env("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = _required_env("WHATSAPP_PHONE_NUMBER_ID")
    app_secret = _required_env("WHATSAPP_APP_SECRET")
    verify_token = _required_env("WHATSAPP_VERIFY_TOKEN")
    async with new_client_session(
        headers={"Authorization": f"Bearer {access_token}"}
    ) as session:
        response = await session.get(
            f"https://graph.facebook.com/v23.0/{phone_number_id}",
            params={"fields": "id,display_phone_number,verified_name"},
        )
        assert response.status == 200
        payload = await response.json()

    assert payload.get("id") == phone_number_id
    assert payload.get("display_phone_number")
    assert len(app_secret) >= 16
    assert len(verify_token) >= 16
