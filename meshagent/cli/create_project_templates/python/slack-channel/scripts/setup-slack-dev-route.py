#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
from urllib.parse import urlparse

from meshagent.api.client import AccessSubject, NotFoundError, ServiceAccount
from meshagent.api.specs.service import (
    ContainerSpec,
    ContainerMountSpec,
    EnvironmentVariable,
    FileStorageMountSpec,
    PortSpec,
    RouteBackendSpec,
    RouteMetadata,
    RoutePathSpec,
    RouteRoomBackendSpec,
    RouteSpec,
    ServiceMetadata,
    ServiceRunAs,
    ServiceSpec,
    SecretValue,
    TokenValue,
)
from meshagent.cli.helper import get_client, resolve_project_id


DEFAULT_AGENT_NAME = "python-slack-channel"
DEFAULT_EVENTS_SERVICE_NAME = "python-slack-channel-events"
DEFAULT_EVENTS_PORT = 8000
DEFAULT_EVENTS_BASE_DOMAIN = "meshagent.dev"
CALLBACK_SCRIPT_PATH = "/app/slack_events_callback.py"


CALLBACK_SCRIPT = r"""
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time

from aiohttp import web

from meshagent.api import RoomClient


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meshagent.slack_events_callback")

MAX_SKEW_SECONDS = 300
QUEUE_NAME = os.getenv("MESHAGENT_SLACK_QUEUE_NAME", "slack-events")
SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").encode("utf-8")
ROOM_CLIENT = None
ROOM_LOCK = asyncio.Lock()
ENQUEUE_LOCK = asyncio.Lock()


def _room_connected() -> bool:
    return (
        ROOM_CLIENT is not None
        and not ROOM_CLIENT.is_closed
        and ROOM_CLIENT.is_connected
    )


async def _get_room() -> RoomClient:
    global ROOM_CLIENT

    if ROOM_CLIENT is not None and not ROOM_CLIENT.is_closed:
        return ROOM_CLIENT

    async with ROOM_LOCK:
        if ROOM_CLIENT is not None and not ROOM_CLIENT.is_closed:
            return ROOM_CLIENT

        old_room = ROOM_CLIENT
        ROOM_CLIENT = None
        if old_room is not None:
            with contextlib.suppress(Exception):
                await old_room.__aexit__(None, None, None)

        room = RoomClient()
        await room.__aenter__()
        ROOM_CLIENT = room
        logger.info("slack_callback_room_connected")
        return room


async def _cleanup_room(app: web.Application) -> None:
    del app
    global ROOM_CLIENT

    room = ROOM_CLIENT
    ROOM_CLIENT = None
    if room is not None:
        with contextlib.suppress(Exception):
            await room.__aexit__(None, None, None)
        logger.info("slack_callback_room_closed")


def _verify_slack_signature(headers, body: bytes) -> bool:
    if not SIGNING_SECRET:
        logger.warning("missing SLACK_SIGNING_SECRET")
        return False
    signature = headers.get("X-Slack-Signature", "")
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    if not signature or not timestamp:
        logger.warning("missing Slack signature headers")
        return False
    try:
        ts_int = int(timestamp)
    except ValueError:
        logger.warning("invalid Slack timestamp")
        return False
    if abs(int(time.time()) - ts_int) > MAX_SKEW_SECONDS:
        logger.warning("stale Slack timestamp")
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = "v0=" + hmac.new(SIGNING_SECRET, base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        logger.warning("invalid Slack signature")
        return False
    return True


def _payload_summary(payload):
    if not isinstance(payload, dict):
        return {
            "type": "non_json",
            "event_type": "",
            "subtype": "",
            "event_id": "",
            "channel": "",
            "user": "",
            "bot_id": "",
        }
    event = payload.get("event")
    if not isinstance(event, dict):
        event = {}
    return {
        "type": payload.get("type") or "",
        "event_type": event.get("type") or "",
        "subtype": event.get("subtype") or "",
        "event_id": payload.get("event_id") or "",
        "channel": event.get("channel") or "",
        "user": event.get("user") or "",
        "bot_id": event.get("bot_id") or "",
    }


def _log_payload(action: str, payload) -> None:
    summary = _payload_summary(payload)
    logger.info(
        "%s type=%s event_type=%s subtype=%s event_id=%s channel=%s user=%s bot_id=%s",
        action,
        summary["type"],
        summary["event_type"],
        summary["subtype"],
        summary["event_id"],
        summary["channel"],
        summary["user"],
        summary["bot_id"],
    )


async def handle(request: web.Request) -> web.Response:
    if request.method == "GET" and request.path.rstrip("/") == "/health":
        return web.json_response(
            {"ok": True, "room_connected": _room_connected()},
        )
    if request.method != "POST":
        raise web.HTTPMethodNotAllowed(request.method, ["POST"])

    body = await request.read()
    if not _verify_slack_signature(request.headers, body):
        raise web.HTTPForbidden()

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        _log_payload("slack_callback_url_verification", payload)
        challenge = payload.get("challenge")
        if isinstance(challenge, str) and challenge:
            return web.Response(text=challenge)
        raise web.HTTPForbidden()

    _log_payload("slack_callback_received", payload)
    try:
        room = await _get_room()
        async with ENQUEUE_LOCK:
            if not room.is_connected:
                raise RuntimeError("room connection is not ready")
            await room.queues.send(
                name=QUEUE_NAME,
                message={"body": body.decode("utf-8")},
                create=True,
            )
    except Exception:
        _log_payload("slack_callback_enqueue_failed", payload)
        logger.exception("slack_callback_enqueue_exception")
        raise web.HTTPServiceUnavailable(reason="failed to enqueue Slack event")

    _log_payload("slack_callback_enqueued", payload)

    raise web.HTTPAccepted()


app = web.Application()
app.on_cleanup.append(_cleanup_room)
app.router.add_route("*", "/{tail:.*}", handle)
web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
""".strip()


