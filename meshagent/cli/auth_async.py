import os
import json
import time
import base64
import hashlib
import secrets
import webbrowser
import asyncio
from pathlib import Path
from urllib.parse import urlencode
from aiohttp import web, ClientSession

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

CACHE_FILE = Path.home() / ".meshagent" / "session.json"
REDIRECT_PORT = 8765
REDIRECT_URL = f"http://localhost:{REDIRECT_PORT}/callback"

# Expected env vars:
# - MESHAGENT_API_URL (required): e.g., https://api.meshagent.com
# - MESHAGENT_OAUTH_CLIENT_ID (required)
# - MESHAGENT_OAUTH_CLIENT_SECRET (optional; only if your server requires it)
# - MESHAGENT_OAUTH_SCOPES (optional; defaults to "openid email profile")

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _ensure_cache_dir():
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _now() -> int:
    return int(time.time())


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair():
    """
    Returns (code_verifier, code_challenge) using S256 per RFC 7636.
    """
    verifier = _b64url_no_pad(secrets.token_bytes(32))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = _b64url_no_pad(digest)
    return verifier, challenge


def _api_base() -> str:
    api = os.getenv("MESHAGENT_API_URL", "https://api.meshagent.com")
    if not api:
        raise RuntimeError("MESHAGENT_API_URL is not set")
    return api.rstrip("/")


def _authorization_url() -> str:
    return f"{_api_base()}/oauth/authorize"


def _token_url() -> str:
    return f"{_api_base()}/oauth/token"


def _client_id() -> str:
    cid = os.getenv("MESHAGENT_OAUTH_CLIENT_ID", "p8xy1ZUi73jJUJbNfTg92HUSDpCSZJcc")
    if not cid:
        raise RuntimeError("MESHAGENT_OAUTH_CLIENT_ID is not set")
    return cid


def _client_secret() -> str | None:
    return os.getenv("MESHAGENT_OAUTH_CLIENT_SECRET")


def _scopes() -> str:
    return os.getenv("MESHAGENT_OAUTH_SCOPES", "admin")


def _save(tokens: dict):
    """
    Persist minimal token info to disk.
    Expected keys: access_token, refresh_token (optional), expires_at (epoch int).
    """
    _ensure_cache_dir()
    CACHE_FILE.write_text(
        json.dumps(
            {
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": tokens.get("expires_at"),
                "token_type": tokens.get("token_type", "Bearer"),
                "scope": tokens.get("scope"),
                "id_token": tokens.get("id_token"),
            }
        )
    )


def _load() -> dict | None:
    _ensure_cache_dir()
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())


async def _post_form(url: str, form: dict) -> dict:
    """
    POST application/x-www-form-urlencoded and return parsed JSON or raise.
    """
    headers = {"Accept": "application/json"}
    async with ClientSession() as s:
        async with s.post(url, data=form, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Token endpoint error {resp.status}: {text}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"Unexpected non-JSON response from token endpoint: {text}"
                )


# -----------------------------------------------------------------------------
# Local HTTP callback
# -----------------------------------------------------------------------------


async def _wait_for_code(expected_state: str) -> str:
    """
    Spin up a one-shot aiohttp server and await ?code=…&state=…
    Validates 'state' if provided. Returns the 'code'.
    """
    app = web.Application()
    code_fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def callback(request):
        code = request.query.get("code")
        state = request.query.get("state")
        if expected_state and state != expected_state:
            return web.Response(status=400, text="State mismatch. Close this tab.")
        if code:
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


# -----------------------------------------------------------------------------
# OAuth flows
# -----------------------------------------------------------------------------


async def _exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URL,
        "client_id": _client_id(),
        "code_verifier": code_verifier,
    }
    # Include client_secret only if provided (public clients typically omit)
    client_secret = _client_secret()
    if client_secret:
        form["client_secret"] = client_secret

    token_json = await _post_form(_token_url(), form)

    # Compute absolute expiry; default to 3600s if expires_in missing
    expires_in = int(token_json.get("expires_in", 3600))
    token_json["expires_at"] = _now() + max(0, expires_in - 30)  # small safety skew
    return token_json


async def _refresh_tokens(tokens: dict) -> dict:
    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError("No refresh token available to refresh access token.")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": _client_id(),
    }
    client_secret = _client_secret()
    if client_secret:
        form["client_secret"] = client_secret

    token_json = await _post_form(_token_url(), form)

    # Some servers rotate refresh tokens; keep old one if none returned
    token_json["refresh_token"] = token_json.get(
        "refresh_token", tokens.get("refresh_token")
    )
    expires_in = int(token_json.get("expires_in", 3600))
    token_json["expires_at"] = _now() + max(0, expires_in - 30)
    return token_json


# -----------------------------------------------------------------------------
# Public API (unchanged names)
# -----------------------------------------------------------------------------


async def login():
    """
    Launches the system browser for OAuth 2.0 Authorization Code + PKCE.
    Persists tokens to ~/.meshagent/session.json
    """
    authz = _authorization_url()
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

    # Kick user to browser without blocking the loop
    await asyncio.to_thread(webbrowser.open, auth_url)
    print(f"Waiting for auth redirect on {auth_url}…")

    # Await the auth code, then exchange for tokens
    auth_code = await _wait_for_code(state)
    print("Got code, exchanging…")

    tokens = await _exchange_code_for_tokens(auth_code, code_verifier)
    _save(tokens)
    print("✅ Logged in (tokens cached).")


async def session():
    """
    Returns a tuple (client, tokens_dict)
    - client is None (kept for backward compatibility with prior signature).
    - tokens_dict contains access_token, refresh_token, expires_at, token_type, scope, id_token.
    Will auto-refresh if expired/near-expiry and update the cache.
    """
    tokens = _load()
    if not tokens:
        return None, None

    # Refresh if expired or within 5 min of expiry
    if not tokens.get("expires_at") or tokens["expires_at"] <= _now() + 5 * 60:
        try:
            tokens = await _refresh_tokens(tokens)
            _save(tokens)
        except Exception as e:
            # If refresh fails, wipe session to force re-login
            print(f"⚠️  Token refresh failed: {e}")
            return None, None

    return None, tokens


async def logout():
    """
    Clears the cached tokens. (If your OAuth server supports revocation,
    you can add a call here; not provided in the spec.)
    """
    _, tokens = await session()
    # Optional: call a revocation endpoint here if your server provides one.
    CACHE_FILE.unlink(missing_ok=True)
    print("👋 Signed out")


async def get_access_token():
    """
    Returns a fresh access token, refreshing if needed.
    """
    _, tokens = await session()
    return tokens["access_token"] if tokens else None
