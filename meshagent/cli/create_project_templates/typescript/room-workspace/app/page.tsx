"use client";

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { ArrowLeft, FileIcon, FolderIcon, MessageSquare, RefreshCw, SquareTerminal, UsersRound } from "lucide-react";
import { RoomClient, type RemoteParticipant, type StorageEntry } from "@meshagent/meshagent";
import { useRoomParticipants } from "@meshagent/meshagent-react";
import { DeveloperConsole } from "@meshagent/meshagent-react-dev";
import {
  ChatBotView,
  FilePreview,
  MeetingScope,
  MeetingView,
  participantDisplayName,
  participantSupportsChat,
} from "@meshagent/meshagent-tailwind";

type ActiveTab = "chat" | "meeting" | "files" | "console";
type ConnectionState = "connecting" | "connected" | "disconnected";

const defaultChatPath = ".threads/main.thread";
const rootFolder = "";

function getDefaultShellImage(): string {
  return "meshagent/python:default";
}

function joinPaths(...parts: string[]): string {
  return parts
    .flatMap((part) => part.split("/"))
    .map((part) => part.trim())
    .filter(Boolean)
    .join("/");
}

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function formatSize(size: number | null): string {
  if (size === null) {
    return "";
  }

  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${unitIndex === 0 ? value : value.toFixed(1)} ${units[unitIndex]}`;
}

function formatModified(date: Date | null): string {
  if (date === null) {
    return "";
  }

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function chatAgentName(participants: RemoteParticipant[]): string | undefined {
  const participant = participants.find(
    (candidate) => candidate.role === "agent" && participantSupportsChat(candidate),
  );
  return participant === undefined ? undefined : participantDisplayName(participant) ?? undefined;
}

function connectionLabel(state: ConnectionState, error: string | null): string {
  if (error !== null) {
    return error;
  }
  if (state === "connected") {
    return "Connected to this room";
  }
  if (state === "disconnected") {
    return "Disconnected";
  }
  return "Connecting to this room";
}

function TabButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className={active ? "tab active" : "tab"} onClick={onClick} type="button">
      {children}
    </button>
  );
}

function FileBrowser({ room }: { room: RoomClient }) {
  const [folder, setFolder] = useState(rootFolder);
  const [openedFile, setOpenedFile] = useState<string | null>(null);
  const [entries, setEntries] = useState<StorageEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    room.storage
      .list(folder)
      .then((nextEntries) => {
        if (!cancelled) {
          setEntries(
            nextEntries
              .filter((entry) => !entry.name.startsWith("."))
              .sort((left, right) => {
                if (left.isFolder !== right.isFolder) {
                  return left.isFolder ? -1 : 1;
                }
                return left.name.localeCompare(right.name);
              }),
          );
        }
      })
      .catch((nextError: unknown) => {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : String(nextError));
          setEntries([]);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [folder, refreshToken, room]);

  if (openedFile !== null) {
    return (
      <div className="file-preview">
        <div className="file-toolbar">
          <button className="icon-button" onClick={() => setOpenedFile(null)} title="Back to files" type="button">
            <ArrowLeft size={18} />
          </button>
          <div className="file-path">{openedFile}</div>
        </div>
        <div className="preview-content">
          <FilePreview room={room} path={openedFile} />
        </div>
      </div>
    );
  }

  return (
    <div className="file-browser">
      <div className="file-toolbar">
        <button
          className="icon-button"
          disabled={folder === rootFolder}
          onClick={() => setFolder(parentPath(folder))}
          title="Parent folder"
          type="button"
        >
          <ArrowLeft size={18} />
        </button>
        <div className="file-path">/{folder}</div>
        <button className="icon-button" onClick={() => setRefreshToken((value) => value + 1)} title="Refresh" type="button">
          <RefreshCw size={18} />
        </button>
      </div>
      <div className="file-body">
        {loading ? (
          <div className="center-message">Loading files</div>
        ) : error !== null ? (
          <div className="center-message">{error}</div>
        ) : entries.length === 0 ? (
          <div className="center-message">No files in this folder</div>
        ) : (
          <table className="file-list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Size</th>
                <th>Modified</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const path = joinPaths(folder, entry.name);
                return (
                  <tr key={path}>
                    <td>
                      <button
                        className="file-row"
                        onClick={() => entry.isFolder ? setFolder(path) : setOpenedFile(path)}
                        type="button"
                      >
                        <span className="file-name">
                          {entry.isFolder ? <FolderIcon size={18} /> : <FileIcon size={18} />}
                          {entry.name}
                        </span>
                      </button>
                    </td>
                    <td>{formatSize(entry.size)}</td>
                    <td>{formatModified(entry.updatedAt)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function MeetingWorkspace({ room }: { room: RoomClient }) {
  const [activeTab, setActiveTab] = useState<ActiveTab>("chat");
  const participants = useRoomParticipants(room);
  const agentName = useMemo(() => chatAgentName(participants), [participants]);

  return (
    <MeetingScope client={room}>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand">
            <h1>Meeting App</h1>
            <div className="status">{participants.length} participant{participants.length === 1 ? "" : "s"}</div>
          </div>
          <nav className="tabs" aria-label="Room views">
            <TabButton active={activeTab === "chat"} onClick={() => setActiveTab("chat")}>
              <MessageSquare size={17} /> Chat
            </TabButton>
            <TabButton active={activeTab === "meeting"} onClick={() => setActiveTab("meeting")}>
              <UsersRound size={17} /> Meeting
            </TabButton>
            <TabButton active={activeTab === "files"} onClick={() => setActiveTab("files")}>
              <FileIcon size={17} /> Files
            </TabButton>
            <TabButton active={activeTab === "console"} onClick={() => setActiveTab("console")}>
              <SquareTerminal size={17} /> Console
            </TabButton>
          </nav>
        </header>

        <main className="content">
          <section className="panel">
            {activeTab === "chat" ? (
              <div className="chat-layout">
                <div className="chat-pane">
                  <ChatBotView room={room} path={agentName === undefined ? defaultChatPath : undefined} participants={participants} agentName={agentName} />
                </div>
                <div className="file-pane">
                  <FileBrowser room={room} />
                </div>
              </div>
            ) : activeTab === "meeting" ? (
              <div className="meeting-pane">
                <MeetingView onCancel={() => setActiveTab("chat")} />
              </div>
            ) : activeTab === "console" ? (
              <DeveloperConsole room={room} shellImage={getDefaultShellImage()} />
            ) : (
              <FileBrowser room={room} />
            )}
          </section>
        </main>
      </div>
    </MeetingScope>
  );
}

export default function Home() {
  const [room, setRoom] = useState<RoomClient | null>(null);
  const [state, setState] = useState<ConnectionState>("connecting");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    const nextRoom = RoomClient.withIAP();

    setRoom(nextRoom);
    nextRoom
      .start()
      .then(() => {
        nextRoom.messaging.enable();
        if (!stopped) {
          setState("connected");
        }
      })
      .catch((nextError: unknown) => {
        if (!stopped) {
          setState("disconnected");
          setError(nextError instanceof Error ? nextError.message : String(nextError));
        }
      });

    return () => {
      stopped = true;
      nextRoom.dispose();
    };
  }, []);

  if (room === null || state !== "connected") {
    return (
      <main className="app-shell">
        <header className="topbar">
          <div className="brand">
            <h1>Meeting App</h1>
            <div className="status">{connectionLabel(state, error)}</div>
          </div>
        </header>
        <div className="center-message">{connectionLabel(state, error)}</div>
      </main>
    );
  }

  return <MeetingWorkspace room={room} />;
}