def _normalize_non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _service_account_matches(account: ServiceAccount, name: str) -> bool:
    candidates = {
        account.id,
        account.key,
        account.name,
        account.email,
        account.display_name,
    }
    return name in candidates


def _service_account_run_as_value(account: ServiceAccount, fallback: str) -> str:
    return str(account.email or account.name or fallback).strip()


def _secret_named(page, name: str):
    for secret in page.secrets:
        if secret.name == name:
            return secret
    return None


def _route_domain_from_url(url: str | None) -> str | None:
    url = _normalize_non_empty(url)
    if url is None:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.lower() or None


def _route_subdomain_from_room(room: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", room.lower()).strip("-")
    return slug or "python-slack-channel"


async def _configured_pages_domain() -> str | None:
    client = await get_client()
    try:
        try:
            config = await client.get_config()
        except Exception:
            return None
    finally:
        await client.close()
    value = config.domains.pages
    if isinstance(value, str):
        value = value.strip().lower().removeprefix(".")
        return value or None
    return None


async def _resolve_events_domain(
    *,
    room: str,
    explicit_domain: str | None,
    url: str | None,
    base_domain: str | None,
) -> str | None:
    explicit_domain = _normalize_non_empty(explicit_domain)
    if explicit_domain is not None:
        return explicit_domain
    url_domain = _route_domain_from_url(url)
    if url_domain is not None:
        return url_domain
    pages_domain = await _configured_pages_domain()
    domain = pages_domain or _normalize_non_empty(base_domain)
    if domain is None:
        return None
    return f"{_route_subdomain_from_room(room)}.{domain.removeprefix('.')}"


async def _resolve_service_account(
    *, client, project_id: str, name: str
) -> tuple[str, str]:
    for filter_value in (name, None):
        page = await client.list_service_accounts(
            project_id=project_id,
            page_size=100,
            filter=filter_value,
        )
        for account in page.service_accounts:
            if _service_account_matches(account, name):
                return account.id, _service_account_run_as_value(account, name)

    account = await client.create_service_account(
        project_id=project_id,
        name=name,
        display_name="Python Slack Channel",
        description="Runs the Python Slack channel service.",
        metadata=None,
        annotations=None,
    )
    return account.id, _service_account_run_as_value(account, name)


async def _find_service_account_secret_id(
    *, client, project_id: str, service_account_id: str, name: str
) -> str | None:
    page = await client.search_service_account_secrets(
        project_id=project_id,
        service_account_id=service_account_id,
        name=name,
        page_size=100,
    )
    secret = _secret_named(page, name)
    return secret.id if secret is not None else None


async def _upsert_service_account_secret(
    *,
    client,
    project_id: str,
    service_account_id: str,
    name: str,
    value: str,
) -> str:
    secret_id = await _find_service_account_secret_id(
        client=client,
        project_id=project_id,
        service_account_id=service_account_id,
        name=name,
    )
    if secret_id is None:
        secret = await client.create_service_account_secret(
            project_id=project_id,
            service_account_id=service_account_id,
            name=name,
            type="opaque",
            http_only=True,
        )
        secret_id = secret.id

    await client.create_service_account_secret_version(
        project_id=project_id,
        service_account_id=service_account_id,
        secret_id=secret_id,
        value=value.encode("utf-8"),
    )
    return secret_id


async def _grant_service_account_runtime_access(
    *, client, project_id: str, service_account_id: str
) -> None:
    whoami = await client.whoami()
    if whoami.type in {"user", "service_account"} and whoami.id:
        await client.grant_resource_policy(
            project_id=project_id,
            resource_type="service_account",
            resource_id=service_account_id,
            subject=AccessSubject(type=whoami.type, id=whoami.id),
            roles=["run_service_as"],
        )

    await client.grant_resource_policy(
        project_id=project_id,
        resource_type="service_account",
        resource_id=service_account_id,
        subject=AccessSubject(
            type="userset",
            id=project_id,
            object_type="project",
            relation="service_account",
        ),
        roles=["run_service_as", "use_proxy_secrets"],
    )


def _events_service_spec(
    *,
    service_name: str,
    room: str,
    port: int,
    signing_secret: str | None,
    service_account_email: str | None,
    signing_secret_id: str | None,
) -> ServiceSpec:
    run_as = None
    signing_secret_env = EnvironmentVariable(
        name="SLACK_SIGNING_SECRET",
        value=signing_secret,
    )
    if signing_secret is None:
        if not service_account_email or not signing_secret_id:
            raise ValueError(
                "service_account_email and signing_secret_id are required "
                "when signing_secret is not provided"
            )
        run_as = ServiceRunAs(
            email=service_account_email,
            scopes=["secrets:proxy", "secrets:read"],
        )
        signing_secret_env = EnvironmentVariable(
            name="SLACK_SIGNING_SECRET",
            secret=SecretValue(id=signing_secret_id),
        )

    return ServiceSpec(
        version="v1",
        kind="Service",
        metadata=ServiceMetadata(
            name=service_name,
            description="Slack Events API callback endpoint for local development.",
            annotations={"meshagent.service.id": service_name},
        ),
        container=ContainerSpec(
            image="meshagent/python-sdk-slim:default",
            run_as=run_as,
            command=f"python {CALLBACK_SCRIPT_PATH}",
            environment=[
                signing_secret_env,
                EnvironmentVariable(
                    name="MESHAGENT_ROOM",
                    value=room,
                ),
                EnvironmentVariable(
                    name="MESHAGENT_SLACK_QUEUE_NAME",
                    value="slack-events",
                ),
                EnvironmentVariable(
                    name="MESHAGENT_TOKEN",
                    token=TokenValue(identity=service_name, role="agent"),
                ),
            ],
            storage=ContainerMountSpec(
                files=[
                    FileStorageMountSpec(
                        path=CALLBACK_SCRIPT_PATH,
                        text=CALLBACK_SCRIPT,
                    )
                ]
            ),
        ),
        ports=[
            PortSpec(
                num=port,
                type="http",
                published=True,
                public=True,
                liveness="/health",
                annotations={},
            )
        ],
    )


async def _upsert_room_service(
    *, client, project_id: str, room: str, spec: ServiceSpec
) -> None:
    existing = None
    for service in await client.list_room_services(
        project_id=project_id, room_name=room
    ):
        if service.metadata.name == spec.metadata.name:
            existing = service
            break

    if existing is None:
        await client.create_room_service(
            project_id=project_id, room_name=room, service=spec
        )
        return

    await client.update_room_service(
        project_id=project_id,
        room_name=room,
        service_id=existing.id,
        service=spec.model_copy(update={"id": existing.id}),
    )


def _route_spec(*, domain: str, room: str, target: str) -> RouteSpec:
    return RouteSpec(
        metadata=RouteMetadata(name=domain, annotations={}),
        domain=domain,
        backend=RouteBackendSpec(room=RouteRoomBackendSpec(name=room)),
        paths=[RoutePathSpec(path="/", pathType="prefix", targetPort=target)],
    )


async def _upsert_route(
    *, client, project_id: str, domain: str, room: str, target: str
) -> None:
    spec = _route_spec(domain=domain, room=room, target=target)
    try:
        await client.get_route(project_id=project_id, domain=domain)
    except NotFoundError:
        await client.create_route(project_id=project_id, spec=spec)
        return

    await client.update_route(project_id=project_id, domain=domain, spec=spec)


async def _run(args: argparse.Namespace) -> int:
    project_id = await resolve_project_id(project_id=args.project_id)
    room = _normalize_non_empty(args.room)
    if room is None:
        raise SystemExit("MESHAGENT_ROOM was not set.")

    domain = await _resolve_events_domain(
        room=room,
        explicit_domain=args.events_domain,
        url=args.events_url,
        base_domain=args.events_base_domain,
    )
    if domain is None:
        raise SystemExit(
            "Missing Slack Events API route domain. Set MESHAGENT_SLACK_EVENTS_DOMAIN, "
            "set MESHAGENT_SLACK_EVENTS_URL, or configure a MeshAgent pages domain."
        )

    events_url = _normalize_non_empty(args.events_url) or f"https://{domain}/"
    service_name = (
        _normalize_non_empty(args.events_service_name) or DEFAULT_EVENTS_SERVICE_NAME
    )
    target = f"{service_name}:{args.port}"

    client = await get_client()
    try:
        signing_secret = _normalize_non_empty(args.signing_secret)
        signing_secret_id = _normalize_non_empty(args.signing_secret_id)
        service_account_email = None
        if signing_secret is None:
            service_account_id, service_account_email = await _resolve_service_account(
                client=client,
                project_id=project_id,
                name=args.service_account_name,
            )
            await _grant_service_account_runtime_access(
                client=client,
                project_id=project_id,
                service_account_id=service_account_id,
            )
            if signing_secret_id is None:
                signing_secret_id = await _find_service_account_secret_id(
                    client=client,
                    project_id=project_id,
                    service_account_id=service_account_id,
                    name="slack-signing-secret",
                )

        if signing_secret is None and signing_secret_id is None:
            raise SystemExit(
                "Missing Slack signing secret. Set SLACK_SIGNING_SECRET for local "
                "dev, or set MESHAGENT_SLACK_SIGNING_SECRET_ID for an existing "
                "service-account secret."
            )

        service_spec = _events_service_spec(
            service_name=service_name,
            room=room,
            port=args.port,
            signing_secret=signing_secret,
            service_account_email=service_account_email,
            signing_secret_id=signing_secret_id,
        )
        await _upsert_room_service(
            client=client,
            project_id=project_id,
            room=room,
            spec=service_spec,
        )
        await _upsert_route(
            client=client,
            project_id=project_id,
            domain=domain,
            room=room,
            target=target,
        )
    finally:
        await client.close()

    print("Slack Events API Request URL:")
    print(f"  {events_url}")
    print("")
    print(
        "Paste this URL into the @meshagent Slack app Event Subscriptions "
        "Request URL. Slack will verify it with a signed url_verification request."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure the Slack Events API route for local dev."
    )
    parser.add_argument("--project-id", default=os.getenv("MESHAGENT_PROJECT_ID"))
    parser.add_argument("--room", default=os.getenv("MESHAGENT_ROOM"))
    parser.add_argument(
        "--events-service-name",
        default=os.getenv(
            "MESHAGENT_SLACK_EVENTS_SERVICE_NAME", DEFAULT_EVENTS_SERVICE_NAME
        ),
    )
    parser.add_argument(
        "--service-account-name",
        default=os.getenv("MESHAGENT_SLACK_SERVICE_ACCOUNT_NAME", DEFAULT_AGENT_NAME),
    )
    parser.add_argument("--signing-secret", default=os.getenv("SLACK_SIGNING_SECRET"))
    parser.add_argument(
        "--signing-secret-id",
        default=os.getenv("MESHAGENT_SLACK_SIGNING_SECRET_ID"),
    )
    parser.add_argument(
        "--events-domain",
        default=os.getenv("MESHAGENT_SLACK_EVENTS_DOMAIN"),
    )
    parser.add_argument(
        "--events-base-domain",
        default=os.getenv(
            "MESHAGENT_SLACK_EVENTS_BASE_DOMAIN", DEFAULT_EVENTS_BASE_DOMAIN
        )
        or DEFAULT_EVENTS_BASE_DOMAIN,
    )
    parser.add_argument("--events-url", default=os.getenv("MESHAGENT_SLACK_EVENTS_URL"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MESHAGENT_SLACK_EVENTS_PORT", str(DEFAULT_EVENTS_PORT))),
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
