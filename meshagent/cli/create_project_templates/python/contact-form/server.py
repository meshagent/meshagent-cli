from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import html
import json
import logging
import os
import re
import smtplib
import time
import webbrowser
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4

from aiohttp import web


logging.basicConfig(
    level=os.getenv("CONTACT_FORM_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("contact_form")

DEFAULT_MAIL_DOMAIN = "mail.meshagent.com"
DEFAULT_FROM_ADDRESS = f"contact@{DEFAULT_MAIL_DOMAIN}"
DEFAULT_TO_ADDRESS = ""
PRIVATE_MAILBOX_PERMISSION_ERROR = "5.7.1 Permission denied"
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,63}$", re.IGNORECASE)
PHONE_RE = re.compile(r"^\+?[0-9()\-\s]{7,20}$")

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Contact</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f7fb;
      color: #152033;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 32px 16px;
    }
    main {
      width: min(100%, 680px);
      background: #ffffff;
      border: 1px solid #d8dee9;
      border-radius: 8px;
      padding: clamp(20px, 4vw, 36px);
      box-shadow: 0 18px 45px rgba(31, 43, 68, 0.11);
    }
    h1 {
      margin: 0 0 8px;
      font-size: 2rem;
      line-height: 1.1;
      letter-spacing: 0;
    }
    p {
      margin: 0 0 22px;
      color: #4f5f75;
      line-height: 1.55;
    }
    form {
      display: grid;
      gap: 16px;
    }
    label {
      display: grid;
      gap: 7px;
      font-weight: 650;
      color: #25324a;
    }
    input, textarea, button {
      width: 100%;
      font: inherit;
      border-radius: 6px;
    }
    input, textarea {
      border: 1px solid #bfcbdb;
      padding: 11px 12px;
      background: #ffffff;
      color: #152033;
    }
    textarea {
      min-height: 150px;
      resize: vertical;
    }
    button {
      min-height: 44px;
      border: 0;
      background: #166b5c;
      color: #ffffff;
      font-weight: 700;
      cursor: pointer;
    }
    button:hover { background: #115649; }
    .notice {
      border-radius: 6px;
      margin: 0 0 18px;
      padding: 11px 12px;
      line-height: 1.45;
    }
    .error {
      background: #fff1f0;
      border: 1px solid #ffc9c4;
      color: #9f1c13;
    }
    .success {
      background: #eefbf4;
      border: 1px solid #b8e9cf;
      color: #166534;
    }
  </style>
</head>
<body>
  <main>
    <h1>Contact</h1>
    <p>Send a note and include the best way to reach you.</p>
    __FLASH__
    <form method="post" action="/contact">
      <label>Name
        <input name="name" maxlength="80" required value="__NAME__">
      </label>
      <label>Email
        <input name="email" type="email" maxlength="120" value="__EMAIL__">
      </label>
      <label>Phone
        <input name="phone" type="tel" maxlength="20" pattern="\\+?[0-9()\\-\\s]{7,20}" value="__PHONE__">
      </label>
      <label>Message
        <textarea name="message" maxlength="4000" required>__MESSAGE__</textarea>
      </label>
      <button type="submit">Send message</button>
    </form>
  </main>
</body>
</html>"""


def sanitize_single_line(value: str, *, max_len: int) -> str:
    value = (value or "").strip().replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[<>]", "", value)
    return value[:max_len]


def sanitize_multiline(value: str, *, max_len: int) -> str:
    value = (value or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\x00", "")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value[:max_len]


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.fullmatch(value or ""))


def is_valid_phone(value: str) -> bool:
    if not PHONE_RE.fullmatch(value or ""):
        return False
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15


def room_slug_from_name(room_name: str) -> str:
    room_slug = re.sub(r"[^a-z0-9]+", "-", room_name.strip().lower())
    room_slug = re.sub(r"-+", "-", room_slug).strip("-")[:56]
    return room_slug or "room"


def mail_domain() -> str:
    return (
        os.getenv("MESHAGENT_MAIL_DOMAIN", DEFAULT_MAIL_DOMAIN)
        .strip()
        .removeprefix("@")
        or DEFAULT_MAIL_DOMAIN
    )


def default_from_address() -> str:
    room_name = os.getenv("MESHAGENT_ROOM", "").strip()
    if room_name:
        return f"contact-{room_slug_from_name(room_name)}@{mail_domain()}"
    return DEFAULT_FROM_ADDRESS


def render_page(*, flash: str = "", values: dict[str, str] | None = None) -> str:
    values = values or {}
    return (
        PAGE.replace("__FLASH__", flash)
        .replace("__NAME__", html.escape(values.get("name", ""), quote=True))
        .replace("__EMAIL__", html.escape(values.get("email", ""), quote=True))
        .replace("__PHONE__", html.escape(values.get("phone", ""), quote=True))
        .replace("__MESSAGE__", html.escape(values.get("message", "")))
    )


def smtp_username_from_meshagent_token() -> str | None:
    token = os.getenv("MESHAGENT_TOKEN", "").strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (binascii.Error, TypeError, ValueError):
        return None
    username = decoded.get("name")
    if isinstance(username, str) and username.strip() != "":
        return username.strip()
    return None


def _smtp_config() -> tuple[str | None, str | None, int, str]:
    from_address, _ = _email_config()
    fallback_hostname = from_address.rsplit("@", 1)[1]
    hostname = (
        os.getenv("SMTP_HOSTNAME")
        or os.getenv("MESHAGENT_MAIL_DOMAIN")
        or fallback_hostname
    ).strip()
    if not hostname:
        raise RuntimeError(
            "SMTP_HOSTNAME is not configured. Set SMTP_HOSTNAME or use a "
            "CONTACT_FORM_FROM mailbox address on a MeshAgent mail domain."
        )

    port = int(os.getenv("SMTP_PORT", "587"))
    return (
        os.getenv("SMTP_USERNAME") or smtp_username_from_meshagent_token(),
        os.getenv("SMTP_PASSWORD") or os.getenv("MESHAGENT_TOKEN"),
        port,
        hostname,
    )


def _email_config() -> tuple[str, str]:
    from_address = (os.getenv("CONTACT_FORM_FROM") or default_from_address()).strip()
    to_address = os.getenv("CONTACT_FORM_TO", DEFAULT_TO_ADDRESS).strip()
    if not is_valid_email(from_address):
        raise ValueError("CONTACT_FORM_FROM must be a valid mailbox address")
    if not is_valid_email(to_address):
        raise ValueError("CONTACT_FORM_TO must be a valid recipient address")
    return from_address, to_address


def _flash(*, message: str, kind: str) -> str:
    return f'<p class="notice {kind}">{html.escape(message)}</p>'


def _log_event(event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    LOGGER.info(json.dumps(payload, sort_keys=True))


def _log_details_enabled() -> bool:
    return os.getenv("CONTACT_FORM_LOG_DETAILS", "").strip().lower() in TRUE_ENV_VALUES


def _address_domain(address: str) -> str:
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1]


def _with_log_details(
    fields: dict[str, object],
    details: dict[str, object],
) -> dict[str, object]:
    if _log_details_enabled():
        fields.update(details)
    return fields


def _decode_smtp_response(response: object) -> str:
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    return str(response)


def mail_error_message(exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    if "CONTACT_FORM_TO" in detail:
        return (
            "Set CONTACT_FORM_TO to the address that should receive submissions "
            "before sending."
        )
    return f"Unable to send mail: {detail}"


def _build_message(values: dict[str, str]) -> EmailMessage:
    from_address, to_address = _email_config()
    msg = EmailMessage()
    msg["Subject"] = f"Contact form submission from {values['name']}"
    msg["From"] = from_address
    msg["To"] = to_address
    msg["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[1])
    if values["email"]:
        msg["Reply-To"] = values["email"]
    msg.set_content(
        "New contact form submission\n\n"
        f"Name: {values['name']}\n"
        f"Email: {values['email'] or '(not provided)'}\n"
        f"Phone: {values['phone'] or '(not provided)'}\n\n"
        f"Message:\n{values['message']}\n"
    )
    return msg


def _is_private_mailbox_permission_error(exc: smtplib.SMTPDataError) -> bool:
    detail = str(exc)
    smtp_error = exc.smtp_error
    if isinstance(smtp_error, bytes):
        detail += " " + smtp_error.decode("utf-8", errors="replace")
    else:
        detail += " " + str(smtp_error)
    return PRIVATE_MAILBOX_PERMISSION_ERROR in detail


def _send_message(
    msg: EmailMessage,
    *,
    submission_id: str,
    attempt: str,
    username: str | None,
    password: str | None,
    port: int,
    hostname: str,
    from_address: str,
    to_address: str,
) -> None:
    use_starttls = os.getenv("SMTP_STARTTLS", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    start = time.monotonic()
    _log_event(
        "smtp_send_start",
        **_with_log_details(
            {
                "submission_id": submission_id,
                "attempt": attempt,
                "hostname": hostname,
                "port": port,
                "starttls": use_starttls,
                "auth_configured": bool(username and password),
                "from_domain": _address_domain(from_address),
                "to_domain": _address_domain(to_address),
                "message_id": str(msg.get("Message-ID", "")),
            },
            {
                "username": username or "",
                "password_configured": bool(password),
                "from_address": from_address,
                "to_address": to_address,
                "subject": str(msg.get("Subject", "")),
            },
        ),
    )
    with smtplib.SMTP(hostname, port, timeout=20) as smtp:
        if use_starttls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg, from_addr=from_address, to_addrs=[to_address])

    _log_event(
        "smtp_send_succeeded",
        **_with_log_details(
            {
                "submission_id": submission_id,
                "attempt": attempt,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "from_domain": _address_domain(from_address),
                "to_domain": _address_domain(to_address),
                "message_id": str(msg.get("Message-ID", "")),
            },
            {
                "from_address": from_address,
                "to_address": to_address,
            },
        ),
    )


def _send_email(msg: EmailMessage, *, submission_id: str) -> None:
    username, password, port, hostname = _smtp_config()
    from_address, to_address = _email_config()
    try:
        _send_message(
            msg,
            submission_id=submission_id,
            attempt="configured",
            username=username,
            password=password,
            port=port,
            hostname=hostname,
            from_address=from_address,
            to_address=to_address,
        )
    except smtplib.SMTPDataError as exc:
        if not _is_private_mailbox_permission_error(exc):
            raise
        _log_event(
            "smtp_private_mailbox_retry",
            **_with_log_details(
                {
                    "submission_id": submission_id,
                    "from_domain": _address_domain(from_address),
                    "retry_from_domain": _address_domain(to_address),
                    "to_domain": _address_domain(to_address),
                    "smtp_code": exc.smtp_code,
                },
                {
                    "from_address": from_address,
                    "retry_from_address": to_address,
                    "to_address": to_address,
                    "smtp_error": _decode_smtp_response(exc.smtp_error),
                },
            ),
        )
        _send_message(
            msg,
            submission_id=submission_id,
            attempt="private_mailbox_retry",
            username=username,
            password=password,
            port=port,
            hostname=hostname,
            from_address=to_address,
            to_address=to_address,
        )


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


async def status(request: web.Request) -> web.Response:
    return web.Response(text="ready\n", content_type="text/plain")


async def ping(request: web.Request) -> web.Response:
    return web.json_response({"pong": True})


async def index(request: web.Request) -> web.Response:
    return web.Response(text=render_page(), content_type="text/html")


async def submit_contact(request: web.Request) -> web.Response:
    submission_id = uuid4().hex[:12]
    data = await request.post()
    values = {
        "name": sanitize_single_line(str(data.get("name", "")), max_len=80),
        "email": sanitize_single_line(str(data.get("email", "")), max_len=120),
        "phone": sanitize_single_line(str(data.get("phone", "")), max_len=20),
        "message": sanitize_multiline(str(data.get("message", "")), max_len=4000),
    }
    _log_event(
        "contact_form_submit_received",
        **_with_log_details(
            {
                "submission_id": submission_id,
                "reply_email_provided": bool(values["email"]),
                "phone_provided": bool(values["phone"]),
                "message_chars": len(values["message"]),
            },
            {
                "remote": request.remote or "",
                "name": values["name"],
                "reply_email": values["email"],
            },
        ),
    )

    if not values["name"]:
        _log_event(
            "contact_form_validation_failed",
            submission_id=submission_id,
            reason="missing_name",
        )
        return web.Response(
            text=render_page(
                flash=_flash(message="Name is required.", kind="error"),
                values=values,
            ),
            content_type="text/html",
            status=400,
        )
    if not values["message"]:
        _log_event(
            "contact_form_validation_failed",
            submission_id=submission_id,
            reason="missing_message",
        )
        return web.Response(
            text=render_page(
                flash=_flash(message="Message is required.", kind="error"),
                values=values,
            ),
            content_type="text/html",
            status=400,
        )
    if not values["email"] and not values["phone"]:
        _log_event(
            "contact_form_validation_failed",
            submission_id=submission_id,
            reason="missing_contact_method",
        )
        return web.Response(
            text=render_page(
                flash=_flash(
                    message="Provide a valid email and/or phone number.",
                    kind="error",
                ),
                values=values,
            ),
            content_type="text/html",
            status=400,
        )
    if values["email"] and not is_valid_email(values["email"]):
        _log_event(
            "contact_form_validation_failed",
            submission_id=submission_id,
            reason="invalid_email",
        )
        return web.Response(
            text=render_page(
                flash=_flash(
                    message="Please enter a valid email address.", kind="error"
                ),
                values=values,
            ),
            content_type="text/html",
            status=400,
        )
    if values["phone"] and not is_valid_phone(values["phone"]):
        _log_event(
            "contact_form_validation_failed",
            submission_id=submission_id,
            reason="invalid_phone",
        )
        return web.Response(
            text=render_page(
                flash=_flash(
                    message="Please enter a valid phone number.", kind="error"
                ),
                values=values,
            ),
            content_type="text/html",
            status=400,
        )

    msg = _build_message(values)
    _log_event(
        "contact_form_submit_validated",
        **_with_log_details(
            {
                "submission_id": submission_id,
                "from_domain": _address_domain(str(msg.get("From", ""))),
                "to_domain": _address_domain(str(msg.get("To", ""))),
                "reply_to_provided": bool(msg.get("Reply-To")),
                "message_id": str(msg.get("Message-ID", "")),
            },
            {
                "from_address": str(msg.get("From", "")),
                "to_address": str(msg.get("To", "")),
                "reply_to": str(msg.get("Reply-To", "")),
                "subject": str(msg.get("Subject", "")),
            },
        ),
    )
    try:
        await asyncio.to_thread(_send_email, msg, submission_id=submission_id)
    except Exception as exc:
        failure = {
            "event": "contact_form_send_failed",
            "submission_id": submission_id,
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, smtplib.SMTPResponseException):
            smtp_code = exc.smtp_code
            failure["smtp_code"] = smtp_code
        if _log_details_enabled():
            failure["error"] = str(exc)
            LOGGER.exception(json.dumps(failure, sort_keys=True))
        else:
            LOGGER.error(json.dumps(failure, sort_keys=True))
        return web.Response(
            text=render_page(
                flash=_flash(
                    message=mail_error_message(exc),
                    kind="error",
                ),
                values=values,
            ),
            content_type="text/html",
            status=500,
        )

    _log_event(
        "contact_form_send_succeeded",
        submission_id=submission_id,
        message_id=str(msg.get("Message-ID", "")),
    )
    return web.Response(
        text=render_page(
            flash=_flash(message="Thanks - your message has been sent.", kind="success")
        ),
        content_type="text/html",
    )


async def open_local_contact_form() -> None:
    url = os.getenv("CONTACT_FORM_LOCAL_URL")
    if not url or os.getenv("CONTACT_FORM_OPEN_BROWSER", "1") == "0":
        return
    await asyncio.sleep(0.2)
    print(f"Opening contact form at {url}", flush=True)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(webbrowser.open, url)


async def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/api/ping", ping)
    app.router.add_get("/", index)
    app.router.add_get("/contact", index)
    app.router.add_post("/contact", submit_contact)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Serving contact form on 0.0.0.0:{port}", flush=True)
    await open_local_contact_form()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped contact form.")
