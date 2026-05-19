const fs = require("node:fs");
const path = require("node:path");
const { JsonContent, RoomClient, Tool, Toolkit, startHostedToolkit } = require("@meshagent/meshagent");

const proofDisplayPath = "agent-proof.json";
const proofPath = path.join(__dirname, proofDisplayPath.split("/").pop());
const toolkitName = "meshagent.create.javascript-agent";

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
  await fs.promises.writeFile(proofPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return payload;
}

class JavaScriptPingTool extends Tool {
  constructor() {
    super({
      name: "ping",
      title: "Ping local agent",
      description: "Checks that the local JavaScript backend agent toolkit is reachable.",
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

class JavaScriptStatusTool extends Tool {
  constructor() {
    super({
      name: "status",
      title: "Read local agent status",
      description: "Returns a minimal status payload from the local JavaScript backend agent.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
    });
  }

  async execute() {
    return new JsonContent({ json: { ready: true, language: "javascript", focus: "backend-agent" } });
  }
}

class JavaScriptEchoTool extends Tool {
  constructor() {
    super({
      name: "echo",
      title: "Echo a message",
      description: "Echoes a message through the local JavaScript backend agent.",
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

class JavaScriptAgentToolkit extends Toolkit {
  constructor() {
    super({
      name: toolkitName,
      title: "JavaScript Local Agent Toolkit",
      description: "Local-only ping, status, and echo tools for the JavaScript backend agent.",
      tools: [
        new JavaScriptPingTool(),
        new JavaScriptStatusTool(),
        new JavaScriptEchoTool(),
      ],
    });
  }
}

async function runDevAgentToolkit(existingRoom) {
  const probe = process.env.MESHAGENT_CREATE_DEV_PROBE;
  if (!process.env.MESHAGENT_ROOM || !process.env.MESHAGENT_TOKEN) {
    return;
  }

  const room = existingRoom ?? new RoomClient();
  if (!existingRoom) {
    await room.start();
  }
  const hostedToolkit = await startHostedToolkit({
    room,
    toolkit: new JavaScriptAgentToolkit(),
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
      throw new Error("Local JavaScript agent toolkit proof did not echo the probe.");
    }

    await writeAgentProof({ probe, echo: message });
    console.log(`MeshAgent create dev toolkit proof wrote: ${proofDisplayPath} ${probe}`);
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
