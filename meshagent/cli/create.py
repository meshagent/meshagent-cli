from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
import textwrap
from typing import Literal, Mapping, Sequence

import click

from meshagent.cli.meshagent_images import (
    MESHAGENT_IMAGE_PREFIX_TEMPLATE,
    render_meshagent_image_prefix_template,
)
from meshagent.cli.version import __version__


SOURCE_SUFFIXES = {
    ".cs",
    ".dart",
    ".go",
    ".js",
    ".jsx",
    ".py",
    ".rb",
    ".ts",
    ".tsx",
}
PROJECT_MARKER_NAMES = {
    "Containerfile",
    "Dockerfile",
    "Gemfile",
    "go.mod",
    "meshagent.yaml",
    "meshagent.yml",
    "package.json",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "bin",
    "build",
    "dist",
    "node_modules",
    "obj",
    "target",
    "venv",
}
IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".gitkeep",
}

WEB_FOCUS = "webserver"
AGENT_FOCUS = "backend-agent"
DEFAULT_LANGUAGE = "python"
DEFAULT_FOCUS = AGENT_FOCUS


@dataclass(frozen=True, slots=True)
class InitTemplate:
    language_id: str
    focus_id: str
    label: str
    description: str
    files: Mapping[str, str]
    next_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitLanguage:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class InitFocus:
    id: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class ExistingProjectSelection:
    action: Literal["run-doctor", "create-subfolder"]
    subfolder_name: str | None = None


PYTHON_WEBSERVER = """\
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from aiohttp import web
from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url
from meshagent.tools import FunctionTool, Toolkit, ToolContext
from meshagent.tools.hosting import _start_hosted_toolkit


CONTENT_PATH = Path(__file__).with_name("dev-content.json")
TOOLKIT_NAME = "meshagent.init.python-content"


def default_content() -> dict:
    return {
        "activeId": "hero",
        "items": {
            "hero": {
                "id": "hero",
                "headline": "hello from meshagent create",
                "body": "Run ./scripts/dev.sh to let the local MeshAgent toolkit update this content.",
            }
        },
    }


async def read_content() -> dict:
    try:
        async with aiofiles.open(CONTENT_PATH, encoding="utf-8") as handle:
            return json.loads(await handle.read())
    except Exception:
        return default_content()


async def write_content(content: dict) -> dict:
    await aiofiles.os.makedirs(CONTENT_PATH.parent, exist_ok=True)
    async with aiofiles.open(CONTENT_PATH, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(content, indent=2) + "\\n")
    return content


class CreateContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="create",
            title="Create local web content",
            description="Creates a local Python content record rendered by the dev app.",
            input_schema={
                "type": "object",
                "required": ["id", "headline", "body"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        )

    async def execute(
        self, context: ToolContext, *, id: str, headline: str, body: str
    ) -> dict:
        content = await read_content()
        content.setdefault("items", {})[id] = {
            "id": id,
            "headline": headline,
            "body": body,
        }
        content["activeId"] = id
        await write_content(content)
        return {"ok": True, "item": content["items"][id]}


class UpdateContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="update",
            title="Update local web content",
            description="Updates a local Python content record rendered by the dev app.",
            input_schema={
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "headline": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
        )

    async def execute(
        self,
        context: ToolContext,
        *,
        id: str,
        headline: str | None = None,
        body: str | None = None,
    ) -> dict:
        content = await read_content()
        items = content.setdefault("items", {})
        existing = items.get(id, {"id": id, "headline": "", "body": ""})
        if headline is not None:
            existing["headline"] = headline
        if body is not None:
            existing["body"] = body
        items[id] = existing
        content["activeId"] = id
        await write_content(content)
        return {"ok": True, "item": existing}


class SearchContentTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="search",
            title="Search local web content",
            description="Searches content records currently available to the local Python app.",
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {"query": {"type": "string"}},
            },
        )

    async def execute(self, context: ToolContext, *, query: str) -> dict:
        content = await read_content()
        normalized_query = str(query or "").lower()
        results = [
            item
            for item in content.get("items", {}).values()
            if normalized_query in f"{item.get('headline', '')}\\n{item.get('body', '')}".lower()
        ]
        return {"ok": True, "results": results}


class PythonContentToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(
            name=TOOLKIT_NAME,
            title="Python Local Content Toolkit",
            description="Local-only create, update, and search tools for Python dev app content.",
            tools=[CreateContentTool(), UpdateContentTool(), SearchContentTool()],
        )


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\\n", content_type="text/plain")


async def index(request: web.Request) -> web.Response:
    content = await read_content()
    active_item = content.get("items", {}).get(content.get("activeId")) or content.get(
        "items", {}
    ).get("hero", {})
    return web.Response(
        text=f"{active_item.get('headline', 'hello from meshagent create')}\\n{active_item.get('body', '')}\\n",
        content_type="text/plain",
    )


async def status(request: web.Request) -> web.Response:
    return web.Response(text="ready\\n", content_type="text/plain")


async def ping(request: web.Request) -> web.Response:
    return web.json_response({"pong": True})


async def run_dev_content_toolkit() -> None:
    probe = os.environ.get("MESHAGENT_INIT_DEV_PROBE")
    room_name = os.environ.get("MESHAGENT_ROOM")
    token = os.environ.get("MESHAGENT_TOKEN")
    if not probe or not room_name or not token:
        return

    protocol = WebSocketClientProtocol(
        url=websocket_room_url(room_name=room_name),
        token=token,
    )
    async with RoomClient(protocol_factory=protocol.create_factory()) as room:
        hosted_toolkit = await _start_hosted_toolkit(
            room=room,
            toolkit=PythonContentToolkit(),
        )
        try:
            proof_id = "meshagent-init-proof"
            created = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="create",
                input={
                    "id": proof_id,
                    "headline": "Local dev content created through MeshAgent",
                    "body": "This text was created by the local Python content toolkit.",
                },
            )
            print(f"MeshAgent create dev toolkit create: {json.dumps(created.json)}")

            headline = f"MeshAgent local dev proof {probe}"
            updated = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="update",
                input={
                    "id": proof_id,
                    "headline": headline,
                    "body": "The room invoked the local Python toolkit, and the toolkit updated dev-content.json.",
                },
            )
            print(f"MeshAgent create dev toolkit update: {json.dumps(updated.json)}")

            searched = await room.agents.invoke_tool(
                toolkit=TOOLKIT_NAME,
                tool="search",
                input={"query": probe},
            )
            print(f"MeshAgent create dev toolkit search: {json.dumps(searched.json)}")

            content = await read_content()
            active_item = content.get("items", {}).get(content.get("activeId"))
            search_results = searched.json.get("results", [])
            if active_item.get("headline") != headline or not any(
                item.get("headline") == headline for item in search_results
            ):
                raise RuntimeError("Local Python content toolkit proof failed.")

            print(f"MeshAgent create dev toolkit proof wrote: dev-content.json {probe}")
            hold_seconds = float(
                os.environ.get("MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS") or "0"
            )
            if hold_seconds > 0:
                print(
                    f"MeshAgent create dev toolkit holding registration for {hold_seconds}s"
                )
                await asyncio.sleep(hold_seconds)
        finally:
            await hosted_toolkit.stop()


async def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_get("/api/ping", ping)
    app.router.add_get("/", index)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Serving on 0.0.0.0:{port}")
    asyncio.create_task(run_dev_content_toolkit())

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
"""

PYTHON_AGENT = """\
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiofiles
import aiofiles.os
from meshagent.api import RoomClient, WebSocketClientProtocol, websocket_room_url
from meshagent.tools import FunctionTool, Toolkit, ToolContext
from meshagent.tools.hosting import _start_hosted_toolkit


PROOF_PATH = Path(__file__).with_name("agent-proof.json")
TOOLKIT_NAME = "meshagent.init.python-agent"


class PingTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="ping",
            title="Ping local agent",
            description="Checks that the local Python backend agent toolkit is reachable.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext) -> dict:
        return {"pong": True}


class StatusTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="status",
            title="Read local agent status",
            description="Returns a minimal status payload from the local Python backend agent.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )

    async def execute(self, context: ToolContext) -> dict:
        return {"ready": True, "language": "python", "focus": "backend-agent"}


class EchoTool(FunctionTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo",
            title="Echo a message",
            description="Echoes a message through the local Python backend agent.",
            input_schema={
                "type": "object",
                "required": ["message"],
                "additionalProperties": False,
                "properties": {"message": {"type": "string"}},
            },
        )

    async def execute(self, context: ToolContext, *, message: str) -> dict:
        return {"echo": message}


class PythonAgentToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(
            name=TOOLKIT_NAME,
            title="Python Local Agent Toolkit",
            description="Local-only ping, status, and echo tools for the Python backend agent.",
            tools=[PingTool(), StatusTool(), EchoTool()],
        )


async def write_agent_proof(probe: str, echo: str) -> None:
    payload = {
        "probe": probe,
        "echo": echo,
        "tools": ["ping", "status", "echo"],
    }
    await aiofiles.os.makedirs(PROOF_PATH.parent, exist_ok=True)
    async with aiofiles.open(PROOF_PATH, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(payload, indent=2) + "\\n")


async def main() -> None:
    room_name = os.environ.get("MESHAGENT_ROOM")
    token = os.environ.get("MESHAGENT_TOKEN")
    if not room_name or not token:
        print("MeshAgent room environment is not set; waiting for deployment env.")
        await asyncio.Event().wait()
        return

    protocol = WebSocketClientProtocol(
        url=websocket_room_url(room_name=room_name),
        token=token,
    )
    async with RoomClient(protocol_factory=protocol.create_factory()) as room:
        print(f"Connected to MeshAgent room: {room.room_name}")
        await run_agent_toolkit_proof(room)


async def run_agent_toolkit_proof(room: RoomClient) -> None:
    probe = os.environ.get("MESHAGENT_INIT_DEV_PROBE")
    hosted_toolkit = await _start_hosted_toolkit(
        room=room,
        toolkit=PythonAgentToolkit(),
    )
    try:
        if not probe:
            await room.wait_for_close()
            return

        pinged = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="ping",
            input={},
        )
        print(f"MeshAgent create dev toolkit ping: {json.dumps(pinged.json)}")

        status = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="status",
            input={},
        )
        print(f"MeshAgent create dev toolkit status: {json.dumps(status.json)}")

        message = f"MeshAgent local dev proof {probe}"
        echoed = await room.agents.invoke_tool(
            toolkit=TOOLKIT_NAME,
            tool="echo",
            input={"message": message},
        )
        print(f"MeshAgent create dev toolkit echo: {json.dumps(echoed.json)}")
        if echoed.json.get("echo") != message:
            raise RuntimeError("Local Python agent toolkit proof failed.")

        await write_agent_proof(probe, message)
        print(f"MeshAgent create dev toolkit proof wrote: agent-proof.json {probe}")
        hold_seconds = float(
            os.environ.get("MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS") or "0"
        )
        if hold_seconds > 0:
            print(f"MeshAgent create dev toolkit holding registration for {hold_seconds}s")
            await asyncio.sleep(hold_seconds)
    finally:
        await hosted_toolkit.stop()


if __name__ == "__main__":
    asyncio.run(main())
"""

