// @ts-nocheck
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder, TextEncoder } = require("node:util");
const {
  ChildProperty,
  ElementType,
  JsonContent,
  MeshSchema,
  RoomClient,
  SimpleValue,
  Tool,
  Toolkit,
  ValueProperty,
  startHostedToolkit,
} = require("@meshagent/meshagent");

const proofDisplayPath = "src/chatbot-proof.json";
const proofPath = path.join(__dirname, proofDisplayPath.split("/").pop());
const storageProofDisplayPath = "src/chatbot-storage-proof.json";
const storageProofPath = path.join(__dirname, storageProofDisplayPath.split("/").pop());
const toolkitName = "meshagent.create.typescript-chatbot";
const threadDir = ".threads/meshagent-create/typescript-chatbot";
const threadListPath = `${threadDir}/index.threadl`;

const threadSchema = new MeshSchema({
  rootTagName: "thread",
  elements: [
    new ElementType({
      tagName: "thread",
      description: "a MeshAgent chat thread",
      properties: [
        new ValueProperty({ name: "name", type: SimpleValue.string }),
        new ChildProperty({
          name: "properties",
          childTagNames: ["members", "messages"],
          ordered: true,
        }),
      ],
    }),
    new ElementType({
      tagName: "members",
      properties: [
        new ChildProperty({ name: "items", childTagNames: ["member"] }),
      ],
    }),
    new ElementType({
      tagName: "messages",
      properties: [
        new ValueProperty({ name: "external_thread_id", type: SimpleValue.string }),
        new ChildProperty({ name: "items", childTagNames: ["message"] }),
      ],
    }),
    new ElementType({
      tagName: "member",
      properties: [
        new ValueProperty({ name: "name", type: SimpleValue.string }),
      ],
    }),
    new ElementType({
      tagName: "message",
      properties: [
        new ValueProperty({ name: "id", type: SimpleValue.string }),
        new ValueProperty({ name: "turn_id", type: SimpleValue.string }),
        new ValueProperty({ name: "text", type: SimpleValue.string }),
        new ValueProperty({ name: "created_at", type: SimpleValue.string }),
        new ValueProperty({ name: "author_name", type: SimpleValue.string }),
        new ValueProperty({ name: "role", type: SimpleValue.string }),
        new ValueProperty({ name: "author_ref", type: SimpleValue.string }),
      ],
    }),
  ],
});

const threadListSchema = new MeshSchema({
  rootTagName: "thread_list",
  elements: [
    new ElementType({
      tagName: "thread_list",
      description: "an index of MeshAgent chat threads",
      properties: [
        new ChildProperty({ name: "threads", childTagNames: ["thread"] }),
      ],
    }),
    new ElementType({
      tagName: "thread",
      properties: [
        new ValueProperty({ name: "name", type: SimpleValue.string }),
        new ValueProperty({ name: "path", type: SimpleValue.string }),
        new ValueProperty({ name: "created_at", type: SimpleValue.string }),
        new ValueProperty({ name: "modified_at", type: SimpleValue.string }),
      ],
    }),
  ],
});

const chatbotActionSchema = {
  type: "object",
  additionalProperties: false,
  required: [
    "action",
    "reply",
    "storagePath",
    "payloadJson",
    "roomToolkit",
    "roomTool",
    "roomToolArgumentsJson",
  ],
  properties: {
    action: {
      type: "string",
      enum: [
        "reply",
        "write_room_storage",
        "summarize_room_storage",
        "invoke_room_tool",
      ],
    },
    reply: { type: "string" },
    storagePath: { type: "string" },
    payloadJson: { type: "string" },
    roomToolkit: { type: "string" },
    roomTool: { type: "string" },
    roomToolArgumentsJson: { type: "string" },
  },
};

const chatbotReplySchema = {
  type: "object",
  additionalProperties: false,
  required: ["reply"],
  properties: {
    reply: { type: "string" },
  },
};

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function normalizeMessage(message) {
  const text = String(message ?? "").trim();
  if (text === "") {
    return "Say something.";
  }
  return text;
}

function trimTrailingSlash(value) {
  let trimmed = value;
  while (trimmed.endsWith("/")) {
    trimmed = trimmed.slice(0, -1);
  }
  return trimmed;
}

function nowIso() {
  return new Date().toISOString();
}

