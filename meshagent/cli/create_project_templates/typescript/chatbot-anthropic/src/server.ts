// @ts-nocheck
const http = require("node:http");

const port = Number(process.env.PORT || 3000);
const model = process.env.ANTHROPIC_MODEL || process.env.MESHAGENT_CHATBOT_MODEL || "claude-sonnet-4-6";
const defaultInstructions = "You are a concise assistant running through the MeshAgent room Anthropic proxy.";
const anthropicVersion = process.env.ANTHROPIC_VERSION || "2023-06-01";

function trimTrailingSlash(value) {
  return value.replace(/\/+$/, "");
}

function jsonResponse(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(`${JSON.stringify(payload)}\n`);
}

function textResponse(response, status, body) {
  response.writeHead(status, { "content-type": "text/plain; charset=utf-8" });
  response.end(body);
}

async function readJson(request) {
  const chunks = [];
  let totalBytes = 0;
  for await (const chunk of request) {
    totalBytes += chunk.length;
    if (totalBytes > 1_000_000) {
      throw new Error("Request body is too large.");
    }
    chunks.push(chunk);
  }

  const text = Buffer.concat(chunks).toString("utf8").trim();
  if (!text) {
    return {};
  }
  return JSON.parse(text);
}

function normalizeMessages(value) {
  if (!Array.isArray(value)) {
    throw new Error("messages must be an array.");
  }

  const messages = value.map((message) => {
    const role = String(message?.role ?? "").trim();
    const content = String(message?.content ?? "").trim();
    if (!["user", "assistant", "system"].includes(role) || !content) {
      throw new Error("Each message must have role user, assistant, or system and non-empty content.");
    }
    return { role, content };
  });

  if (messages.length === 0 || messages[messages.length - 1].role !== "user") {
    throw new Error("The latest message must be a user message.");
  }

  return messages;
}

function anthropicSystem(messages) {
  const extraInstructions = messages
    .filter((message) => message.role === "system")
    .map((message) => message.content);
  return [defaultInstructions, ...extraInstructions].join("\n\n");
}

function anthropicMessages(messages) {
  return messages
    .filter((message) => message.role !== "system")
    .map(({ role, content }) => ({ role, content }));
}

function extractMessageText(payload) {
  const parts = [];
  for (const content of payload?.content ?? []) {
    if (content?.type === "text" && typeof content?.text === "string") {
      parts.push(content.text);
    }
  }
  return parts.join("").trim();
}

async function completeChat(messages) {
  const baseURL = String(process.env.ANTHROPIC_BASE_URL ?? "").trim();
  const apiKey = String(process.env.ANTHROPIC_API_KEY ?? "").trim();
  if (!baseURL || !apiKey) {
    throw new Error(
      "ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY are required. Run locally with `meshagent room connect -- npm run dev` or use `npm run dev` from this sample."
    );
  }

  const response = await fetch(`${trimTrailingSlash(baseURL)}/v1/messages`, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": anthropicVersion,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model,
      max_tokens: 1024,
      system: anthropicSystem(messages),
      messages: anthropicMessages(messages),
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`MeshAgent Anthropic proxy request failed (${response.status}): ${body}`);
  }

  const payload = await response.json();
  const reply = extractMessageText(payload);
  if (!reply) {
    throw new Error("MeshAgent Anthropic proxy response did not include assistant text.");
  }
  return reply;
}

function html() {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MeshAgent Anthropic Chatbot</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #151922;
      --muted: #5e6878;
      --border: #d8dde8;
      --accent: #1668dc;
      --assistant: #eef4ff;
      --user: #eaf7ee;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1218;
        --panel: #171b24;
        --text: #eef2f8;
        --muted: #aab4c4;
        --border: #2d3442;
        --accent: #74a7ff;
        --assistant: #1b2a44;
        --user: #173525;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(880px, calc(100vw - 32px));
      min-height: 100vh;
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 16px;
      padding: 24px 0;
    }
    header h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
    }
    header p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    #messages {
      min-height: 360px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding: 16px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .message {
      max-width: 78%;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      white-space: pre-wrap;
      line-height: 1.45;
    }
    .assistant { align-self: flex-start; background: var(--assistant); }
    .user { align-self: flex-end; background: var(--user); }
    form {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
    }
    textarea {
      width: 100%;
      min-height: 52px;
      max-height: 160px;
      resize: vertical;
      padding: 12px;
      color: var(--text);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      font: inherit;
    }
    button {
      min-width: 96px;
      padding: 0 18px;
      color: white;
      background: var(--accent);
      border: 0;
      border-radius: 8px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled {
      cursor: wait;
      opacity: 0.65;
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>MeshAgent Anthropic Chatbot</h1>
      <p>Anthropic-compatible chat through the current MeshAgent room.</p>
    </header>
    <section id="messages" aria-live="polite"></section>
    <form id="chat-form">
      <textarea id="message-input" name="message" placeholder="Type a message" autocomplete="off"></textarea>
      <button id="send-button" type="submit">Send</button>
    </form>
  </main>
  <script>
    const form = document.querySelector("#chat-form");
    const input = document.querySelector("#message-input");
    const button = document.querySelector("#send-button");
    const messagesEl = document.querySelector("#messages");
    const messages = [];

    function renderMessage(role, content) {
      const node = document.createElement("div");
      node.className = "message " + role;
      node.textContent = content;
      messagesEl.appendChild(node);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    async function sendMessage(content) {
      messages.push({ role: "user", content });
      renderMessage("user", content);
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Chat request failed.");
      }
      messages.push({ role: "assistant", content: payload.reply });
      renderMessage("assistant", payload.reply);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const content = input.value.trim();
      if (!content) {
        return;
      }
      input.value = "";
      button.disabled = true;
      try {
        await sendMessage(content);
      } catch (error) {
        renderMessage("assistant", error.message || String(error));
      } finally {
        button.disabled = false;
        input.focus();
      }
    });
  </script>
</body>
</html>`;
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url || "/", `http://${request.headers.host || "localhost"}`);

    if (request.method === "GET" && url.pathname === "/health") {
      textResponse(response, 200, "ok\n");
      return;
    }

    if (request.method === "GET" && url.pathname === "/") {
      response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      response.end(html());
      return;
    }

    if (request.method === "POST" && url.pathname === "/api/chat") {
      const body = await readJson(request);
      const messages = normalizeMessages(body.messages);
      const reply = await completeChat(messages);
      jsonResponse(response, 200, { reply, model });
      return;
    }

    jsonResponse(response, 404, { error: "not found" });
  } catch (error) {
    console.error("Unable to handle chatbot request:", error);
    if (!response.headersSent) {
      jsonResponse(response, 500, { error: error.message || String(error) });
    } else {
      response.end();
    }
  }
});

server.listen(port, "0.0.0.0", () => {
  console.log(`MeshAgent TypeScript Anthropic chatbot listening on http://0.0.0.0:${port}`);
});