PYTHON_WEBSERVER_PYPROJECT = f"""\
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "meshagent-init-python-webserver"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "aiofiles~=24.1",
  "aiohttp[speedups]~=3.13.0",
  "meshagent-api=={__version__}",
  "meshagent-tools=={__version__}",
  "openai~=2.25.0",
]

[tool.setuptools]
py-modules = ["server"]
"""

PYTHON_AGENT_PYPROJECT = f"""\
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "meshagent-init-python-agent"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "aiofiles~=24.1",
  "meshagent-api=={__version__}",
  "meshagent-tools=={__version__}",
  "openai~=2.25.0",
]

[tool.setuptools]
py-modules = ["server"]
"""

PYTHON_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}python-sdk-slim:{__version__} AS build
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir --target /out .

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
EXPOSE 8000
CMD ["-m", "server"]
"""

PYTHON_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}python-sdk-slim:{__version__} AS build
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir --target /out .

FROM scratch
LABEL meshagent.runtime=python
WORKDIR /app
COPY --from=build /out /app
CMD ["-m", "server"]
"""

PYTHON_DOCKERIGNORE = """\
__pycache__/
*.pyc
.pytest_cache/
.venv/
venv/
.git/
.DS_Store
"""

PYTHON_INSTALL_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-python3.13}"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  "$PYTHON" -m venv "$VENV"
  "$VENV_PYTHON" -m pip install --upgrade pip
fi
PIP_ONLY_BINARY="${PIP_ONLY_BINARY:-:all:}" "$VENV_PYTHON" -m pip install -e .
"""

PYTHON_DEV_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-.venv}"
VENV_PYTHON="$VENV/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "Missing $VENV_PYTHON. Run the install script first." >&2
  exit 1
fi
meshagent room connect -- "$VENV_PYTHON" -u server.py
"""

PYTHON_WEBSERVER_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-python-webserver:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait
"""

PYTHON_AGENT_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-python-agent:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait
"""

PYTHON_DEV_CONTENT_JSON = """\
{
  "activeId": "hero",
  "items": {
    "hero": {
      "id": "hero",
      "headline": "hello from meshagent create",
      "body": "Run ./scripts/dev.sh to let the local MeshAgent toolkit update this content."
    }
  }
}
"""

NODE_DEV_CONTENT_JSON = """\
{
  "activeId": "hero",
  "items": {
    "hero": {
      "id": "hero",
      "headline": "hello from meshagent create",
      "body": "Run npm run dev to let the local MeshAgent toolkit update this content."
    }
  }
}
"""

NODE_DEV_CONTENT_TOOLKIT = """\
const fs = require("node:fs");
const path = require("node:path");
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const contentDisplayPath = "__CONTENT_PATH__";
const contentPath = path.join(__dirname, contentDisplayPath.split("/").pop());
const toolkitName = "__TOOLKIT_NAME__";

function defaultContent() {
  return {
    activeId: "hero",
    items: {
      hero: {
        id: "hero",
        headline: "hello from meshagent create",
        body: "Run npm run dev to let the local MeshAgent toolkit update this content.",
      },
    },
  };
}

async function readContent() {
  try {
    return JSON.parse(await fs.promises.readFile(contentPath, "utf8"));
  } catch {
    return defaultContent();
  }
}

async function writeContent(content) {
  await fs.promises.mkdir(path.dirname(contentPath), { recursive: true });
  await fs.promises.writeFile(contentPath, `${JSON.stringify(content, null, 2)}\\n`, "utf8");
  return content;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

class __CLASS_PREFIX__CreateContentTool extends Tool {
  constructor() {
    super({
      name: "create",
      title: "Create local web content",
      description: "Creates a local __LANGUAGE_LABEL__ content record rendered by the dev app.",
      inputSchema: {
        type: "object",
        required: ["id", "headline", "body"],
        additionalProperties: false,
        properties: {
          id: { type: "string" },
          headline: { type: "string" },
          body: { type: "string" },
        },
      },
    });
  }

  async execute({ id, headline, body }) {
    const content = await readContent();
    content.items = content.items ?? {};
    content.items[id] = {
      id,
      headline,
      body,
      updatedAt: new Date().toISOString(),
    };
    content.activeId = id;
    await writeContent(content);
    return new JsonContent({ json: { ok: true, item: content.items[id] } });
  }
}

class __CLASS_PREFIX__UpdateContentTool extends Tool {
  constructor() {
    super({
      name: "update",
      title: "Update local web content",
      description: "Updates a local __LANGUAGE_LABEL__ content record rendered by the dev app.",
      inputSchema: {
        type: "object",
        required: ["id"],
        additionalProperties: false,
        properties: {
          id: { type: "string" },
          headline: { type: "string" },
          body: { type: "string" },
        },
      },
    });
  }

  async execute({ id, headline, body }) {
    const content = await readContent();
    content.items = content.items ?? {};
    const existing = content.items[id] ?? { id, headline: "", body: "" };
    content.items[id] = {
      ...existing,
      ...(headline === undefined ? {} : { headline }),
      ...(body === undefined ? {} : { body }),
      updatedAt: new Date().toISOString(),
    };
    content.activeId = id;
    await writeContent(content);
    return new JsonContent({ json: { ok: true, item: content.items[id] } });
  }
}

class __CLASS_PREFIX__SearchContentTool extends Tool {
  constructor() {
    super({
      name: "search",
      title: "Search local web content",
      description: "Searches content records currently available to the local __LANGUAGE_LABEL__ app.",
      inputSchema: {
        type: "object",
        required: ["query"],
        additionalProperties: false,
        properties: {
          query: { type: "string" },
        },
      },
    });
  }

  async execute({ query }) {
    const content = await readContent();
    const normalizedQuery = String(query ?? "").toLowerCase();
    const results = Object.values(content.items ?? {}).filter((item) => (
      `${item.headline ?? ""}\\n${item.body ?? ""}`.toLowerCase().includes(normalizedQuery)
    ));
    return new JsonContent({ json: { ok: true, results } });
  }
}

class __CLASS_PREFIX__ContentToolkit extends Toolkit {
  constructor() {
    super({
      name: toolkitName,
      title: "__LANGUAGE_LABEL__ Local Content Toolkit",
      description: "Local-only create, update, and search tools for the __LANGUAGE_LABEL__ dev app content.",
      tools: [
        new __CLASS_PREFIX__CreateContentTool(),
        new __CLASS_PREFIX__UpdateContentTool(),
        new __CLASS_PREFIX__SearchContentTool(),
      ],
    });
  }
}

async function runDevContentToolkit(focus, existingRoom) {
  const probe = process.env.MESHAGENT_INIT_DEV_PROBE;
  if (!probe || !process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    return;
  }

  const room = existingRoom ?? new RoomClient();
  if (!existingRoom) {
    await room.start();
  }
  const hostedToolkit = await startHostedToolkit({
    room,
    toolkit: new __CLASS_PREFIX__ContentToolkit(),
    public_: true,
  });

  try {
    const proofId = "meshagent-init-proof";
    const created = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "create",
      arguments: {
        id: proofId,
        headline: "Local dev content created through MeshAgent",
        body: `This text was created by the local __LANGUAGE_LABEL__ content toolkit for ${focus}.`,
      },
    });
    console.log(`MeshAgent create dev toolkit create: ${JSON.stringify(created.json)}`);

    const headline = `MeshAgent local dev proof ${probe}`;
    const updated = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "update",
      arguments: {
        id: proofId,
        headline,
        body: `The room invoked the local __LANGUAGE_LABEL__ toolkit for ${focus}, and the toolkit updated ${contentDisplayPath}.`,
      },
    });
    console.log(`MeshAgent create dev toolkit update: ${JSON.stringify(updated.json)}`);

    const searched = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "search",
      arguments: { query: probe },
    });
    console.log(`MeshAgent create dev toolkit search: ${JSON.stringify(searched.json)}`);

    const content = await readContent();
    const activeItem = content.items?.[content.activeId];
    const searchResults = searched.json?.results ?? [];
    if (
      activeItem?.headline !== headline
      || !searchResults.some((item) => item?.headline === headline)
    ) {
      throw new Error("Local content toolkit proof did not update searchable web content.");
    }

    console.log(`MeshAgent create dev toolkit proof wrote: ${contentDisplayPath} ${probe}`);
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
    if (Number.isFinite(holdSeconds) && holdSeconds > 0) {
      console.log(`MeshAgent create dev toolkit holding registration for ${holdSeconds}s`);
      await sleep(holdSeconds * 1000);
    }
  } finally {
    await hostedToolkit.stop();
    if (!existingRoom) {
      room.dispose();
    }
  }
}
"""

