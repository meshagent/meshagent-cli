"""Run a managed-agent Python tool with the Monty package bundled by the CLI."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import jsonschema
from aiohttp import ClientTimeout
from aiohttp.abc import AbstractResolver
from pydantic_monty import NOT_HANDLED, AsyncMonty

from meshagent.api.http import new_client_session, new_tcp_connector
from meshagent.api.managed_agents import (
    ManagedAgentHttpConnection,
    ManagedAgentPythonTool,
)

_MAX_BYTES = 1024 * 1024
_FORBIDDEN_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "proxy-authorization",
    "proxy-authenticate",
    "proxy-connection",
    "keep-alive",
    "upgrade",
    "te",
    "trailer",
    "cookie",
    "set-cookie",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}
_PRELUDE = """
import json
class _HttpResponse:
    def __init__(self, response):
        self.status = response['status']
        self.status_code = self.status
        self.headers = response['headers']
        self.text = response['text']
    def json(self):
        return json.loads(self.text)
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError('HTTP request failed with status ' + str(self.status))
class _Http:
    async def request(self, url=None, *, connection=None, path=None, method='GET', headers=None, params=None, json=None, body=None):
        import json as codec
        response = codec.loads(await _meshagent_http(codec.dumps({'url': url, 'connection': connection, 'path': path, 'method': method, 'headers': headers or {}, 'params': params or {}, 'json': json, 'body': body})))
        if 'error' in response:
            raise RuntimeError(response['error'])
        return _HttpResponse(response)
