import json

import pytest
from aiohttp import web

from meshagent.api.managed_agents import (
    ManagedAgentHttpConnection,
    ManagedAgentPythonTool,
)
from meshagent.cli.managed_python import run_embedded_tool


def _tool(source: str) -> ManagedAgentPythonTool:
    return ManagedAgentPythonTool.model_validate(
        {
            "type": "python",
            "name": "lookup",
            "source": source,
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        }
    )


@pytest.mark.asyncio
async def test_embedded_tool_environment_uses_explicit_values_only(monkeypatch):
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-leak")
    tool = _tool(
        "import os\n"
        "async def lookup(args):\n"
        "    return {'key': os.getenv('API_KEY'), 'mapping': os.environ['API_KEY'], "
        "'host': os.getenv('HOST_ONLY_SECRET')}"
    )

    result = await run_embedded_tool(
        tool, {}, {}, {"API_KEY": "configured-value"}, None, False
    )

    assert result["result"] == {
        "key": "configured-value",
        "mapping": "configured-value",
        "host": None,
    }


@pytest.mark.asyncio
async def test_embedded_tool_secret_header_stays_in_authenticated_proxy():
    async def proxy(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer current-login-token"
        assert request.query["url"] == "https://api.example.com/customers"
        secret_headers = json.loads(request.query["secret-headers"])
        assert secret_headers == {"x-api-key": {"secret_id": "crm-key", "prefix": ""}}
        assert "crm-key" not in json.dumps(dict(request.headers))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/proxy-request", proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        tool = _tool(
            "async def lookup(args):\n"
            "    response = await http.request(connection='crm', path='/customers')\n"
            "    return response.json()"
        )
        connection = ManagedAgentHttpConnection.model_validate(
            {
                "base_url": "https://api.example.com",
                "headers": [{"name": "X-API-Key", "secret": {"id": "crm-key"}}],
            }
        )
        result = await run_embedded_tool(
            tool,
            {"crm": connection},
            {},
            {},
            {
                "base_url": f"http://127.0.0.1:{port}",
                "token": "current-login-token",
            },
            True,
        )
        assert result["result"] == {"ok": True}
        assert result["requests"] == [
            {"method": "GET", "connection": "crm", "status": 200}
        ]
    finally:
        await runner.cleanup()