NODE_AGENT_TOOLKIT = """\
const fs = require("node:fs");
const path = require("node:path");
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const proofDisplayPath = "__PROOF_PATH__";
const proofPath = path.join(__dirname, proofDisplayPath.split("/").pop());
const toolkitName = "__TOOLKIT_NAME__";

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function writeAgentProof({ probe, echo }) {
  const payload = {
    probe,
    echo,
    tools: ["ping", "status", "echo"],
  };
  await fs.promises.mkdir(path.dirname(proofPath), { recursive: true });
  await fs.promises.writeFile(proofPath, `${JSON.stringify(payload, null, 2)}\\n`, "utf8");
  return payload;
}

class __CLASS_PREFIX__PingTool extends Tool {
  constructor() {
    super({
      name: "ping",
      title: "Ping local agent",
      description: "Checks that the local __LANGUAGE_LABEL__ backend agent toolkit is reachable.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
    });
  }

  async execute() {
    return new JsonContent({ json: { pong: true } });
  }
}

class __CLASS_PREFIX__StatusTool extends Tool {
  constructor() {
    super({
      name: "status",
      title: "Read local agent status",
      description: "Returns a minimal status payload from the local __LANGUAGE_LABEL__ backend agent.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
    });
  }

  async execute() {
    return new JsonContent({ json: { ready: true, language: "__LANGUAGE_ID__", focus: "backend-agent" } });
  }
}

class __CLASS_PREFIX__EchoTool extends Tool {
  constructor() {
    super({
      name: "echo",
      title: "Echo a message",
      description: "Echoes a message through the local __LANGUAGE_LABEL__ backend agent.",
      inputSchema: {
        type: "object",
        required: ["message"],
        additionalProperties: false,
        properties: {
          message: { type: "string" },
        },
      },
    });
  }

  async execute({ message }) {
    return new JsonContent({ json: { echo: message } });
  }
}

class __CLASS_PREFIX__AgentToolkit extends Toolkit {
  constructor() {
    super({
      name: toolkitName,
      title: "__LANGUAGE_LABEL__ Local Agent Toolkit",
      description: "Local-only ping, status, and echo tools for the __LANGUAGE_LABEL__ backend agent.",
      tools: [
        new __CLASS_PREFIX__PingTool(),
        new __CLASS_PREFIX__StatusTool(),
        new __CLASS_PREFIX__EchoTool(),
      ],
    });
  }
}

async function runDevAgentToolkit(existingRoom) {
  const probe = process.env.MESHAGENT_INIT_DEV_PROBE;
  if (!process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    return;
  }

  const room = existingRoom ?? new RoomClient();
  if (!existingRoom) {
    await room.start();
  }
  const hostedToolkit = await startHostedToolkit({
    room,
    toolkit: new __CLASS_PREFIX__AgentToolkit(),
    public_: true,
  });

  try {
    if (!probe) {
      await new Promise(() => {});
      return;
    }

    const pinged = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "ping",
      arguments: {},
    });
    console.log(`MeshAgent create dev toolkit ping: ${JSON.stringify(pinged.json)}`);

    const status = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "status",
      arguments: {},
    });
    console.log(`MeshAgent create dev toolkit status: ${JSON.stringify(status.json)}`);

    const message = `MeshAgent local dev proof ${probe}`;
    const echoed = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "echo",
      arguments: { message },
    });
    console.log(`MeshAgent create dev toolkit echo: ${JSON.stringify(echoed.json)}`);
    if (echoed.json?.echo !== message) {
      throw new Error("Local __LANGUAGE_LABEL__ agent toolkit proof did not echo the probe.");
    }

    await writeAgentProof({ probe, echo: message });
    console.log(`MeshAgent create dev toolkit proof wrote: ${proofDisplayPath} ${probe}`);
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
    if (Number.isFinite(holdSeconds) && holdSeconds > 0) {
      console.log(`MeshAgent create dev toolkit holding registration for ${holdSeconds}s`);
      await sleep(holdSeconds * 1000);
    }
  } finally {
    await hostedToolkit.stop();
    if (!existingRoom) {
      room.dispose();
    }
  }
}
"""


def _node_dev_content_toolkit(
    *,
    language_label: str,
    class_prefix: str,
    toolkit_name: str,
    content_path: str,
) -> str:
    return (
        NODE_DEV_CONTENT_TOOLKIT.replace("__LANGUAGE_LABEL__", language_label)
        .replace("__CLASS_PREFIX__", class_prefix)
        .replace("__TOOLKIT_NAME__", toolkit_name)
        .replace("__CONTENT_PATH__", content_path)
    )


def _node_agent_toolkit(
    *,
    language_label: str,
    language_id: str,
    class_prefix: str,
    toolkit_name: str,
    proof_path: str,
) -> str:
    return (
        NODE_AGENT_TOOLKIT.replace("__LANGUAGE_LABEL__", language_label)
        .replace("__LANGUAGE_ID__", language_id)
        .replace("__CLASS_PREFIX__", class_prefix)
        .replace("__TOOLKIT_NAME__", toolkit_name)
        .replace("__PROOF_PATH__", proof_path)
    )


def _node_webserver_source(
    *,
    language_label: str,
    class_prefix: str,
    toolkit_name: str,
    content_path: str,
) -> str:
    return (
        'const http = require("node:http");\n'
        + _node_dev_content_toolkit(
            language_label=language_label,
            class_prefix=class_prefix,
            toolkit_name=toolkit_name,
            content_path=content_path,
        )
        + """

const port = Number(process.env.PORT || 3000);

const server = http.createServer(async (request, response) => {
  try {
    if (request.url === "/health") {
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end("ok\\n");
      return;
    }

    if (request.url === "/status") {
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end("ready\\n");
      return;
    }

    if (request.url === "/api/ping") {
      response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ pong: true }) + "\\n");
      return;
    }

    if (request.url === "/") {
      const content = await readContent();
      const activeItem = content.items?.[content.activeId] ?? content.items?.hero;
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end(`${activeItem?.headline ?? "hello from meshagent create"}\\n${activeItem?.body ?? ""}\\n`);
      return;
    }

    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("not found\\n");
  } catch (error) {
    console.error("Unable to handle request:", error);
    if (!response.headersSent) {
      response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
    }
    response.end("internal server error\\n");
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`Serving on 0.0.0.0:${port}`);
});

runDevContentToolkit("webserver").catch((error) => {
  console.error("Unable to run MeshAgent create dev content toolkit proof:", error);
  process.exitCode = 1;
});
"""
    )


def _node_agent_source(
    *,
    language_label: str,
    language_id: str,
    class_prefix: str,
    toolkit_name: str,
    proof_path: str,
) -> str:
    return (
        _node_agent_toolkit(
            language_label=language_label,
            language_id=language_id,
            class_prefix=class_prefix,
            toolkit_name=toolkit_name,
            proof_path=proof_path,
        )
        + """

async function main() {
  if (!process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    console.log("MeshAgent room environment is not set; waiting for deployment env.");
    await new Promise(() => {});
    return;
  }

  const room = new RoomClient();
  await room.start();
  console.log(`Connected to MeshAgent room: ${room.roomName}`);
  try {
    await runDevAgentToolkit(room);
  } finally {
    room.dispose();
  }
}

main().catch((error) => {
  console.error("Unable to start MeshAgent RoomClient:", error);
  process.exitCode = 1;
});
"""
    )


JAVASCRIPT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-javascript",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build server.js -o dist",
    "dev": "meshagent room connect -- node server.js",
    "deploy": "meshagent deploy . --tag meshagent-init-javascript:dev --public --liveness /health --wait",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@vercel/ncc": "^0.38.3"
  }}
}}
"""

JAVASCRIPT_AGENT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-javascript-agent",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build server.js -o dist",
    "dev": "meshagent room connect -- node server.js",
    "deploy": "meshagent deploy . --tag meshagent-init-javascript-agent:dev --meshagent-token agentDefault --wait",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@vercel/ncc": "^0.38.3"
  }}
}}
"""

JAVASCRIPT_WEBSERVER = _node_webserver_source(
    language_label="JavaScript",
    class_prefix="JavaScript",
    toolkit_name="meshagent.init.javascript-content",
    content_path="dev-content.json",
)