function messageId() {
  return crypto.randomUUID();
}

function threadPathForSession(sessionId) {
  const key = String(sessionId ?? "").trim() || "default";
  return `${threadDir}/${encodeURIComponent(key)}.thread`;
}

function findChild(element, tagName) {
  return element.getChildren().find((child) => child.tagName === tagName) ?? null;
}

function findMessagesElement(document) {
  const messages = findChild(document.root, "messages");
  if (!messages) {
    throw new Error("thread document is missing messages element");
  }
  return messages;
}

function ensureThreadDocument(document) {
  let messages = findChild(document.root, "messages");
  if (!messages) {
    document.root.createChildElement("messages", {});
  }
  let members = findChild(document.root, "members");
  if (!members) {
    document.root.createChildElement("members", {});
  }
}

function threadMessagesFromDocument(document) {
  const messages = findMessagesElement(document);
  return messages
    .getChildren()
    .filter((child) => child.tagName === "message")
    .map((child) => ({
      id: child.getAttribute("id") ?? "",
      role: child.getAttribute("role") ?? "",
      content: child.getAttribute("text") ?? "",
      authorName: child.getAttribute("author_name") ?? "",
      createdAt: child.getAttribute("created_at") ?? "",
    }));
}

async function openThreadDocument(room, threadPath) {
  const document = await room.sync.open(threadPath, {
    create: true,
    schema: threadSchema,
  });
  ensureThreadDocument(document);
  return document;
}

async function readThreadMessages({ room, threadPath }) {
  const document = await openThreadDocument(room, threadPath);
  try {
    return threadMessagesFromDocument(document);
  } finally {
    await room.sync.close(threadPath);
  }
}

async function appendThreadMessages({ room, threadPath, messages }) {
  const document = await openThreadDocument(room, threadPath);
  try {
    const parent = findMessagesElement(document);
    for (const message of messages) {
      parent.createChildElement("message", {
        id: message.id ?? messageId(),
        turn_id: message.turnId ?? "",
        role: message.role,
        text: message.content,
        author_name: message.authorName,
        author_ref: message.authorRef ?? "",
        created_at: message.createdAt ?? nowIso(),
      });
    }
    return threadMessagesFromDocument(document);
  } finally {
    await room.sync.close(threadPath);
  }
}

async function upsertThreadIndex({ room, threadPath, name }) {
  const document = await room.sync.open(threadListPath, {
    create: true,
    schema: threadListSchema,
  });
  try {
    const timestamp = nowIso();
    let existing = null;
    for (const child of document.root.getChildren()) {
      if (child.tagName === "thread" && child.getAttribute("path") === threadPath) {
        existing = child;
        break;
      }
    }
    if (!existing) {
      document.root.createChildElement("thread", {
        name,
        path: threadPath,
        created_at: timestamp,
        modified_at: timestamp,
      });
      return;
    }
    existing.setAttribute("name", name);
    existing.setAttribute("modified_at", timestamp);
  } finally {
    await room.sync.close(threadListPath);
  }
}

