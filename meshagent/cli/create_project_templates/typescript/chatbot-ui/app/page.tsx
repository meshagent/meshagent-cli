"use client";

import { decode, encode } from "@msgpack/msgpack";
import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

type ChatRole = "user" | "assistant";

type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
};

type AgentMessage = {
  type?: string;
  message_id?: string;
  thread_id?: string;
  turn_id?: string;
  source_message_id?: string;
  content?: Array<{ type?: string; text?: string }>;
  text?: string;
  status?: string | null;
  mode?: string | null;
  error?: { message?: string | null } | null;
};

const THREAD_START = "meshagent.agent.thread.start";
const TURN_START = "meshagent.agent.turn.start";
const THREAD_STARTED = "meshagent.agent.thread.started";
const TURN_STARTED = "meshagent.agent.turn.started";
const TURN_ENDED = "meshagent.agent.turn.ended";
const TURN_START_REJECTED = "meshagent.agent.turn.start.rejected";
const TEXT_DELTA = "meshagent.agent.text_content.delta";
const THREAD_STATUS = "meshagent.agent.thread.status";
const CONNECTION_STATUS = "meshagent.agent.connection.status";

function makeId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function messagesUrl() {
  const configured = process.env.NEXT_PUBLIC_MESHAGENT_MESSAGES_URL;
  if (configured !== undefined && configured.trim() !== "") {
    return configured;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/messages`;
}

function messageText(message: AgentMessage) {
  if (typeof message.text === "string") {
    return message.text;
  }
  if (!Array.isArray(message.content)) {
    return "";
  }
  return message.content
    .map((part) => (part.type === "text" && typeof part.text === "string" ? part.text : ""))
    .join("");
}

export default function Home() {
  const [socket, setSocket] = useState<WebSocket | null>(null);
  const [connectionStatus, setConnectionStatus] = useState("Connecting");
  const [turnStatus, setTurnStatus] = useState<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const pendingAssistantMessageId = useRef<string | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);

  const canSend = socket?.readyState === WebSocket.OPEN && activeTurnId === null && input.trim() !== "";
  const statusText = useMemo(() => {
    if (activeTurnId !== null) {
      return turnStatus ?? "Working";
    }
    return connectionStatus;
  }, [activeTurnId, connectionStatus, turnStatus]);

  useEffect(() => {
    const messagesElement = messagesRef.current;
    if (messagesElement === null) {
      return;
    }
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }, [messages]);

  useEffect(() => {
    let stopped = false;

    function connect() {
      if (stopped) {
        return;
      }

      const ws = new WebSocket(messagesUrl(), ["meshagent-msgpack"]);
      ws.binaryType = "arraybuffer";
      socketRef.current = ws;
      setSocket(ws);
      setConnectionStatus(reconnectAttemptRef.current === 0 ? "Connecting" : "Reconnecting");

      ws.addEventListener("open", () => {
        if (socketRef.current !== ws) {
          return;
        }
        reconnectAttemptRef.current = 0;
        setConnectionStatus("Connected");
      });

      ws.addEventListener("close", () => {
        if (socketRef.current !== ws) {
          return;
        }
        socketRef.current = null;
        setSocket(null);
        setActiveTurnId(null);
        setTurnStatus(null);
        if (stopped) {
          return;
        }

        reconnectAttemptRef.current += 1;
        const delay = Math.min(5000, 500 * reconnectAttemptRef.current);
        setConnectionStatus("Reconnecting");
        reconnectTimerRef.current = setTimeout(connect, delay);
      });

      ws.addEventListener("error", () => {
        if (socketRef.current === ws) {
          setConnectionStatus("Connection interrupted");
        }
      });

      ws.addEventListener("message", (event) => {
        if (!(event.data instanceof ArrayBuffer)) {
          return;
        }
        const decoded = decode(new Uint8Array(event.data)) as AgentMessage;
        handleAgentMessage(decoded);
      });
    }

    connect();

    return () => {
      stopped = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  function sendAgentMessage(message: AgentMessage) {
    const currentSocket = socketRef.current;
    if (currentSocket?.readyState !== WebSocket.OPEN) {
      setConnectionStatus("Disconnected");
      return;
    }
    currentSocket.send(encode(message));
  }

  function handleAgentMessage(message: AgentMessage) {
    if (message.type === CONNECTION_STATUS) {
      setConnectionStatus(message.status ?? "Connected");
      return;
    }

    if (message.type === THREAD_STARTED && typeof message.thread_id === "string") {
      setThreadId(message.thread_id);
      return;
    }

    if (message.type === TURN_STARTED && typeof message.turn_id === "string") {
      setActiveTurnId(message.turn_id);
      setTurnStatus("Working");
      return;
    }

    if (message.type === THREAD_STATUS) {
      setTurnStatus(message.status ?? null);
      return;
    }

    if (message.type === TEXT_DELTA) {
      appendAssistantText(messageText(message));
      return;
    }

    if (message.type === TURN_START_REJECTED) {
      appendAssistantText(message.error?.message ?? "The assistant rejected the turn.");
      setActiveTurnId(null);
      setTurnStatus(null);
      return;
    }

    if (message.type === TURN_ENDED) {
      if (message.error?.message) {
        appendAssistantText(message.error.message);
      }
      setActiveTurnId(null);
      setTurnStatus(null);
    }
  }

  function appendAssistantText(text: string) {
    if (text === "") {
      return;
    }

    const existingId = pendingAssistantMessageId.current;
    if (existingId !== null) {
      setMessages((current) =>
        current.map((message) =>
          message.id === existingId ? { ...message, text: `${message.text}${text}` } : message,
        ),
      );
      return;
    }

    const id = makeId("assistant");
    pendingAssistantMessageId.current = id;
    setMessages((current) => [...current, { id, role: "assistant", text }]);
  }

  function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    sendInputMessage();
  }

  function sendInputMessage() {
    const text = input.trim();
    if (!canSend || text === "") {
      return;
    }

    const content = [{ type: "text", text }];
    const messageId = makeId("message");
    pendingAssistantMessageId.current = null;
    setMessages((current) => [...current, { id: messageId, role: "user", text }]);
    setInput("");
    setTurnStatus("Starting");
    setActiveTurnId("pending");

    if (threadId === null) {
      const startThread = {
        type: THREAD_START,
        message_id: messageId,
        content,
        name: text.slice(0, 80),
      };
      sendAgentMessage(startThread);
      return;
    }

    sendAgentMessage({
      type: TURN_START,
      message_id: messageId,
      thread_id: threadId,
      content,
    });
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    sendInputMessage();
  }

  return (
    <main className="shell">
      <section className="chat">
        <header className="topbar">
          <div>
            <p className="eyebrow">MeshAgent</p>
            <h1>Chat</h1>
          </div>

          <div className={activeTurnId === null ? "status" : "status busy"}>
            <span />
            {statusText}
          </div>
        </header>

        <div className="messages" aria-live="polite" ref={messagesRef}>
          {messages.length === 0 ? (
            <div className="empty">
              <h2>Start a conversation</h2>
              <p>Ask the assistant to research, plan, write, or operate in the room.</p>
            </div>
          ) : (
            <div className="message-stack">
              {messages.map((message) => (
                <article className={`message ${message.role}`} key={message.id}>
                  <div className="bubble">{message.text}</div>
                </article>
              ))}
            </div>
          )}
        </div>

        <form className="composer" onSubmit={submitMessage}>
          <textarea
            aria-label="Message"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Message the assistant"
            rows={1} />

          <button disabled={!canSend} type="submit">Send</button>
        </form>
      </section>
    </main>
  );
}