JAVASCRIPT_AGENT = _node_agent_source(
    language_label="JavaScript",
    language_id="javascript",
    class_prefix="JavaScript",
    toolkit_name="meshagent.init.javascript-agent",
    proof_path="agent-proof.json",
)

JAVASCRIPT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY server.js ./
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
EXPOSE 3000
CMD ["index.js"]
"""

JAVASCRIPT_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY server.js ./
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
CMD ["index.js"]
"""

JAVASCRIPT_DOCKERIGNORE = """\
node_modules/
dist/
.npm-cache/
npm-debug.log*
.git/
.DS_Store
"""

NPMRC = """\
cache=.npm-cache
fund=false
audit=false
"""

TYPESCRIPT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-typescript",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build src/server.ts -o dist",
    "dev": "meshagent room connect -- tsx src/server.ts",
    "deploy": "meshagent deploy . --tag meshagent-init-typescript:dev --public --liveness /health --wait",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@types/node": "^22.10.0",
    "tsx": "^4.20.0",
    "@vercel/ncc": "^0.38.3",
    "typescript": "^5.8.0"
  }}
}}
"""

TYPESCRIPT_AGENT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-typescript-agent",
  "version": "0.1.0",
  "private": true,
  "type": "commonjs",
  "scripts": {{
    "build": "ncc build src/server.ts -o dist",
    "dev": "meshagent room connect -- tsx src/server.ts",
    "deploy": "meshagent deploy . --tag meshagent-init-typescript-agent:dev --meshagent-token agentDefault --wait",
    "start": "node dist/index.js"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}"
  }},
  "devDependencies": {{
    "@types/node": "^22.10.0",
    "tsx": "^4.20.0",
    "@vercel/ncc": "^0.38.3",
    "typescript": "^5.8.0"
  }}
}}
"""

TYPESCRIPT_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "CommonJS",
    "moduleResolution": "Node",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "types": ["node"]
  },
  "include": ["src/**/*.ts"]
}
"""

TYPESCRIPT_WEBSERVER = "// @ts-nocheck\n" + _node_webserver_source(
    language_label="TypeScript",
    class_prefix="TypeScript",
    toolkit_name="meshagent.init.typescript-content",
    content_path="src/dev-content.json",
)

TYPESCRIPT_AGENT = "// @ts-nocheck\n" + _node_agent_source(
    language_label="TypeScript",
    language_id="typescript",
    class_prefix="TypeScript",
    toolkit_name="meshagent.init.typescript-agent",
    proof_path="src/agent-proof.json",
)

TYPESCRIPT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
EXPOSE 3000
CMD ["index.js"]
"""

TYPESCRIPT_AGENT_DOCKERFILE = f"""\
ARG MESHAGENT_IMAGE_PREFIX={MESHAGENT_IMAGE_PREFIX_TEMPLATE}
FROM ${{MESHAGENT_IMAGE_PREFIX}}node-sdk:{__version__} AS build
WORKDIR /app
COPY package*.json tsconfig.json ./
RUN npm install
COPY src ./src
RUN npm run build

FROM scratch
LABEL meshagent.runtime=node
WORKDIR /app
COPY --from=build /app/dist/index.js /app/index.js
CMD ["index.js"]
"""

REACT_PACKAGE_JSON = f"""\
{{
  "name": "meshagent-init-react",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {{
    "build": "vite build",
    "dev": "meshagent room connect -- sh -c 'node scripts/dev-content-toolkit.js & vite --host 0.0.0.0'",
    "deploy": "meshagent deploy . --tag meshagent-init-react:dev --public --liveness /health --room-mount /:/data:rw --wait"
  }},
  "dependencies": {{
    "@meshagent/meshagent": "^{__version__}",
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^6.0.0",
    "typescript": "^5.8.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  }},
  "devDependencies": {{}}
}}
"""

REACT_DEV_CONTENT_TOOLKIT_JS = """\
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const contentPath = path.join(projectRoot, "src", "dev-content.json");
const toolkitName = "meshagent.init.react-content";

async function readContent() {
  try {
    return JSON.parse(await readFile(contentPath, "utf8"));
  } catch {
    return {
      activeId: "hero",
      items: {
        hero: {
          id: "hero",
          headline: "hello from meshagent create",
          body: "Run npm run dev to let the local MeshAgent toolkit update this content.",
        },
      },
    };
  }
}

async function writeContent(content) {
  await mkdir(path.dirname(contentPath), { recursive: true });
  await writeFile(contentPath, `${JSON.stringify(content, null, 2)}\\n`, "utf8");
  return content;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

class CreateContentTool extends Tool {
  constructor() {
    super({
      name: "create",
      title: "Create local web content",
      description: "Creates a local React content record rendered by the dev server.",
      inputSchema: {
        type: "object",
        required: ["id", "headline", "body"],
        additionalProperties: false,
        properties: {
          id: { type: "string" },
          headline: { type: "string" },
          body: { type: "string" },
        },
      },
    });
  }

  async execute({ id, headline, body }) {
    const content = await readContent();
    content.items = content.items ?? {};
    content.items[id] = {
      id,
      headline,
      body,
      updatedAt: new Date().toISOString(),
    };
    content.activeId = id;
    await writeContent(content);
    return new JsonContent({ json: { ok: true, item: content.items[id] } });
  }
}

class UpdateContentTool extends Tool {
  constructor() {
    super({
      name: "update",
      title: "Update local web content",
      description: "Updates a local React content record rendered by the dev server.",
      inputSchema: {
        type: "object",
        required: ["id"],
        additionalProperties: false,
        properties: {
          id: { type: "string" },
          headline: { type: "string" },
          body: { type: "string" },
        },
      },
    });
  }

  async execute({ id, headline, body }) {
    const content = await readContent();
    content.items = content.items ?? {};
    const existing = content.items[id] ?? { id, headline: "", body: "" };
    content.items[id] = {
      ...existing,
      ...(headline === undefined ? {} : { headline }),
      ...(body === undefined ? {} : { body }),
      updatedAt: new Date().toISOString(),
    };
    content.activeId = id;
    await writeContent(content);
    return new JsonContent({ json: { ok: true, item: content.items[id] } });
  }
}

class SearchContentTool extends Tool {
  constructor() {
    super({
      name: "search",
      title: "Search local web content",
      description: "Searches content records currently available to the local React app.",
      inputSchema: {
        type: "object",
        required: ["query"],
        additionalProperties: false,
        properties: {
          query: { type: "string" },
        },
      },
    });
  }

  async execute({ query }) {
    const content = await readContent();
    const normalizedQuery = String(query ?? "").toLowerCase();
    const results = Object.values(content.items ?? {}).filter((item) => (
      `${item.headline ?? ""}\\n${item.body ?? ""}`.toLowerCase().includes(normalizedQuery)
    ));
    return new JsonContent({ json: { ok: true, results } });
  }
}

class ReactContentToolkit extends Toolkit {
  constructor() {
    super({
      name: toolkitName,
      title: "React Local Content Toolkit",
      description: "Local-only create, update, and search tools for the React dev app content.",
      tools: [
        new CreateContentTool(),
        new UpdateContentTool(),
        new SearchContentTool(),
      ],
    });
  }
}

async function main() {
  const probe = process.env.MESHAGENT_INIT_DEV_PROBE;
  if (!probe || !process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    return;
  }

  const room = new RoomClient();
  await room.start();
  const hostedToolkit = await startHostedToolkit({
    room,
    toolkit: new ReactContentToolkit(),
    public_: true,
  });

  try {
    const proofId = "meshagent-init-proof";
    const created = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "create",
      arguments: {
        id: proofId,
        headline: "Local dev content created through MeshAgent",
        body: "This text was created by the local React content toolkit.",
      },
    });
    console.log(`MeshAgent create dev toolkit create: ${JSON.stringify(created.json)}`);

    const headline = `MeshAgent local dev proof ${probe}`;
    const updated = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "update",
      arguments: {
        id: proofId,
        headline,
        body: "The room invoked the local toolkit, and the toolkit updated src/dev-content.json.",
      },
    });
    console.log(`MeshAgent create dev toolkit update: ${JSON.stringify(updated.json)}`);

    const searched = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "search",
      arguments: { query: probe },
    });
    console.log(`MeshAgent create dev toolkit search: ${JSON.stringify(searched.json)}`);

    const content = await readContent();
    const activeItem = content.items?.[content.activeId];
    const searchResults = searched.json?.results ?? [];
    if (
      activeItem?.headline !== headline
      || !searchResults.some((item) => item?.headline === headline)
    ) {
      throw new Error("Local React content toolkit proof did not update searchable web content.");
    }

    console.log(`MeshAgent create dev toolkit proof wrote: src/dev-content.json ${probe}`);
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_INIT_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
    if (Number.isFinite(holdSeconds) && holdSeconds > 0) {
      console.log(`MeshAgent create dev toolkit holding registration for ${holdSeconds}s`);
      await sleep(holdSeconds * 1000);
    }
  } finally {
    await hostedToolkit.stop();
    room.dispose();
  }
}

main().catch((error) => {
  console.error("Unable to run MeshAgent create dev content toolkit proof:", error);
  process.exitCode = 1;
});
"""