function parseJsonObject(text, label) {
  const value = JSON.parse(text);
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} was not a JSON object.`);
  }
  return value;
}

function tryParseJsonObject(text) {
  try {
    return parseJsonObject(text, "thread message");
  } catch {
    return null;
  }
}

function extractResponseText(payload) {
  if (typeof payload.output_text === "string" && payload.output_text.trim() !== "") {
    return payload.output_text.trim();
  }

  const parts = [];
  for (const item of payload.output ?? []) {
    for (const content of item.content ?? []) {
      if (typeof content.text === "string") {
        parts.push(content.text);
      }
    }
  }
  return parts.join("").trim();
}

async function callMeshAgentLLM({ instructions, input, schemaName, schema }) {
  const baseURL = String(process.env.OPENAI_BASE_URL ?? "").trim();
  const apiKey = String(process.env.OPENAI_API_KEY ?? "").trim();
  if (!baseURL || !apiKey) {
    throw new Error(
      "MeshAgent LLM router environment is missing. Run this chatbot through `meshagent room connect` so OPENAI_BASE_URL and OPENAI_API_KEY are set."
    );
  }

  const response = await fetch(`${trimTrailingSlash(baseURL)}/responses`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${apiKey}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: process.env.MESHAGENT_CHATBOT_MODEL || "gpt-5.4",
      instructions,
      input,
      text: {
        format: {
          type: "json_schema",
          name: schemaName,
          schema,
          strict: true,
        },
      },
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`MeshAgent LLM request failed (${response.status}): ${body}`);
  }

  const payload = await response.json();
  const outputText = extractResponseText(payload);
  if (!outputText) {
    throw new Error("MeshAgent LLM response did not include output text.");
  }
  return parseJsonObject(outputText, "MeshAgent LLM response");
}

function chatbotControllerInstructions() {
  return [
    "You are a MeshAgent chatbot shim. Choose one action for the latest user message.",
    "The conversation history comes from a MeshAgent thread document. Use it as the source of truth.",
    "Return only JSON that matches the schema.",
    "Use action reply for normal chat and put the assistant response in reply.",
    "If the latest user asks what they just said, answer from the previous user message and start the reply with: Your previous message was:",
    "If the latest user asks to write JSON to a room storage file, use action write_room_storage, copy the storage path into storagePath, and copy the JSON object text into payloadJson.",
    "If the latest user asks to summarize the contents of that file and knownStoragePath is set, use action summarize_room_storage and copy knownStoragePath into storagePath.",
    "If the latest user asks to call or use a MeshAgent room tool, use action invoke_room_tool, set roomToolkit and roomTool, and put the JSON arguments in roomToolArgumentsJson.",
    "For fields that do not apply to the chosen action, use an empty string.",
  ].join("\n");
}

async function chooseChatbotAction({ threadPath, threadMessages, message, storagePath }) {
  return callMeshAgentLLM({
    instructions: chatbotControllerInstructions(),
    schemaName: "meshagent_chatbot_action",
    schema: chatbotActionSchema,
    input: JSON.stringify(
      {
        threadPath,
        knownStoragePath: storagePath || "",
        threadMessages,
        latestUserMessage: message,
      },
      null,
      2
    ),
  });
}

async function completeReplyWithLLM({ instruction, payload }) {
  const result = await callMeshAgentLLM({
    instructions: [
      instruction,
      "Return only JSON that matches the schema.",
    ].join("\n"),
    schemaName: "meshagent_chatbot_reply",
    schema: chatbotReplySchema,
    input: JSON.stringify(payload, null, 2),
  });
  return String(result.reply ?? "").trim();
}

async function writeJsonToRoomStorage({ room, storagePath, payload }) {
  const body = `${JSON.stringify(payload, null, 2)}\n`;
  await room.storage.upload(storagePath, new TextEncoder().encode(body), {
    overwrite: true,
    name: path.basename(storagePath),
    mimeType: "application/json",
  });
}

async function readJsonFromRoomStorage({ room, storagePath }) {
  const file = await room.storage.download(storagePath);
  return JSON.parse(new TextDecoder().decode(file.data));
}

function contentToPlainObject(content) {
  if (content && typeof content.json !== "undefined") {
    return content.json;
  }
  if (content && typeof content.text === "string") {
    return { text: content.text };
  }
  if (content === null || content === undefined) {
    return null;
  }
  return JSON.parse(JSON.stringify(content));
}

async function invokeRoomTool({ room, toolkit, tool, argumentsJson }) {
  const input = argumentsJson.trim() ? parseJsonObject(argumentsJson, "room tool arguments") : {};
  const content = await room.invoke({ toolkit, tool, input });
  return {
    toolkit,
    tool,
    arguments: input,
    result: contentToPlainObject(content),
  };
}

function roomToolMessage(toolCall) {
  return {
    role: "tool",
    authorName: "MeshAgent room tool",
    content: JSON.stringify({
      kind: "meshagent_room_tool_call",
      ...toolCall,
    }),
  };
}

function roomApiMessage(apiCall) {
  return {
    role: "event",
    authorName: "MeshAgent room API",
    content: JSON.stringify({
      kind: "meshagent_room_api_call",
      ...apiCall,
    }),
  };
}

function storageApiMessage({ api, storagePath, result }) {
  return roomApiMessage({
    api,
    arguments: { path: storagePath },
    result,
  });
}

function roomToolCallsFromMessages(messages) {
  const calls = [];
  for (const message of messages) {
    if (message.role !== "tool") {
      continue;
    }
    const parsed = tryParseJsonObject(message.content);
    if (parsed?.kind === "meshagent_room_tool_call") {
      calls.push({
        toolkit: parsed.toolkit,
        tool: parsed.tool,
        arguments: parsed.arguments ?? {},
        result: parsed.result ?? null,
      });
    }
  }
  return calls;
}

function roomApiCallsFromMessages(messages) {
  const calls = [];
  for (const message of messages) {
    const parsed = tryParseJsonObject(message.content);
    if (parsed?.kind === "meshagent_room_api_call") {
      calls.push({
        api: parsed.api,
        arguments: parsed.arguments ?? {},
        result: parsed.result ?? null,
      });
    }
  }
  return calls;
}

function visibleChatMessages(messages) {
  return messages.filter((message) => message.role !== "tool" && message.role !== "event");
}

function latestStoragePathFromMessages(messages) {
  for (const message of [...messages].reverse()) {
    const parsed = tryParseJsonObject(message.content);
    const storagePath = parsed?.arguments?.path ?? parsed?.storagePath;
    if (typeof storagePath === "string" && storagePath.trim() !== "") {
      return storagePath.trim();
    }
  }
  return "";
}

function buildStorageResult({ probe, storagePath }) {
  return {
    probe,
    result: "room-storage-chatbot-result",
    status: "written",
    storagePath,
    source: "typescript-chatbot",
  };
}

function summaryContainsStoredFacts(summary, payload) {
  const text = String(summary ?? "");
  return [payload.result, payload.probe, payload.status].every((value) =>
    text.includes(String(value))
  );
}

async function chatbotReply({ room, sessionId, message }) {
  const text = normalizeMessage(message);
  const threadPath = threadPathForSession(sessionId);
  await upsertThreadIndex({ room, threadPath, name: String(sessionId ?? "default") });
  const previousMessages = await readThreadMessages({ room, threadPath });
  const plan = await chooseChatbotAction({
    threadPath,
    threadMessages: previousMessages,
    message: text,
    storagePath: latestStoragePathFromMessages(previousMessages),
  });

  const pendingMessages = [
    {
      role: "user",
      authorName: "user",
      content: text,
    },
  ];
  let reply = String(plan.reply ?? "").trim();
  let storagePath = String(plan.storagePath ?? "").trim();

  if (plan.action === "write_room_storage") {
    if (!storagePath) {
      throw new Error("MeshAgent LLM did not choose a room storage path.");
    }
    const storedPayload = parseJsonObject(plan.payloadJson, "room storage payloadJson");
    await writeJsonToRoomStorage({ room, storagePath, payload: storedPayload });
    pendingMessages.push(storageApiMessage({
      api: "storage.upload",
      storagePath,
      result: { ok: true, bytes: JSON.stringify(storedPayload, null, 2).length + 1 },
    }));
    if (!reply) {
      reply = `Wrote room storage file ${storagePath}.`;
    }
  } else if (plan.action === "summarize_room_storage") {
    storagePath = storagePath || latestStoragePathFromMessages(previousMessages);
    if (!storagePath) {
      reply = "I do not have a room storage file to summarize yet.";
    } else {
      const storedPayload = await readJsonFromRoomStorage({ room, storagePath });
      pendingMessages.push(storageApiMessage({
        api: "storage.download",
        storagePath,
        result: storedPayload,
      }));
      reply = await completeReplyWithLLM({
        instruction: [
          "You summarize room storage JSON for a MeshAgent chatbot.",
          "If the JSON has result, probe, and status fields, set reply exactly to:",
          "Room storage summary: <result> for probe <probe> has status <status>.",
          "Otherwise, write one short sentence that summarizes the JSON contents.",
        ].join("\n"),
        payload: {
          userMessage: text,
          storagePath,
          storedPayload,
        },
      });
    }
  } else if (plan.action === "invoke_room_tool") {
    const toolkit = String(plan.roomToolkit ?? "").trim();
    const tool = String(plan.roomTool ?? "").trim();
    if (!toolkit || !tool) {
      throw new Error("MeshAgent LLM did not choose a room toolkit and tool.");
    }
    const toolCall = await invokeRoomTool({
      room,
      toolkit,
      tool,
      argumentsJson: String(plan.roomToolArgumentsJson ?? ""),
    });
    pendingMessages.push(roomToolMessage(toolCall));
    reply = await completeReplyWithLLM({
      instruction: "Write one short assistant reply that summarizes the MeshAgent room tool result.",
      payload: {
        userMessage: text,
        toolCall,
      },
    });
  } else if (!reply) {
    reply = "I am ready to chat.";
  }

  pendingMessages.push({
    role: "assistant",
    authorName: "typescript-chatbot",
    content: reply,
  });

  const threadMessages = await appendThreadMessages({
    room,
    threadPath,
    messages: pendingMessages,
  });

  return {
    sessionId: String(sessionId ?? "").trim() || "default",
    threadPath,
    threadListPath,
    reply,
    storagePath,
    messages: threadMessages,
    roomToolCalls: roomToolCallsFromMessages(threadMessages),
    roomApiCalls: roomApiCallsFromMessages(threadMessages),
  };
}

async function writeChatbotProof({ probe, sessionId, threadPath, threadMessages }) {
  const payload = {
    probe,
    sessionId,
    threadPath,
    threadListPath,
    messages: visibleChatMessages(threadMessages),
    threadMessages,
    roomToolCalls: roomToolCallsFromMessages(threadMessages),
    roomApiCalls: roomApiCallsFromMessages(threadMessages),
    tools: ["chat"],
  };
  await fs.promises.mkdir(path.dirname(proofPath), { recursive: true });
  await fs.promises.writeFile(proofPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return payload;
}

async function writeChatbotStorageProof({ probe, sessionId, threadPath, storagePath, storedPayload, storageSummary, threadMessages }) {
  const payload = {
    probe,
    sessionId,
    threadPath,
    threadListPath,
    storagePath,
    storedPayload,
    storageSummary,
    messages: visibleChatMessages(threadMessages),
    threadMessages,
    roomToolCalls: roomToolCallsFromMessages(threadMessages),
    roomApiCalls: roomApiCallsFromMessages(threadMessages),
    tools: ["chat"],
  };
  await fs.promises.mkdir(path.dirname(storageProofPath), { recursive: true });
  await fs.promises.writeFile(storageProofPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return payload;
}

class TypeScriptChatTool extends Tool {
  constructor({ room }) {
    super({
      name: "chat",
      title: "Chat with MeshAgent LLM",
      description: "Routes chat through the room LLM router, stores history in a MeshAgent thread, and can call room tools.",
      inputSchema: {
        type: "object",
        required: ["sessionId", "message"],
        additionalProperties: false,
        properties: {
          sessionId: { type: "string" },
          message: { type: "string" },
        },
      },
    });
    this.room = room;
  }

  async execute({ sessionId, message }) {
    return new JsonContent({ json: await chatbotReply({ room: this.room, sessionId, message }) });
  }
}

class TypeScriptChatbotToolkit extends Toolkit {
  constructor({ room }) {
    super({
      name: toolkitName,
      title: "TypeScript Local Chatbot Toolkit",
      description: "Local chatbot backed by MeshAgent LLM, MeshAgent threads, and MeshAgent room tools.",
      tools: [new TypeScriptChatTool({ room })],
    });
  }
}

async function runDevTranscriptProof(room, probe) {
  const sessionId = `meshagent-create-chat-${probe}`;
  const threadPath = threadPathForSession(sessionId);
  const firstMessage = `MeshAgent local dev proof ${probe}`;
  const firstTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: firstMessage },
  });
  console.log(`MeshAgent create dev chatbot turn 1: ${JSON.stringify(firstTurn.json)}`);
  if (firstTurn.json?.threadPath !== threadPath || !String(firstTurn.json?.reply ?? "").trim()) {
    throw new Error("Local TypeScript chatbot proof did not write the first turn to a MeshAgent thread.");
  }

  const secondMessage = "What did I just say?";
  const secondTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: secondMessage },
  });
  console.log(`MeshAgent create dev chatbot turn 2: ${JSON.stringify(secondTurn.json)}`);
  if (!String(secondTurn.json?.reply ?? "").includes(firstMessage)) {
    throw new Error("Local TypeScript chatbot proof did not preserve chat history in the MeshAgent thread.");
  }

  const toolMessage = `Call MeshAgent room tool storage.stat with JSON ${JSON.stringify({ path: threadPath })}`;
  const toolTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: toolMessage },
  });
  console.log(`MeshAgent create dev chatbot room tool: ${JSON.stringify(toolTurn.json)}`);
  const roomToolCalls = toolTurn.json?.roomToolCalls ?? [];
  if (!roomToolCalls.some((call) => call.toolkit === "storage" && call.tool === "stat")) {
    throw new Error("Local TypeScript chatbot proof did not call a MeshAgent room tool.");
  }

  const threadMessages = await readThreadMessages({ room, threadPath });
  await writeChatbotProof({ probe, sessionId, threadPath, threadMessages });
  console.log(`MeshAgent create dev chatbot proof wrote: ${proofDisplayPath} ${probe}`);
}

async function runDevRoomStorageProof(room, probe) {
  const sessionId = `meshagent-create-storage-${probe}`;
  const threadPath = threadPathForSession(sessionId);
  const storagePath = `.meshagent-create/chatbot-result-${probe}.json`;
  const storedPayload = buildStorageResult({ probe, storagePath });
  const writeMessage = `Write result to room storage file ${storagePath} with JSON ${JSON.stringify(storedPayload)}`;
  const writeTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: writeMessage },
  });
  console.log(`MeshAgent create dev chatbot storage write: ${JSON.stringify(writeTurn.json)}`);
  if (writeTurn.json?.threadPath !== threadPath || writeTurn.json?.storagePath !== storagePath) {
    throw new Error("Local TypeScript chatbot proof did not write storage state to the MeshAgent thread.");
  }

  const summaryMessage = "Summarize the contents of that file.";
  const summaryTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: summaryMessage },
  });
  console.log(`MeshAgent create dev chatbot storage summary: ${JSON.stringify(summaryTurn.json)}`);

  const actualStoredPayload = await readJsonFromRoomStorage({ room, storagePath });
  if (JSON.stringify(actualStoredPayload) !== JSON.stringify(storedPayload)) {
    throw new Error("Local TypeScript chatbot proof did not write the expected room storage payload.");
  }
  if (!summaryContainsStoredFacts(summaryTurn.json?.reply, actualStoredPayload)) {
    throw new Error("Local TypeScript chatbot proof did not summarize the room storage file.");
  }

  const toolMessage = `Call MeshAgent room tool storage.stat with JSON ${JSON.stringify({ path: storagePath })}`;
  const toolTurn = await room.agents.invokeTool({
    toolkit: toolkitName,
    tool: "chat",
    arguments: { sessionId, message: toolMessage },
  });
  console.log(`MeshAgent create dev chatbot storage room tool: ${JSON.stringify(toolTurn.json)}`);
  const roomToolCalls = toolTurn.json?.roomToolCalls ?? [];
  if (!roomToolCalls.some((call) => call.toolkit === "storage" && call.tool === "stat")) {
    throw new Error("Local TypeScript chatbot proof did not call a MeshAgent room tool.");
  }

  const threadMessages = await readThreadMessages({ room, threadPath });
  await writeChatbotStorageProof({
    probe,
    sessionId,
    threadPath,
    storagePath,
    storedPayload: actualStoredPayload,
    storageSummary: summaryTurn.json?.reply,
    threadMessages,
  });
  console.log(`MeshAgent create dev chatbot storage proof wrote: ${storageProofDisplayPath} ${probe}`);
}

async function runDevChatbotToolkit(existingRoom) {
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
    toolkit: new TypeScriptChatbotToolkit({ room }),
    public_: true,
  });

  try {
    if (!probe) {
      await new Promise(() => {});
      return;
    }

    if (process.env.MESHAGENT_CREATE_CHATBOT_PROOF_MODE === "room-storage") {
      await runDevRoomStorageProof(room, probe);
    } else {
      await runDevTranscriptProof(room, probe);
    }
    const holdSeconds = Number.parseFloat(process.env.MESHAGENT_CREATE_DEV_TOOLKIT_HOLD_SECONDS ?? "0");
    if (Number.isFinite(holdSeconds) && holdSeconds > 0) {
      console.log(`MeshAgent create dev chatbot holding registration for ${holdSeconds}s`);
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
    await runDevChatbotToolkit(room);
  } finally {
    room.dispose();
  }
}

main().catch((error) => {
  console.error("Unable to start MeshAgent RoomClient chatbot:", error);
  process.exitCode = 1;
});