http = _Http()
"""


class _PinnedResolver(AbstractResolver):
    def __init__(self, host: str, addresses: list[tuple[int, str]]) -> None:
        self.host = host
        self.addresses = addresses

    async def resolve(
        self, host: str, port: int = 0, family: int = 0
    ) -> list[dict[str, Any]]:
        if host != self.host:
            raise OSError("unexpected HTTP destination")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for address_family, address in self.addresses
        ]

    async def close(self) -> None:
        pass


def _checked_url(value: str) -> tuple[str, Any]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("URL must be HTTP(S), without credentials or fragment")
    return value, parsed


def _origin(parsed: Any) -> tuple[str, str, int]:
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _header_name(name: str) -> str:
    if (
        re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None
        or name.lower() in _FORBIDDEN_HEADERS
    ):
        raise ValueError("invalid HTTP header name")
    return name.lower()


def _header_value(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("invalid HTTP header value")
    return value


async def _public_addresses(host: str, port: int) -> list[tuple[int, str]]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as error:
        raise ValueError("could not resolve HTTP destination") from error
    addresses = [(family, record[0]) for family, _, _, _, record in records]
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for _, address in addresses
    ):
        raise ValueError("HTTP destination must resolve to public addresses")
    return addresses


async def _http_request(
    request: Mapping[str, Any],
    connections: Mapping[str, ManagedAgentHttpConnection],
    proxy: Mapping[str, str] | None,
    allow_private_http: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    connection_name = request.get("connection")
    raw_url = request.get("url")
    path = request.get("path")
    if connection_name is not None and raw_url is None:
        connection = connections.get(connection_name)
        if connection is None:
            raise ValueError("unknown HTTP connection")
        base, base_parts = _checked_url(connection.base_url)
        target = urljoin(base, path or "")
        target, parsed = _checked_url(target)
        if _origin(parsed) != _origin(base_parts):
            raise ValueError("connection path must stay on its configured origin")
    elif isinstance(raw_url, str) and connection_name is None and path is None:
        connection = None
        target, parsed = _checked_url(raw_url)
    else:
        raise ValueError("provide either url or connection (with optional path)")
    params = request.get("params") or {}
    if not isinstance(params, dict) or any(
        not isinstance(value, (str, int, float, bool)) for value in params.values()
    ):
        raise ValueError("query parameters must be strings, numbers, or booleans")
    if params:
        query = parsed.query + ("&" if parsed.query else "") + urlencode(params)
        target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    method = request.get("method", "GET")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError("unsupported HTTP method")
    headers: dict[str, str] = {}
    secret_headers: dict[str, dict[str, str]] = {}
    if connection is not None:
        if connection.use_proxy_secret is not None:
            secret_headers["authorization"] = {
                "secret_id": connection.use_proxy_secret,
                "prefix": "Bearer ",
            }
        for entry in connection.headers:
            name = _header_name(entry.name)
            if entry.secret is not None:
                secret_headers[name] = {
                    "secret_id": entry.secret.id,
                    "prefix": entry.prefix,
                }
            else:
                assert entry.value is not None
                headers[name] = _header_value(entry.value)
    has_secrets = bool(secret_headers)
    supplied_headers = request.get("headers") or {}
    if not isinstance(supplied_headers, dict):
        raise ValueError("HTTP headers must be an object")
    for raw_name, raw_value in supplied_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise ValueError("HTTP headers must be strings")
        name = _header_name(raw_name)
        if name in secret_headers or (has_secrets and name == "authorization"):
            raise ValueError(f"header {name} is controlled by the HTTP host")
        headers[name] = _header_value(raw_value)
    addresses = None
    if not allow_private_http:
        addresses = await _public_addresses(parsed.hostname, _origin(parsed)[2])
    if has_secrets:
        if proxy is None:
            raise ValueError(
                "secret-backed HTTP requires authenticated proxy credentials"
            )
        base_url, _ = _checked_url(proxy["base_url"])
        query = {"url": target}
        if (
            connection is not None
            and connection.use_proxy_secret is not None
            and len(secret_headers) == 1
        ):
            query["secret-id"] = connection.use_proxy_secret
        else:
            query["secret-headers"] = json.dumps(secret_headers, separators=(",", ":"))
        target = base_url.rstrip("/") + "/proxy-request?" + urlencode(query)
        headers["authorization"] = "Bearer " + proxy["token"]
    body = request.get("body")
    json_body = request.get("json")
    if body is not None and not isinstance(body, str):
        raise ValueError("HTTP body must be a string")
    if body is not None and json_body is not None:
        raise ValueError("provide either json or body")
    connector = (
        new_tcp_connector(resolver=_PinnedResolver(parsed.hostname, addresses))
        if addresses and not has_secrets
        else None
    )
    options = {"connector": connector} if connector is not None else {}
    async with new_client_session(
        timeout=ClientTimeout(total=15), trust_env=False, **options
    ) as client:
        try:
            async with client.request(
                method,
                target,
                headers=headers,
                data=body,
                json=json_body,
                allow_redirects=False,
            ) as response:
                content = await response.content.read(_MAX_BYTES + 1)
                if len(content) > _MAX_BYTES:
                    raise ValueError("HTTP response exceeds 1 MiB")
                value = {
                    "status": response.status,
                    "headers": {
                        name.lower(): value
                        for name, value in response.headers.items()
                        if name.lower() != "set-cookie"
                    },
                    "text": content.decode("utf-8", errors="replace"),
                }
                return value, {
                    "method": method,
                    "connection": connection_name,
                    "status": response.status,
                }
        except asyncio.TimeoutError as error:
            raise ValueError("HTTP request timed out") from error
        except OSError as error:
            raise ValueError("HTTP request failed") from error


async def run_embedded_tool(
    tool: ManagedAgentPythonTool,
    connections: Mapping[str, ManagedAgentHttpConnection],
    arguments: dict[str, Any],
    environment: Mapping[str, str],
    proxy: Mapping[str, str] | None,
    allow_private_http: bool,
) -> dict[str, Any]:
    """Execute the current source without an external runner installation."""
    jsonschema.validate(arguments, tool.input_schema)
    traces: list[dict[str, Any]] = []
    logs: list[str] = []
    calls = 0

    async def host_http(encoded: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 32:
            return json.dumps({"error": "tool exceeded 32 HTTP requests"})
        if len(encoded) > _MAX_BYTES:
            return json.dumps({"error": "HTTP request exceeds 1 MiB"})
        try:
            request = json.loads(encoded)
            if not isinstance(request, dict):
                raise ValueError("invalid HTTP request")
            value, trace = await _http_request(
                request, connections, proxy, allow_private_http
            )
            traces.append(trace)
            return json.dumps(value)
        except (ValueError, KeyError, TypeError) as error:
            return json.dumps({"error": str(error)})

    def host_os(
        *, name: str, args: tuple[Any, ...], kwargs: dict[str, Any], **_: Any
    ) -> Any:
        if name == "os.getenv":
            key = args[0] if args else kwargs["key"]
            default = args[1] if len(args) > 1 else kwargs.get("default")
            return environment.get(key, default)
        if name == "os.environ":
            return dict(environment)
        return NOT_HANDLED

    def on_print(_stream: str, text: str) -> None:
        if sum(len(item) for item in logs) < 16384:
            logs.append(text[: 16384 - sum(len(item) for item in logs)])

    async def execute() -> Any:
        bundle_root = vars(sys).get("_MEIPASS")
        binary_path = (
            Path(bundle_root)
            / "meshagent"
            / "cli"
            / "bin"
            / ("monty.exe" if os.name == "nt" else "monty")
            if bundle_root is not None
            else None
        )
        async with AsyncMonty(
            binary_path=binary_path,
            min_processes=0,
            max_processes=1,
            request_timeout=30,
        ) as pool:
            async with pool.checkout(
                script_name=f"{tool.name}.py",
                limits={
                    "max_feed_duration_secs": 30,
                    "max_turn_duration_secs": 10,
                    "max_memory": 64 * 1024 * 1024,
                    "max_suspensions": 128,
                    "max_total_sleep_secs": 0,
                },
            ) as session:
                await session.feed_run(_PRELUDE)
                program = (
                    f"{tool.source}\n\nimport json as _meshagent_json\n"
                    f"_meshagent_json.dumps(await {tool.entrypoint or tool.name}(_meshagent_json.loads(_meshagent_input)))"
                )
                return await session.feed_run(
                    program,
                    inputs={"_meshagent_input": json.dumps(arguments)},
                    external_lookup={"_meshagent_http": host_http},
                    os=host_os,
                    print_callback=on_print,
                )

    result = await asyncio.wait_for(execute(), timeout=30)
    if not isinstance(result, str) or len(result) > _MAX_BYTES:
        raise ValueError("tool result is invalid or exceeds 1 MiB")
    return {"result": json.loads(result), "logs": "".join(logs), "requests": traces}
