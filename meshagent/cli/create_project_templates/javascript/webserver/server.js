const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const contentDisplayPath = "dev-content.json";
const contentPath = path.join(__dirname, contentDisplayPath.split("/").pop());
const toolkitName = "meshagent.create.javascript-content";

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
  await fs.promises.writeFile(contentPath, `${JSON.stringify(content, null, 2)}\n`, "utf8");
  return content;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

class JavaScriptCreateContentTool extends Tool {
  constructor() {
    super({
      name: "create",
      title: "Create local web content",
      description: "Creates a local JavaScript content record rendered by the dev app.",
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

class JavaScriptUpdateContentTool extends Tool {
  constructor() {
    super({
      name: "update",
      title: "Update local web content",
      description: "Updates a local JavaScript content record rendered by the dev app.",
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

class JavaScriptSearchContentTool extends Tool {
  constructor() {
    super({
      name: "search",
      title: "Search local web content",
      description: "Searches content records currently available to the local JavaScript app.",
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
      `${item.headline ?? ""}\n${item.body ?? ""}`.toLowerCase().includes(normalizedQuery)
    ));
    return new JsonContent({ json: { ok: true, results } });
  }
}

class JavaScriptContentToolkit extends Toolkit {
  constructor() {
    super({
      name: toolkitName,
      title: "JavaScript Local Content Toolkit",
      description: "Local-only create, update, and search tools for the JavaScript dev app content.",
      tools: [
        new JavaScriptCreateContentTool(),
        new JavaScriptUpdateContentTool(),
        new JavaScriptSearchContentTool(),
      ],
    });
  }
}

async function runDevContentToolkit(focus, existingRoom) {
  const probe = process.env.MESHAGENT_CREATE_DEV_PROBE;
  if (!probe || !process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    return;
  }

  const room = existingRoom ?? new RoomClient();
  if (!existingRoom) {
    await room.start();
  }
  const hostedToolkit = await startHostedToolkit({
    room,
    toolkit: new JavaScriptContentToolkit(),
    public_: true,
  });

  try {
    const proofId = "meshagent-create-proof";
    const created = await room.agents.invokeTool({
      toolkit: toolkitName,
      tool: "create",
      arguments: {
        id: proofId,
        headline: "Local dev content created through MeshAgent",
        body: `This text was created by the local JavaScript content toolkit for ${focus}.`,
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
        body: `The room invoked the local JavaScript toolkit for ${focus}, and the toolkit updated ${contentDisplayPath}.`,
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
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
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


const port = Number(process.env.PORT || 3000);

const server = http.createServer(async (request, response) => {
  try {
    if (request.url === "/health") {
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end("ok\n");
      return;
    }

    if (request.url === "/status") {
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end("ready\n");
      return;
    }

    if (request.url === "/api/ping") {
      response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ pong: true }) + "\n");
      return;
    }

    if (request.url === "/") {
      const content = await readContent();
      const activeItem = content.items?.[content.activeId] ?? content.items?.hero;
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
      response.end(`${activeItem?.headline ?? "hello from meshagent create"}\n${activeItem?.body ?? ""}\n`);
      return;
    }

    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("not found\n");
  } catch (error) {
    console.error("Unable to handle request:", error);
    if (!response.headersSent) {
      response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
    }
    response.end("internal server error\n");
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`Serving on 0.0.0.0:${port}`);
});

runDevContentToolkit("webserver").catch((error) => {
  console.error("Unable to run MeshAgent create dev content toolkit proof:", error);
  process.exitCode = 1;
});
