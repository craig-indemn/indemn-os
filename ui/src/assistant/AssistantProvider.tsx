import { useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { AssistantContext, type AssistantMessage } from "./useAssistant";
import { AssistantPanel } from "./AssistantPanel";
import { getToken } from "../api/client";

// Chat harness WebSocket URL — resolves from env or derives from current host
const CHAT_HARNESS_URL =
  import.meta.env.VITE_CHAT_HARNESS_WS_URL ||
  (window.location.protocol === "https:" ? "wss:" : "ws:") +
    "//" +
    (window.location.host.includes("indemn-ui")
      ? window.location.host.replace("indemn-ui", "indemn-runtime-chat")
      : window.location.host) +
    "/ws/chat";

// Default associate — per-user CRM assistant. Set via env or API.
const DEFAULT_ASSOCIATE_ID = import.meta.env.VITE_DEFAULT_ASSOCIATE_ID || "";

export function AssistantProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const connectedRef = useRef(false);

  const togglePanel = useCallback(() => setIsOpen((o) => !o), []);

  // Connect to chat harness WebSocket
  const ensureConnected = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(CHAT_HARNESS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      // First message: connect with auth + associate identity
      ws.send(
        JSON.stringify({
          type: "connect",
          associate_id: DEFAULT_ASSOCIATE_ID,
          auth_token: getToken(), // User's JWT — assistant inherits user's permissions
        })
      );
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleHarnessMessage(data);
      } catch {
        // ignore malformed
      }
    };

    ws.onclose = () => {
      connectedRef.current = false;
    };
  }, []);

  // Handle typed JSON messages from the chat harness
  const handleHarnessMessage = useCallback((data: Record<string, unknown>) => {
    switch (data.type) {
      case "connected":
        connectedRef.current = true;
        break;

      case "response":
        // Agent text response — append to streaming message
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" && last.id === "streaming") {
            return prev.map((m) =>
              m.id === "streaming"
                ? { ...m, content: m.content + (data.content as string) }
                : m
            );
          }
          return [
            ...prev,
            { id: "streaming", role: "assistant", content: data.content as string },
          ];
        });
        break;

      case "tool_call":
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: `Running: ${data.name} ${JSON.stringify(data.args).slice(0, 200)}`,
          },
        ]);
        break;

      case "tool_result":
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: String(data.content || "").slice(0, 500),
          },
        ]);
        break;

      case "entity":
        // TODO: render as EntityTable component instead of JSON
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: JSON.stringify(data.data, null, 2),
          },
        ]);
        break;

      case "event":
        // Mid-conversation entity event
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: `Event: ${JSON.stringify(data.data).slice(0, 200)}`,
          },
        ]);
        break;

      case "done":
        // Finalize streaming message
        setMessages((prev) =>
          prev.map((m) =>
            m.id === "streaming" ? { ...m, id: crypto.randomUUID() } : m
          )
        );
        setIsStreaming(false);
        break;

      case "error":
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: `Error: ${data.content}`,
          },
        ]);
        setIsStreaming(false);
        break;
    }
  }, []);

  const sendMessage = useCallback(
    (content: string) => {
      const userMsg: AssistantMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content,
      };
      setMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);

      ensureConnected();

      // Send when connected (retry briefly if connecting)
      const trySend = (attempts = 0) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
          wsRef.current.send(
            JSON.stringify({
              type: "message",
              content,
              context: buildContext(),
            })
          );
        } else if (attempts < 30) {
          setTimeout(() => trySend(attempts + 1), 100);
        } else {
          setMessages((prev) => [
            ...prev,
            {
              id: crypto.randomUUID(),
              role: "assistant",
              content: "Error: could not connect to assistant",
            },
          ]);
          setIsStreaming(false);
        }
      };
      trySend();
    },
    [ensureConnected]
  );

  // Build context from current UI state [G-59]
  // Per base-ui-operational-surface: {view_type, current_entity, current_filter, role_focus}
  const buildContext = () => {
    const path = window.location.pathname;
    const parts = path.split("/").filter(Boolean);
    return {
      view_type: parts[0] || "queue",
      current_path: path,
      entity_type: parts.length >= 2 ? parts[0] : undefined,
      entity_id: parts.length >= 2 ? parts[1] : undefined,
    };
  };

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      wsRef.current?.close();
    };
  }, []);

  return (
    <AssistantContext.Provider
      value={{ messages, isOpen, isStreaming, togglePanel, sendMessage }}
    >
      {children}
      <AssistantPanel />
    </AssistantContext.Provider>
  );
}