REACT_DEV_CONTENT_JSON = """\
{
  "activeId": "hero",
  "items": {
    "hero": {
      "id": "hero",
      "headline": "hello from meshagent create",
      "body": "Run npm run dev to let the local MeshAgent toolkit update this content."
    }
  }
}
"""

REACT_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2022",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2022"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Node",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx"
  },
  "include": ["src"]
}
"""

REACT_VITE_CONFIG = """\
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
});
"""

REACT_INDEX_HTML = """\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>MeshAgent Create</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

REACT_MAIN = """\
import React from "react";
import { createRoot } from "react-dom/client";
import devContent from "./dev-content.json";

type DevContent = {
  activeId: string;
  items: Record<string, { headline: string; body: string }>;
};

function App() {
  const content = devContent as DevContent;
  const activeItem = content.items[content.activeId] ?? content.items.hero;
  return (
    <main>
      <h1>{activeItem.headline}</h1>
      <p>{activeItem.body}</p>
    </main>
  );
}

const root = document.getElementById("root");
if (root === null) {
  throw new Error("Root element was not found");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
"""

REACT_DOCKERFILE = """\
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json tsconfig.json vite.config.ts index.html ./
RUN npm install
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
  'pid /data/nginx/nginx.pid;' \\
  'events {}' \\
  'http {' \\
  '  include /etc/nginx/mime.types;' \\
  '  client_body_temp_path /data/nginx/client_temp;' \\
  '  proxy_temp_path /data/nginx/proxy_temp;' \\
  '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
  '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
  '  scgi_temp_path /data/nginx/scgi_temp;' \\
  '  server { listen 80; location = /health { return 200 "ok\\n"; } location = /status { return 200 "ready\\n"; } location = /api/ping { default_type application/json; return 200 "{\\"pong\\":true}\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
  '}' > /etc/nginx/nginx.conf
EXPOSE 80
CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
"""

REACT_DOCKERIGNORE = """\
node_modules/
dist/
.npm-cache/
npm-debug.log*
.git/
.DS_Store
"""

DOTNET_CSPROJ = f"""\
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Meshagent.Api" Version="{__version__}" />
  </ItemGroup>
</Project>
"""

DOTNET_AGENT_CSPROJ = f"""\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net9.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Meshagent.Api" Version="{__version__}" />
  </ItemGroup>
</Project>
"""

DOTNET_PROGRAM = """\
using System.Text;
using System.Text.Json;
using Meshagent.Api.Room;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var port = Environment.GetEnvironmentVariable("PORT") ?? "5000";
app.Urls.Clear();
app.Urls.Add($"http://0.0.0.0:{port}");

app.MapGet("/health", () => Results.Text("ok\\n", "text/plain"));
app.MapGet("/status", () => Results.Text("ready\\n", "text/plain"));
app.MapGet("/api/ping", () => Results.Json(new { pong = true }));
app.MapGet("/", () => Results.Text("hello from meshagent create\\n", "text/plain"));

_ = PublishDevReadyMarkerAsync("webserver");

await app.RunAsync();

static async Task PublishDevReadyMarkerAsync(string focus)
{
    var readyPath = Environment.GetEnvironmentVariable("MESHAGENT_INIT_DEV_READY_PATH");
    var probe = Environment.GetEnvironmentVariable("MESHAGENT_INIT_DEV_PROBE");
    if (
        string.IsNullOrWhiteSpace(readyPath)
        || string.IsNullOrWhiteSpace(probe)
        || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_ROOM"))
        || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_TOKEN"))
    )
    {
        return;
    }

    await using var room = new RoomClient();
    await room.ConnectAsync();
    var payload = JsonSerializer.Serialize(new
    {
        probe,
        room = room.RoomName,
        language = "dotnet",
        focus,
    }) + "\\n";
    await room.Storage.Upload(
        readyPath,
        Encoding.UTF8.GetBytes(payload),
        overwrite: true,
        mimeType: "application/json");
    Console.WriteLine($"MeshAgent create dev probe wrote: {readyPath} {probe}");
    await room.WaitForCloseAsync();
}
"""

DOTNET_AGENT_PROGRAM = """\
using System.Text;
using System.Text.Json;
using Meshagent.Api.Room;

if (
    string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_ROOM"))
    || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MESHAGENT_TOKEN"))
)
{
    Console.WriteLine("MeshAgent room environment is not set; waiting for deployment env.");
    await Task.Delay(Timeout.InfiniteTimeSpan);
    return;
}

await using var room = new RoomClient();
await room.ConnectAsync();
Console.WriteLine($"Connected to MeshAgent room: {room.RoomName}");
await PublishDevReadyMarkerAsync(room);
await Task.Delay(Timeout.InfiniteTimeSpan);

static async Task PublishDevReadyMarkerAsync(RoomClient room)
{
    var readyPath = Environment.GetEnvironmentVariable("MESHAGENT_INIT_DEV_READY_PATH");
    var probe = Environment.GetEnvironmentVariable("MESHAGENT_INIT_DEV_PROBE");
    if (string.IsNullOrWhiteSpace(readyPath) || string.IsNullOrWhiteSpace(probe))
    {
        return;
    }

    var payload = JsonSerializer.Serialize(new
    {
        probe,
        room = room.RoomName,
        language = "dotnet",
        focus = "backend-agent",
    }) + "\\n";
    await room.Storage.Upload(
        readyPath,
        Encoding.UTF8.GetBytes(payload),
        overwrite: true,
        mimeType: "application/json");
    Console.WriteLine($"MeshAgent create dev probe wrote: {readyPath} {probe}");
}
"""

DOTNET_DOCKERFILE = """\
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
WORKDIR /src
COPY MeshAgentHello.csproj ./
RUN dotnet restore
COPY Program.cs ./
RUN dotnet publish -c Release -o /app/publish --no-restore --disable-build-servers /p:UseSharedCompilation=false

FROM mcr.microsoft.com/dotnet/aspnet:9.0
WORKDIR /app
COPY --from=build /app/publish .
EXPOSE 5000
ENTRYPOINT ["dotnet", "MeshAgentHello.dll"]
"""

DOTNET_AGENT_DOCKERFILE = """\
FROM mcr.microsoft.com/dotnet/sdk:9.0 AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1
WORKDIR /src
COPY MeshAgentHello.csproj ./
RUN dotnet restore
COPY Program.cs ./
RUN dotnet publish -c Release -o /app/publish --no-restore --disable-build-servers /p:UseSharedCompilation=false

FROM mcr.microsoft.com/dotnet/runtime:9.0
WORKDIR /app
COPY --from=build /app/publish .
ENTRYPOINT ["dotnet", "MeshAgentHello.dll"]
"""

DOTNET_DOCKERIGNORE = """\
bin/
obj/
.dotnet-home/
.nuget/
.git/
.DS_Store
"""

DOTNET_INSTALL_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"
NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"
DOTNET_NOLOGO="${DOTNET_NOLOGO:-1}"
DOTNET_SKIP_FIRST_TIME_EXPERIENCE="${DOTNET_SKIP_FIRST_TIME_EXPERIENCE:-1}"
export DOTNET_CLI_HOME NUGET_PACKAGES DOTNET_NOLOGO DOTNET_SKIP_FIRST_TIME_EXPERIENCE
if command -v dotnet >/dev/null 2>&1; then
  dotnet restore
else
  echo "The .NET SDK 9.0 is required on the host. Install dotnet, then rerun this script." >&2
  exit 127
fi
"""

DOTNET_DEV_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DOTNET_CLI_HOME="${DOTNET_CLI_HOME:-$ROOT/.dotnet-home}"
NUGET_PACKAGES="${NUGET_PACKAGES:-$ROOT/.nuget/packages}"
DOTNET_NOLOGO="${DOTNET_NOLOGO:-1}"
DOTNET_SKIP_FIRST_TIME_EXPERIENCE="${DOTNET_SKIP_FIRST_TIME_EXPERIENCE:-1}"
export DOTNET_CLI_HOME NUGET_PACKAGES DOTNET_NOLOGO DOTNET_SKIP_FIRST_TIME_EXPERIENCE
if command -v dotnet >/dev/null 2>&1; then
  meshagent room connect -- dotnet run
else
  echo "The .NET SDK 9.0 is required on the host. Install dotnet, then rerun this script." >&2
  exit 127
fi
"""

DOTNET_WEBSERVER_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-dotnet-webserver:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --wait
"""

DOTNET_AGENT_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-dotnet-agent:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait
"""

DART_AGENT_PUBSPEC = f"""\
name: meshagent_init_dart_agent
publish_to: "none"
environment:
  sdk: ">=3.8.0 <4.0.0"
dependencies:
  meshagent: ^{__version__}
"""

