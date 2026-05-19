import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const contentPath = path.join(projectRoot, "src", "dev-content.json");
const toolkitName = "meshagent.create.react-content";

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
  await writeFile(contentPath, `${JSON.stringify(content, null, 2)}\n`, "utf8");
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
      `${item.headline ?? ""}\n${item.body ?? ""}`.toLowerCase().includes(normalizedQuery)
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
  const probe = process.env.MESHAGENT_CREATE_DEV_PROBE;
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
    const proofId = "meshagent-create-proof";
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
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
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
