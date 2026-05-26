"use client";

import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { RoomClient, type RemoteParticipant, type RoomMessageEvent } from "@meshagent/meshagent";

const CHAT_MESSAGE_TYPE = "meshagent.room-chat.message";

type ChatMessage = {
  id: string;
  direction: "incoming" | "outgoing";
  participantId: string;
  participantName: string;
  text: string;
  sentAt: string;
};

function makeId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function participantName(participant: RemoteParticipant) {
  const name = participant.getAttribute("name");
  if (typeof name === "string" && name.trim() !== "") {
    return name;
  }

  const displayName = participant.getAttribute("display_name");
  if (typeof displayName === "string" && displayName.trim() !== "") {
    return displayName;
  }

  return participant.id;
}

function messageText(message: Record<string, unknown>) {
  const text = message["text"];
  return typeof text === "string" ? text : "";
}

function remoteUserParticipants(participants: RemoteParticipant[]) {
  return participants.filter((participant) => participant.role === "user");
}

export default function Home() {
  const [status, setStatus] = useState("Connecting");
  const [participants, setParticipants] = useState<RemoteParticipant[]>([]);
  const [selectedParticipantId, setSelectedParticipantId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const roomRef = useRef<RoomClient | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);

  const userParticipants = useMemo(() => remoteUserParticipants(participants), [participants]);
  const selectedParticipant = useMemo(
    () => userParticipants.find((participant) => participant.id === selectedParticipantId) ?? null,
    [userParticipants, selectedParticipantId],
  );

  const canSend = selectedParticipant !== null && status === "Connected" && input.trim() !== "";

  useEffect(() => {
    const messagesElement = messagesRef.current;
    if (messagesElement === null) {
      return;
    }
    messagesElement.scrollTop = messagesElement.scrollHeight;
  }, [messages]);

  useEffect(() => {
    let stopped = false;
    let room: RoomClient | null = null;

    function refreshParticipants() {
      if (room === null) {
        return;
      }

      const nextParticipants = room.messaging.remoteParticipants;
      const nextUserParticipants = remoteUserParticipants(nextParticipants);
      setParticipants(nextParticipants);
      setSelectedParticipantId((current) => {
        if (current !== null && nextUserParticipants.some((participant) => participant.id === current)) {
          return current;
        }
        return nextUserParticipants[0]?.id ?? null;
      });
    }

    function handleMessage(event: RoomMessageEvent) {
      if (room === null) {
        return;
      }

      refreshParticipants();

      if (event.message.type !== CHAT_MESSAGE_TYPE) {
        return;
      }

      const text = messageText(event.message.message);
      if (text === "") {
        return;
      }

      const sender = room.messaging.getParticipant(event.message.fromParticipantId);
      const participantLabel = sender === null ? event.message.fromParticipantId : participantName(sender);
      setMessages((current) => [
        ...current,
        {
          id: makeId("incoming"),
          direction: "incoming",
          participantId: event.message.fromParticipantId,
          participantName: participantLabel,
          text,
          sentAt: new Date().toISOString(),
        },
      ]);
    }

    async function connect() {
      try {
        if (stopped) {
          return;
        }

        room = RoomClient.withIAP();
        roomRef.current = room;
        room.messaging.on("message", handleMessage);
        room.messaging.on("messaging_enabled", refreshParticipants);
        room.messaging.on("participant_added", refreshParticipants);
        room.messaging.on("participant_removed", refreshParticipants);
        room.messaging.on("participant_attributes_updated", refreshParticipants);

        await room.start();
        if (stopped) {
          return;
        }
        room.messaging.enable();
        setStatus("Connected");
      } catch (error) {
        console.error(error);
        if (!stopped) {
          setStatus("Disconnected");
        }
      }
    }

    void connect();

    return () => {
      stopped = true;
      if (room !== null) {
        room.messaging.off("message", handleMessage);
        room.messaging.off("messaging_enabled", refreshParticipants);
        room.messaging.off("participant_added", refreshParticipants);
        room.messaging.off("participant_removed", refreshParticipants);
        room.messaging.off("participant_attributes_updated", refreshParticipants);
        room.dispose();
      }
      if (roomRef.current === room) {
        roomRef.current = null;
      }
    };
  }, []);

  async function sendInputMessage() {
    const text = input.trim();
    const room = roomRef.current;
    if (room === null || selectedParticipant === null || text === "") {
      return;
    }

    setInput("");
    const sentAt = new Date().toISOString();
    setMessages((current) => [
      ...current,
      {
        id: makeId("outgoing"),
        direction: "outgoing",
        participantId: selectedParticipant.id,
        participantName: participantName(selectedParticipant),
        text,
        sentAt,
      },
    ]);

    try {
      await room.messaging.sendMessage({
        to: selectedParticipant,
        type: CHAT_MESSAGE_TYPE,
        message: { text, sent_at: sentAt },
      });
    } catch (error) {
      console.error(error);
      setStatus("Send failed");
    }
  }

  function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void sendInputMessage();
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    void sendInputMessage();
  }

  return (
    <main className="shell">
      <section className="workspace">
        <aside className="participants">
          <header>
            <p className="eyebrow">MeshAgent</p>
            <h1>Room Chat</h1>
            <div className={status === "Connected" ? "status online" : "status"}>
              <span />
              {status}
            </div>
          </header>

          <div className="participant-list" aria-label="Participants">
            {userParticipants.length === 0 ? (
              <p className="empty-participants">No remote participants</p>
            ) : (
              userParticipants.map((participant) => (
                <button
                  className={participant.id === selectedParticipantId ? "participant selected" : "participant"}
                  key={participant.id}
                  onClick={() => setSelectedParticipantId(participant.id)}
                  type="button"
                >
                  <span>{participantName(participant)}</span>
                  <small>{participant.role}</small>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="chat">
          <header className="chatbar">
            <div>
              <p className="eyebrow">Conversation</p>
              <h2>{selectedParticipant === null ? "Select a participant" : participantName(selectedParticipant)}</h2>
            </div>
          </header>

          <div className="messages" aria-live="polite" ref={messagesRef}>
            {messages.length === 0 ? (
              <div className="empty">
                <h3>No messages yet</h3>
              </div>
            ) : (
              <div className="message-stack">
                {messages.map((message) => (
                  <article className={`message ${message.direction}`} key={message.id}>
                    <div>
                      <p className="message-meta">{message.participantName}</p>
                      <div className="bubble">{message.text}</div>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>

          <form className="composer" onSubmit={submitMessage}>
            <textarea
              aria-label="Message"
              disabled={selectedParticipant === null}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder={selectedParticipant === null ? "Select a participant" : "Message"}
              rows={1}
              value={input}
            />
            <button disabled={!canSend} type="submit">
              Send
            </button>
          </form>
        </section>
      </section>
    </main>
  );
}