DART_AGENT = """\
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:meshagent/meshagent.dart';

Future<void> main() async {
  final roomName = Platform.environment['MESHAGENT_ROOM'];
  final token = Platform.environment['MESHAGENT_TOKEN'];
  if (roomName == null || roomName.isEmpty || token == null || token.isEmpty) {
    print('MeshAgent room environment is not set; waiting for deployment env.');
    await Completer<void>().future;
    return;
  }

  final room = RoomClient(
    protocolFactory: WebSocketClientProtocol.createFactory(
      url: _websocketRoomUrl(roomName),
      token: token,
    ),
  );
  await room.start();
  print('Connected to MeshAgent room: ${room.roomName}');
  final wroteDevProof = await publishDevReadyMarker(room);
  if (wroteDevProof) {
    room.dispose();
    return;
  }
  await Completer<void>().future;
}

Future<bool> publishDevReadyMarker(RoomClient room) async {
  final readyPath = Platform.environment['MESHAGENT_INIT_DEV_READY_PATH'];
  final probe = Platform.environment['MESHAGENT_INIT_DEV_PROBE'];
  if (readyPath == null || readyPath.isEmpty || probe == null || probe.isEmpty) {
    return false;
  }

  final payload = jsonEncode({
    'probe': probe,
    'room': room.roomName,
    'language': 'dart',
    'focus': 'backend-agent',
  });
  await room.storage.upload(
    readyPath,
    Uint8List.fromList(utf8.encode('$payload\\n')),
    overwrite: true,
    mimeType: 'application/json',
  );
  print('MeshAgent create dev probe wrote: $readyPath $probe');
  return true;
}

Uri _websocketRoomUrl(String roomName) {
  var baseUrl = Platform.environment['MESHAGENT_ROOM_URL'] ??
      Platform.environment['MESHAGENT_API_URL'] ??
      'https://api.meshagent.com';
  if (baseUrl.startsWith('https:')) {
    baseUrl = 'wss:${baseUrl.substring('https:'.length)}';
  } else if (baseUrl.startsWith('http:')) {
    baseUrl = 'ws:${baseUrl.substring('http:'.length)}';
  }
  return Uri.parse('$baseUrl/rooms/$roomName');
}

"""

DART_AGENT_DOCKERFILE = """\
FROM dart:stable
WORKDIR /app
COPY pubspec.yaml ./
RUN dart pub get
COPY bin ./bin
CMD ["dart", "run", "bin/server.dart"]
"""

DART_DOCKERIGNORE = """\
.dart_tool/
.pub-cache/
build/
.git/
.DS_Store
"""

DART_INSTALL_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if command -v dart >/dev/null 2>&1; then
  dart pub get
else
  echo "The Dart SDK is required on the host. Install dart, then rerun this script." >&2
  exit 127
fi
"""

DART_DEV_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if command -v dart >/dev/null 2>&1; then
  meshagent room connect -- dart run bin/server.dart
else
  echo "The Dart SDK is required on the host. Install dart, then rerun this script." >&2
  exit 127
fi
"""

DART_AGENT_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-dart-agent:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --meshagent-token agentDefault --wait
"""

FLUTTER_PUBSPEC = f"""\
name: meshagent_init_flutter
description: A minimal deployable Flutter web app for MeshAgent.
publish_to: "none"
environment:
  sdk: ">=3.8.0 <4.0.0"
dependencies:
  flutter:
    sdk: flutter
  meshagent: ^{__version__}
"""

FLUTTER_DEV_ROOM_PROOF_DART = """\
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:meshagent/meshagent.dart';

Future<void> main() async {
  final readyPath = Platform.environment['MESHAGENT_INIT_DEV_READY_PATH'];
  final probe = Platform.environment['MESHAGENT_INIT_DEV_PROBE'];
  final roomName = Platform.environment['MESHAGENT_ROOM'];
  final token = Platform.environment['MESHAGENT_TOKEN'];
  if (
    readyPath == null ||
    readyPath.isEmpty ||
    probe == null ||
    probe.isEmpty ||
    roomName == null ||
    roomName.isEmpty ||
    token == null ||
    token.isEmpty
  ) {
    return;
  }

  final room = RoomClient(
    protocolFactory: WebSocketClientProtocol.createFactory(
      url: _websocketRoomUrl(roomName),
      token: token,
    ),
  );
  await room.start();
  final payload = jsonEncode({
    'probe': probe,
    'room': room.roomName,
    'language': 'flutter',
    'focus': 'webserver',
  });
  await room.storage.upload(
    readyPath,
    Uint8List.fromList(utf8.encode('$payload\\n')),
    overwrite: true,
    mimeType: 'application/json',
  );
  print('MeshAgent create dev probe wrote: $readyPath $probe');
  room.dispose();
}

Uri _websocketRoomUrl(String roomName) {
  var baseUrl = Platform.environment['MESHAGENT_ROOM_URL'] ??
      Platform.environment['MESHAGENT_API_URL'] ??
      'https://api.meshagent.com';
  if (baseUrl.startsWith('https:')) {
    baseUrl = 'wss:${baseUrl.substring('https:'.length)}';
  } else if (baseUrl.startsWith('http:')) {
    baseUrl = 'ws:${baseUrl.substring('http:'.length)}';
  }
  return Uri.parse('$baseUrl/rooms/$roomName');
}
"""

FLUTTER_MAIN = """\
import 'package:flutter/material.dart';

void main() {
  runApp(const MeshAgentInitApp());
}

class MeshAgentInitApp extends StatelessWidget {
  const MeshAgentInitApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'MeshAgent Create',
      home: Scaffold(
        body: Center(
          child: Text(
            'hello from meshagent create',
            style: Theme.of(context).textTheme.headlineMedium,
          ),
        ),
      ),
    );
  }
}
"""

FLUTTER_INDEX_HTML = """\
<!doctype html>
<html>
<head>
  <base href="$FLUTTER_BASE_HREF">
  <meta charset="UTF-8">
  <meta content="IE=Edge" http-equiv="X-UA-Compatible">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MeshAgent Create</title>
</head>
<body>
  <script src="flutter_bootstrap.js" async></script>
</body>
</html>
"""

FLUTTER_DOCKERFILE = """\
FROM ghcr.io/cirruslabs/flutter:stable AS build
WORKDIR /app
COPY pubspec.yaml ./
RUN flutter pub get
COPY lib ./lib
COPY web ./web
RUN flutter build web --release

FROM nginx:1.27-alpine
COPY --from=build /app/build/web /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf && printf '%s\\n' \\
  'pid /data/nginx/nginx.pid;' \\
  'events {}' \\
  'http {' \\
  '  include /etc/nginx/mime.types;' \\
  '  client_body_temp_path /data/nginx/client_temp;' \\
  '  proxy_temp_path /data/nginx/proxy_temp;' \\
  '  fastcgi_temp_path /data/nginx/fastcgi_temp;' \\
  '  uwsgi_temp_path /data/nginx/uwsgi_temp;' \\
  '  scgi_temp_path /data/nginx/scgi_temp;' \\
  '  server { listen 80; location = /health { return 200 "ok\\n"; } location = /status { return 200 "ready\\n"; } location = /api/ping { default_type application/json; return 200 "{\\"pong\\":true}\\n"; } location / { try_files $uri $uri/ /index.html; } }' \\
  '}' > /etc/nginx/nginx.conf
EXPOSE 80
CMD ["sh", "-c", "mkdir -p /data/nginx/client_temp /data/nginx/proxy_temp /data/nginx/fastcgi_temp /data/nginx/uwsgi_temp /data/nginx/scgi_temp && nginx -c /etc/nginx/nginx.conf -g 'daemon off;'"]
"""

FLUTTER_DOCKERIGNORE = """\
.dart_tool/
.pub-cache/
build/
.flutter-plugins
.flutter-plugins-dependencies
.git/
.DS_Store
"""

FLUTTER_INSTALL_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if command -v flutter >/dev/null 2>&1; then
  flutter pub get
else
  echo "The Flutter SDK is required on the host. Install flutter, then rerun this script." >&2
  exit 127
fi
"""

FLUTTER_DEV_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PUB_CACHE="${PUB_CACHE:-$ROOT/.pub-cache}"
export PUB_CACHE
if [ -n "${MESHAGENT_INIT_DEV_PROBE:-}" ] && [ -n "${MESHAGENT_INIT_DEV_READY_PATH:-}" ]; then
  if command -v dart >/dev/null 2>&1; then
    meshagent room connect -- dart run tool/dev_room_proof.dart
  else
    echo "The Dart SDK is required on the host for the Flutter dev proof. Install dart, then rerun this script." >&2
    exit 127
  fi
elif command -v flutter >/dev/null 2>&1 && command -v dart >/dev/null 2>&1; then
  meshagent room connect -- sh -c 'dart run tool/dev_room_proof.dart & flutter run -d web-server --web-hostname 0.0.0.0 --web-port 3000'
else
  echo "The Flutter SDK and Dart SDK are required on the host. Install flutter and dart, then rerun this script." >&2
  exit 127
