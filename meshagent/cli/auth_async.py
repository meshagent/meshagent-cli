import asyncio
import base64
import hashlib
import inspect
import json
import os
import secrets
import time
import webbrowser
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

from aiohttp import ClientSession, web

from meshagent.api.client import Meshagent, User
from meshagent.api.oauth_scopes import FULL_OAUTH_SCOPE
from meshagent.cli.local_settings import (
    LOCAL_STATE_USER_ID,
    StoredSession,
    StoredUserProfile,
    apply_active_profile_api_url_environment,
    clear_active_session,
    get_active_profile,
    get_active_session,
    get_active_user_id,
    resolve_api_url,
    save_authenticated_profile,
    set_active_session,
    set_local_session,
)

REDIRECT_PORT = 8765
REDIRECT_URL = f"http://localhost:{REDIRECT_PORT}/callback"


def _now() -> int:
    return int(time.time())


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    """
    Returns (code_verifier, code_challenge) using S256 per RFC 7636.
    """
    verifier = _b64url_no_pad(secrets.token_bytes(32))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = _b64url_no_pad(digest)
    return verifier, challenge


def _api_base(*, api_url: str | None = None) -> str:
    return resolve_api_url(api_url=api_url)


def _authorization_url(*, api_url: str | None = None) -> str:
    return f"{_api_base(api_url=api_url)}/oauth/authorize"


def _token_url(*, api_url: str | None = None) -> str:
    return f"{_api_base(api_url=api_url)}/oauth/token"


def _client_id() -> str:
    cid = os.getenv("MESHAGENT_OAUTH_CLIENT_ID", "p8xy1ZUi73jJUJbNfTg92HUSDpCSZJcc")
    if not cid:
        raise RuntimeError("MESHAGENT_OAUTH_CLIENT_ID is not set")
    return cid


def _client_secret() -> str | None:
    return os.getenv("MESHAGENT_OAUTH_CLIENT_SECRET")


def _scopes() -> str:
    return os.getenv("MESHAGENT_OAUTH_SCOPES", FULL_OAUTH_SCOPE)


def _stored_session_from_tokens(tokens: dict[str, object]) -> StoredSession:
    return StoredSession.model_validate(
        {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": tokens.get("expires_at"),
            "token_type": tokens.get("token_type", "Bearer"),
            "scope": tokens.get("scope"),
            "id_token": tokens.get("id_token"),
        }
    )


def _tokens_from_stored_session(session: StoredSession) -> dict[str, object]:
    return session.model_dump(mode="json")


async def _fetch_authenticated_profile(
    *,
    access_token: str,
    api_url: str,
) -> StoredUserProfile:
    client = Meshagent(base_url=api_url, token=access_token)
    try:
        user = User.model_validate(await client.get_user_profile("me"))
    finally:
        await client.close()

    return StoredUserProfile.from_user(user)


async def _persist_tokens(
    *,
    tokens: dict[str, object],
    api_url: str,
    resolve_profile: bool,
    fallback_to_local_state: bool,
) -> None:
    stored_session = _stored_session_from_tokens(tokens)
    access_token = stored_session.access_token

    if resolve_profile and access_token is not None:
        try:
            profile = await _fetch_authenticated_profile(
                access_token=access_token,
                api_url=api_url,
            )
        except Exception:
            if fallback_to_local_state:
                set_local_session(session=stored_session, api_url=api_url)
            else:
                set_active_session(session=stored_session, api_url=api_url)
        else:
            save_authenticated_profile(
                profile=profile,
                session=stored_session,
                api_url=api_url,
            )
    else:
        set_active_session(session=stored_session, api_url=api_url)

    apply_active_profile_api_url_environment()


