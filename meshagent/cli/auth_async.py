import os
import json
import webbrowser
import asyncio
from pathlib import Path
from aiohttp import web
from supabase._async.client import (
    AsyncClient,
    create_client,
)  # async flavour :contentReference[oaicite:1]{index=1}
from supabase.lib.client_options import ClientOptions
from gotrue import AsyncMemoryStorage

AUTH_URL = os.getenv("MESHAGENT_AUTH_URL", "https://infra.meshagent.com")
AUTH_ANON_KEY = os.getenv(
    "MESHAGENT_AUTH_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5memhyeWpoc3RjZXdkeWdvampzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzQ2MzU2MjgsImV4cCI6MjA1MDIxMTYyOH0.ujx9CIbYEvWbA77ogB1gg1Jrv3KtpB1rWh_LRRLpcow",
)
CACHE_FILE = Path.home() / ".meshagent" / "session.json"
REDIRECT_PORT = 8765
REDIRECT_URL = f"http://localhost:{REDIRECT_PORT}/callback"

# ---------- helpers ----------------------------------------------------------


def _ensure_cache_dir():
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


async def _client() -> AsyncClient:
    return await create_client(
        AUTH_URL,
        AUTH_ANON_KEY,
        options=ClientOptions(
            flow_type="pkce",  # OAuth + PKCE :contentReference[oaicite:2]{index=2}
            auto_refresh_token=True,
            persist_session=False,
            storage=AsyncMemoryStorage(),
        ),
    )


def _save(s):
    _ensure_cache_dir()
    CACHE_FILE.write_text(
        json.dumps(
            {
                "access_token": s.access_token,
                "refresh_token": s.refresh_token,
                "expires_at": s.expires_at,  # int (seconds since epoch)
            }
        )
    )


def _load():
    _ensure_cache_dir()
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())


# ---------- local HTTP callback ---------------------------------------------


async def _wait_for_code() -> str:
    """Spin up a one-shot aiohttp server and await ?code=…"""
    app = web.Application()
    code_fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    async def callback(request):
        code = request.query.get("code")
        if code:
            if not code_fut.done():
                code_fut.set_result(code)
            return web.Response(text="You may close this tab.")
        return web.Response(status=400)

    app.add_routes([web.get("/callback", callback)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", REDIRECT_PORT)
    await site.start()

    try:
        return await code_fut
    finally:
        await runner.cleanup()


# ---------- public API -------------------------------------------------------


async def login():
    supa = await _client()

    # 1️⃣  Build provider URL – async now
    res = await supa.auth.sign_in_with_oauth(
        {
            "provider": "google",
            "options": {"redirect_to": REDIRECT_URL},
        }
    )  # :contentReference[oaicite:3]{index=3}
    oauth_url = res.url

    # 2️⃣  Kick user to browser without blocking the loop
    await asyncio.to_thread(webbrowser.open, oauth_url)
    print(f"Waiting for Google OAuth redirect on {oauth_url}…")

    # 3️⃣  Await the auth code, then exchange for tokens
    auth_code = await _wait_for_code()
    print("Got code, exchanging…")
    sess = await supa.auth.exchange_code_for_session({"auth_code": auth_code})  #
    _save(sess.session)
    print("✅ Logged in as", sess.user.email)


async def session():
    supa = await _client()
    cached = _load()
    fresh = None
    if cached:
        await supa.auth.set_session(cached["access_token"], cached["refresh_token"])
        fresh = await supa.auth.get_session()  # returns a Session object
        _save(fresh)
    return supa, fresh


async def logout():
    supa, s = await session()
    if s:
        await supa.auth.sign_out()
    CACHE_FILE.unlink(missing_ok=True)
    print("👋 Signed out")


async def get_access_token():
    supa, fresh = await session()
    return fresh.access_token