fi
"""

FLUTTER_DEPLOY_SCRIPT = """\
#!/usr/bin/env sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_TAG="${IMAGE_TAG:-meshagent-init-flutter:dev}"
meshagent deploy . --tag "$IMAGE_TAG" --public --liveness /health --room-mount /:/data:rw --wait
"""

WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
STATIC_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
AGENT_NEXT_STEPS = (
    "meshagent doctor",
    "./scripts/install.sh",
    "MESHAGENT_ROOM=<room> ./scripts/dev.sh",
    "./scripts/deploy.sh",
)
NPM_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)
NPM_STATIC_WEBSERVER_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)
NPM_AGENT_NEXT_STEPS = (
    "meshagent doctor",
    "npm install",
    "MESHAGENT_ROOM=<room> npm run dev",
    "npm run deploy",
)

LANGUAGES: Mapping[str, InitLanguage] = {
    "python": InitLanguage(
        id="python",
        label="Python",
        description="Python 3.13.",
    ),
    "javascript": InitLanguage(
        id="javascript",
        label="JavaScript",
        description="Node.js/CommonJS.",
    ),
    "typescript": InitLanguage(
        id="typescript",
        label="TypeScript",
        description="Node.js/TypeScript.",
    ),
    "react": InitLanguage(
        id="react",
        label="React",
        description="React/Vite.",
    ),
    "dotnet": InitLanguage(
        id="dotnet",
        label=".NET",
        description=".NET.",
    ),
    "dart-flutter": InitLanguage(
        id="dart-flutter",
        label="Dart/Flutter",
        description="Dart or Flutter.",
    ),
}

FOCUSES: Mapping[str, InitFocus] = {
    WEB_FOCUS: InitFocus(
        id=WEB_FOCUS,
        label="Web server",
        description="HTTP app with a health endpoint and public route.",
    ),
    AGENT_FOCUS: InitFocus(
        id=AGENT_FOCUS,
        label="Backend agent",
        description="Headless RoomClient SDK service without a public port.",
    ),
}

TEMPLATES: Mapping[tuple[str, str], InitTemplate] = {
    ("python", WEB_FOCUS): InitTemplate(
        language_id="python",
        focus_id=WEB_FOCUS,
        label="Python web server",
        description="Async Python HTTP service on a declared container port.",
        files={
            "pyproject.toml": PYTHON_WEBSERVER_PYPROJECT,
            "server.py": PYTHON_WEBSERVER,
            "dev-content.json": PYTHON_DEV_CONTENT_JSON,
            "Dockerfile": PYTHON_DOCKERFILE,
            ".dockerignore": PYTHON_DOCKERIGNORE,
            "scripts/install.sh": PYTHON_INSTALL_SCRIPT,
            "scripts/dev.sh": PYTHON_DEV_SCRIPT,
            "scripts/deploy.sh": PYTHON_WEBSERVER_DEPLOY_SCRIPT,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("python", AGENT_FOCUS): InitTemplate(
        language_id="python",
        focus_id=AGENT_FOCUS,
        label="Python backend agent",
        description="Headless Python RoomClient service.",
        files={
            "pyproject.toml": PYTHON_AGENT_PYPROJECT,
            "server.py": PYTHON_AGENT,
            "Dockerfile": PYTHON_AGENT_DOCKERFILE,
            ".dockerignore": PYTHON_DOCKERIGNORE,
            "scripts/install.sh": PYTHON_INSTALL_SCRIPT,
            "scripts/dev.sh": PYTHON_DEV_SCRIPT,
            "scripts/deploy.sh": PYTHON_AGENT_DEPLOY_SCRIPT,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("javascript", WEB_FOCUS): InitTemplate(
        language_id="javascript",
        focus_id=WEB_FOCUS,
        label="JavaScript web server",
        description="Node.js HTTP service on a declared container port.",
        files={
            "package.json": JAVASCRIPT_PACKAGE_JSON,
            ".npmrc": NPMRC,
            "server.js": JAVASCRIPT_WEBSERVER,
            "dev-content.json": NODE_DEV_CONTENT_JSON,
            "Dockerfile": JAVASCRIPT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("javascript", AGENT_FOCUS): InitTemplate(
        language_id="javascript",
        focus_id=AGENT_FOCUS,
        label="JavaScript backend agent",
        description="Headless Node.js RoomClient service.",
        files={
            "package.json": JAVASCRIPT_AGENT_PACKAGE_JSON,
            ".npmrc": NPMRC,
            "server.js": JAVASCRIPT_AGENT,
            "Dockerfile": JAVASCRIPT_AGENT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("typescript", WEB_FOCUS): InitTemplate(
        language_id="typescript",
        focus_id=WEB_FOCUS,
        label="TypeScript web server",
        description="Node.js TypeScript HTTP service on a declared container port.",
        files={
            "package.json": TYPESCRIPT_PACKAGE_JSON,
            ".npmrc": NPMRC,
            "tsconfig.json": TYPESCRIPT_TSCONFIG,
            "src/server.ts": TYPESCRIPT_WEBSERVER,
            "src/dev-content.json": NODE_DEV_CONTENT_JSON,
            "Dockerfile": TYPESCRIPT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=NPM_WEBSERVER_NEXT_STEPS,
    ),
    ("typescript", AGENT_FOCUS): InitTemplate(
        language_id="typescript",
        focus_id=AGENT_FOCUS,
        label="TypeScript backend agent",
        description="Headless TypeScript RoomClient service.",
        files={
            "package.json": TYPESCRIPT_AGENT_PACKAGE_JSON,
            ".npmrc": NPMRC,
            "tsconfig.json": TYPESCRIPT_TSCONFIG,
            "src/server.ts": TYPESCRIPT_AGENT,
            "Dockerfile": TYPESCRIPT_AGENT_DOCKERFILE,
            ".dockerignore": JAVASCRIPT_DOCKERIGNORE,
        },
        next_steps=NPM_AGENT_NEXT_STEPS,
    ),
    ("react", WEB_FOCUS): InitTemplate(
        language_id="react",
        focus_id=WEB_FOCUS,
        label="React web server",
        description="React/Vite web app served by nginx on a declared container port.",
        files={
            "package.json": REACT_PACKAGE_JSON,
            ".npmrc": NPMRC,
            "tsconfig.json": REACT_TSCONFIG,
            "vite.config.ts": REACT_VITE_CONFIG,
            "index.html": REACT_INDEX_HTML,
            "scripts/dev-content-toolkit.js": REACT_DEV_CONTENT_TOOLKIT_JS,
            "src/dev-content.json": REACT_DEV_CONTENT_JSON,
            "src/main.tsx": REACT_MAIN,
            "Dockerfile": REACT_DOCKERFILE,
            ".dockerignore": REACT_DOCKERIGNORE,
        },
        next_steps=NPM_STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", WEB_FOCUS): InitTemplate(
        language_id="dotnet",
        focus_id=WEB_FOCUS,
        label=".NET web server",
        description="ASP.NET Core HTTP service on a declared container port.",
        files={
            "MeshAgentHello.csproj": DOTNET_CSPROJ,
            "Program.cs": DOTNET_PROGRAM,
            "Dockerfile": DOTNET_DOCKERFILE,
            ".dockerignore": DOTNET_DOCKERIGNORE,
            "scripts/install.sh": DOTNET_INSTALL_SCRIPT,
            "scripts/dev.sh": DOTNET_DEV_SCRIPT,
            "scripts/deploy.sh": DOTNET_WEBSERVER_DEPLOY_SCRIPT,
        },
        next_steps=WEBSERVER_NEXT_STEPS,
    ),
    ("dotnet", AGENT_FOCUS): InitTemplate(
        language_id="dotnet",
        focus_id=AGENT_FOCUS,
        label=".NET backend agent",
        description="Headless .NET RoomClient service.",
        files={
            "MeshAgentHello.csproj": DOTNET_AGENT_CSPROJ,
            "Program.cs": DOTNET_AGENT_PROGRAM,
            "Dockerfile": DOTNET_AGENT_DOCKERFILE,
            ".dockerignore": DOTNET_DOCKERIGNORE,
            "scripts/install.sh": DOTNET_INSTALL_SCRIPT,
            "scripts/dev.sh": DOTNET_DEV_SCRIPT,
            "scripts/deploy.sh": DOTNET_AGENT_DEPLOY_SCRIPT,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
    ("dart-flutter", WEB_FOCUS): InitTemplate(
        language_id="dart-flutter",
        focus_id=WEB_FOCUS,
        label="Flutter web server",
        description="Flutter web app served by nginx on a declared container port.",
        files={
            "pubspec.yaml": FLUTTER_PUBSPEC,
            "lib/main.dart": FLUTTER_MAIN,
            "tool/dev_room_proof.dart": FLUTTER_DEV_ROOM_PROOF_DART,
            "web/index.html": FLUTTER_INDEX_HTML,
            "Dockerfile": FLUTTER_DOCKERFILE,
            ".dockerignore": FLUTTER_DOCKERIGNORE,
            "scripts/install.sh": FLUTTER_INSTALL_SCRIPT,
            "scripts/dev.sh": FLUTTER_DEV_SCRIPT,
            "scripts/deploy.sh": FLUTTER_DEPLOY_SCRIPT,
        },
        next_steps=STATIC_WEBSERVER_NEXT_STEPS,
    ),
    ("dart-flutter", AGENT_FOCUS): InitTemplate(
        language_id="dart-flutter",
        focus_id=AGENT_FOCUS,
        label="Dart backend agent",
        description="Headless Dart RoomClient service.",
        files={
            "pubspec.yaml": DART_AGENT_PUBSPEC,
            "bin/server.dart": DART_AGENT,
            "Dockerfile": DART_AGENT_DOCKERFILE,
            ".dockerignore": DART_DOCKERIGNORE,
            "scripts/install.sh": DART_INSTALL_SCRIPT,
            "scripts/dev.sh": DART_DEV_SCRIPT,
            "scripts/deploy.sh": DART_AGENT_DEPLOY_SCRIPT,
        },
        next_steps=AGENT_NEXT_STEPS,
    ),
}

LANGUAGE_ALIASES = {
    ".net": "dotnet",
    "c#": "dotnet",
    "csharp": "dotnet",
    "dart": "dart-flutter",
    "dart/flutter": "dart-flutter",
    "dart-flutter": "dart-flutter",
    "dotnet": "dotnet",
    "flutter": "dart-flutter",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "node.js": "javascript",
    "nodejs": "javascript",
    "python": "python",
    "py": "python",
    "react": "react",
    "react.js": "react",
    "reactjs": "react",
    "react-vite": "react",
    "ts": "typescript",
    "typescript": "typescript",
    "node-ts": "typescript",
    "node.ts": "typescript",
    "vite-react": "react",
}
FOCUS_ALIASES = {
    "agent": AGENT_FOCUS,
    "backend": AGENT_FOCUS,
    "backend-agent": AGENT_FOCUS,
    "backend_agent": AGENT_FOCUS,
    "roomclient": AGENT_FOCUS,
    "room-client": AGENT_FOCUS,
    "web": WEB_FOCUS,
    "webserver": WEB_FOCUS,
    "web-server": WEB_FOCUS,
    "web_server": WEB_FOCUS,
    "webservering": WEB_FOCUS,
    "webserving": WEB_FOCUS,
}


def _has_existing_project_content(root: Path) -> bool:
    for path in sorted(root.rglob("*")):
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if path.is_dir():
            continue
        if path.name in IGNORED_FILE_NAMES:
            continue
        if path.name in PROJECT_MARKER_NAMES:
            return True
        if path.suffix.lower() in SOURCE_SUFFIXES:
            return True
    return False


def _write_file(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_contents = render_meshagent_image_prefix_template(
        textwrap.dedent(contents).lstrip()
    )
    path.write_text(rendered_contents, encoding="utf-8")
    if path.suffix == ".sh":
        path.chmod(0o755)


def _supported_language_text() -> str:
    return ", ".join(language.id for language in LANGUAGES.values())


def _supported_focus_text() -> str:
    return ", ".join(focus.id for focus in FOCUSES.values())


def _resolve_language_id(language: str | None) -> str:
    if language is None or language.strip() == "":
        return DEFAULT_LANGUAGE

    normalized = language.strip().lower()
    language_id = LANGUAGE_ALIASES.get(normalized)
    if language_id is None:
        expected = _supported_language_text()
        raise click.ClickException(
            f"Unsupported language: {language}. Expected one of: {expected}."
        )
    return language_id


def _resolve_focus_id(focus: str | None) -> str:
    if focus is None or focus.strip() == "":
        return DEFAULT_FOCUS

    normalized = focus.strip().lower()
    focus_id = FOCUS_ALIASES.get(normalized)
    if focus_id is None:
        expected = _supported_focus_text()
        raise click.ClickException(
            f"Unsupported focus: {focus}. Expected one of: {expected}."
        )
    return focus_id


def _resolve_template(language_id: str, focus_id: str) -> InitTemplate:
    template = TEMPLATES.get((language_id, focus_id))
    if template is not None:
        return template

    language = LANGUAGES[language_id]
    supported = [
        focus
        for template_language_id, focus in TEMPLATES
        if template_language_id == language_id
    ]
    supported_text = ", ".join(supported) if supported else "none"
    raise click.ClickException(
        f"Unsupported template combination: {language.label} does not support "
        f"{focus_id}. Supported focus: {supported_text}."
    )


def _stdio_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _should_launch_tui(
    *,
    language: str | None,
    focus: str | None,
    interactive: bool | None,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> bool:
    if interactive is False:
        return False
    if interactive is True:
        return stdin_is_tty and stdout_is_tty
    return (language is None or focus is None) and stdin_is_tty and stdout_is_tty


def _supported_focus_ids_for_language(language_id: str) -> tuple[str, ...]:
    return tuple(
        focus_id
        for template_language_id, focus_id in TEMPLATES
        if template_language_id == language_id
    )


def _language_choices() -> Sequence[tuple[str, str, str, tuple[str, ...]]]:
    return tuple(
        (
            language.id,
            language.label,
            language.description,
            _supported_focus_ids_for_language(language.id),
        )
        for language in LANGUAGES.values()
    )


def _focus_choices() -> Sequence[tuple[str, str, str]]:
    return tuple(
        (focus.id, focus.label, focus.description) for focus in FOCUSES.values()
    )


def _run_init_tui(
    *,
    language_choices: Sequence[tuple[str, str, str, tuple[str, ...]]],
    focus_choices: Sequence[tuple[str, str, str]],
) -> tuple[str, str] | None:
    from meshagent.cli.tui.create import (
        InitFocusChoice,
        InitLanguageChoice,
        run_init_wizard_tui,
    )

    languages = [
        InitLanguageChoice(
            id=language_id,
            label=label,
            description=description,
            focus_ids=focus_ids,
        )
        for language_id, label, description, focus_ids in language_choices
    ]
    focuses = [
        InitFocusChoice(id=focus_id, label=label, description=description)
        for focus_id, label, description in focus_choices
    ]
    result = asyncio.run(run_init_wizard_tui(languages=languages, focuses=focuses))
    if result.status != "completed":
        return None
    if result.selected_language_id is None or result.selected_focus_id is None:
        return None
    return result.selected_language_id, result.selected_focus_id


def _run_existing_project_tui() -> ExistingProjectSelection | None:
    from meshagent.cli.tui.create import run_existing_project_init_tui

    result = asyncio.run(run_existing_project_init_tui())
    if result.status != "completed" or result.action is None:
        return None
    return ExistingProjectSelection(
        action=result.action,
        subfolder_name=result.subfolder_name,
    )


def _validate_subfolder_name(folder_name: str | None) -> str:
    if folder_name is None:
        raise click.ClickException("Folder name cannot be empty.")

    resolved_folder_name = folder_name.strip()
    if resolved_folder_name == "":
        raise click.ClickException("Folder name cannot be empty.")
    if (
        resolved_folder_name in {".", ".."}
        or "/" in resolved_folder_name
        or "\\" in resolved_folder_name
    ):
        raise click.ClickException("Folder name must be a single new subfolder name.")
    return resolved_folder_name


def _new_project_subfolder(root: Path, folder_name: str | None) -> Path:
    resolved_folder_name = _validate_subfolder_name(folder_name)
    target = root / resolved_folder_name
    if target.exists():
        raise click.ClickException(f"Subfolder already exists: {target}")
    return target


def _run_doctor(root: Path) -> None:
    from meshagent.cli.doctor import _print_report, diagnose_project

    _print_report(diagnose_project(root))


def _write_template(root: Path, template: InitTemplate) -> None:
    for name, contents in template.files.items():
        _write_file(root / name, contents)


def _print_created_report(*, template: InitTemplate) -> None:
    click.echo("")
    click.echo(f"Created a minimal deployable {template.label} hello world project:")
    for name in template.files:
        click.echo(f"  {name}")
    click.echo("")
    click.echo("Next steps:")
    for step in template.next_steps:
        click.echo(f"  {step}")


@click.command(
    "create",
    help="Create a minimal deployable hello world project.",
)
@click.option(
    "--language",
    "-l",
    type=str,
    default=None,
    help=(
        "Template language for non-interactive use. "
        "Supported: python, javascript, typescript, react, dotnet, dart/flutter."
    ),
)
@click.option(
    "--focus",
    type=str,
    default=None,
    help=(
        "Project focus for non-interactive use. Supported: webserver, backend-agent."
    ),
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help=(
        "Run or bypass the interactive template picker. Defaults to interactive "
        "when attached to a TTY and language or focus is missing."
    ),
)
@click.argument(
    "path",
    required=False,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
def create_command(
    path: Path | None = None,
    language: str | None = None,
    focus: str | None = None,
    interactive: bool | None = None,
) -> None:
    """Create a minimal project that can be deployed on MeshAgent."""

    root = (path or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    click.echo("meshagent create")
    click.echo(f"Project: {root}")

    is_interactive_stdio = _stdio_is_interactive()
    if interactive is True and not is_interactive_stdio:
        raise click.ClickException(
            "Interactive mode requires a TTY. Pass --language, --focus, and "
            "--no-interactive when running from a script."
        )

    if _has_existing_project_content(root):
        if interactive is not False and is_interactive_stdio:
            existing_project_selection = _run_existing_project_tui()
            if existing_project_selection is None:
                click.echo("Init canceled.")
                return
            if existing_project_selection.action == "run-doctor":
                click.echo("")
                _run_doctor(root)
                return

            root = _new_project_subfolder(
                root,
                existing_project_selection.subfolder_name,
            )
            root.mkdir(parents=True, exist_ok=False)
            click.echo(f"New project: {root}")
        else:
            click.echo("")
            click.echo("Existing application code or deployment metadata was detected.")
            click.echo("No files were written.")
            click.echo("")
            click.echo("Recommended next step for existing projects:")
            click.echo("  meshagent doctor")
            return

    if _should_launch_tui(
        language=language,
        focus=focus,
        interactive=interactive,
        stdin_is_tty=is_interactive_stdio,
        stdout_is_tty=is_interactive_stdio,
    ):
        selection = _run_init_tui(
            language_choices=_language_choices(),
            focus_choices=_focus_choices(),
        )
        if selection is None:
            click.echo("Init canceled.")
            return
        language_id, focus_id = selection
    else:
        language_id = _resolve_language_id(language)
        focus_id = _resolve_focus_id(focus)

    template = _resolve_template(language_id, focus_id)
    _write_template(root, template)
    _print_created_report(template=template)