async def _post_form(url: str, form: dict[str, str]) -> dict[str, object]:
    """
    POST application/x-www-form-urlencoded and return parsed JSON or raise.
    """
    headers = {"Accept": "application/json"}
    async with ClientSession() as session:
        async with session.post(url, data=form, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Token endpoint error {resp.status}: {text}")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Unexpected non-JSON response from token endpoint: {text}"
                ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected token payload.")

    return payload


async def _wait_for_code(expected_state: str) -> str:
    """
    Spin up a one-shot aiohttp server and await ?code=…&state=…
    Validates 'state' if provided. Returns the 'code'.
    """
    app = web.Application()
    code_fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def callback(request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if expected_state and state != expected_state:
            return web.Response(status=400, text="State mismatch. Close this tab.")
        if code is not None and code != "":
            if not code_fut.done():
                code_fut.set_result(code)
            return web.Response(text="You may close this tab.")
        return web.Response(status=400, text="Missing 'code'.")

    app.add_routes([web.get("/callback", callback)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", REDIRECT_PORT)
    await site.start()

    try:
        return await code_fut
    finally:
        await runner.cleanup()


async def _exchange_code_for_tokens(
    code: str,
    code_verifier: str,
    *,
    api_url: str,
) -> dict[str, object]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URL,
        "client_id": _client_id(),
        "code_verifier": code_verifier,
    }

    client_secret = _client_secret()
    if client_secret is not None:
        form["client_secret"] = client_secret

    token_json = await _post_form(_token_url(api_url=api_url), form)

    expires_in = int(token_json.get("expires_in", 3600))
    token_json["expires_at"] = _now() + max(0, expires_in - 30)
    return token_json


async def _refresh_tokens(
    tokens: dict[str, object],
    *,
    api_url: str,
) -> dict[str, object]:
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or refresh_token == "":
        raise RuntimeError("No refresh token available to refresh access token.")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _client_id(),
    }
    client_secret = _client_secret()
    if client_secret is not None:
        form["client_secret"] = client_secret

    token_json = await _post_form(_token_url(api_url=api_url), form)
    token_json["refresh_token"] = token_json.get("refresh_token", refresh_token)
    expires_in = int(token_json.get("expires_in", 3600))
    token_json["expires_at"] = _now() + max(0, expires_in - 30)
    return token_json


LoginStatusHandler = Callable[[str], Awaitable[None] | None]


async def _emit_login_status(
    *,
    message: str,
    status_handler: Optional[LoginStatusHandler],
    print_status: bool,
) -> None:
    if status_handler is not None:
        maybe_awaitable = status_handler(message)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable

    if print_status:
        print(message)


async def login(
    *,
    status_handler: Optional[LoginStatusHandler] = None,
    print_status: bool = True,
    api_url: str | None = None,
) -> None:
    """
    Launches the system browser for OAuth 2.0 Authorization Code + PKCE.
    Persists tokens to ~/.meshagent/settings.json under the authenticated user.
    """
    resolved_api_url = resolve_api_url(api_url=api_url)
    authz = _authorization_url(api_url=resolved_api_url)
    client_id = _client_id()
    scope = _scopes()

    code_verifier, code_challenge = _pkce_pair()
    state = _b64url_no_pad(secrets.token_bytes(16))

    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URL,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"{authz}?{urlencode(query)}"

    await asyncio.to_thread(webbrowser.open, auth_url)
    await _emit_login_status(
        message=f"Waiting for auth redirect on {auth_url}…",
        status_handler=status_handler,
        print_status=print_status,
    )

    auth_code = await _wait_for_code(state)
    await _emit_login_status(
        message="Got code, exchanging…",
        status_handler=status_handler,
        print_status=print_status,
    )

    tokens = await _exchange_code_for_tokens(
        auth_code,
        code_verifier,
        api_url=resolved_api_url,
    )
    await _persist_tokens(
        tokens=tokens,
        api_url=resolved_api_url,
        resolve_profile=True,
        fallback_to_local_state=True,
    )
    await _emit_login_status(
        message="✅ Logged in (tokens cached).",
        status_handler=status_handler,
        print_status=print_status,
    )


async def session():
    """
    Returns a tuple (client, tokens_dict)
    - client is None (kept for backward compatibility with prior signature).
    - tokens_dict contains access_token, refresh_token, expires_at, token_type, scope, id_token.
    Will auto-refresh if expired/near-expiry and update the active profile cache.
    """
    stored_session = get_active_session()
    access_token = stored_session.access_token if stored_session is not None else None
    if not isinstance(access_token, str) or access_token == "":
        return None, None

    tokens = _tokens_from_stored_session(stored_session)
    resolved_api_url = resolve_api_url()
    active_user_id = get_active_user_id()
    should_resolve_profile = (
        active_user_id is None
        or active_user_id == LOCAL_STATE_USER_ID
        or get_active_profile() is None
    )

    if (
        stored_session.expires_at is None
        or stored_session.expires_at <= _now() + 5 * 60
    ):
        try:
            tokens = await _refresh_tokens(tokens, api_url=resolved_api_url)
            await _persist_tokens(
                tokens=tokens,
                api_url=resolved_api_url,
                resolve_profile=should_resolve_profile,
                fallback_to_local_state=should_resolve_profile,
            )
        except Exception as exc:
            print(f"⚠️  Token refresh failed: {exc}")
            return None, None
    elif should_resolve_profile:
        try:
            await _persist_tokens(
                tokens=tokens,
                api_url=resolved_api_url,
                resolve_profile=True,
                fallback_to_local_state=True,
            )
        except Exception:
            pass

    return None, tokens


async def logout() -> None:
    """
    Clears the cached tokens for the active local profile.
    """
    clear_active_session()
    apply_active_profile_api_url_environment()
    print("👋 Signed out")


async def get_access_token() -> str | None:
    """
    Returns a fresh access token, refreshing if needed.
    """
    _, tokens = await session()
    raw_access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if isinstance(raw_access_token, str) and raw_access_token != "":
        return raw_access_token
    return None
