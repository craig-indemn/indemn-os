import { useState, useCallback, useRef, useEffect, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AssistantContext, type AssistantMessage } from "./useAssistant";
import { AssistantPanel } from "./AssistantPanel";
import { getToken } from "../api/client";
import { useAuth } from "../auth/useAuth";

// Chat harness WebSocket URL — resolves from env or derives from current host
const CHAT_HARNESS_URL =
  import.meta.env.VITE_CHAT_HARNESS_WS_URL ||
  (window.location.protocol === "https:" ? "wss:" : "ws:") +
    "//" +
    (window.location.host.includes("indemn-ui")
      ? window.location.host.replace("indemn-ui", "indemn-runtime-chat")
      : window.location.host) +
    "/ws/chat";

// Default associate — per-user CRM assistant. Set via env or discovered from API.
const STATIC_ASSOCIATE_ID = import.meta.env.VITE_DEFAULT_ASSOCIATE_ID || "";

const STORAGE_KEY = "indemn_assistant_messages";

export function AssistantProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [messages, setMessages] = useState<AssistantMessage[]>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [isOpen, setIsOpen] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const connectedRef = useRef(false);
  const { isAuthenticated } = useAuth();
  const [associateId, setAssociateId] = useState(STATIC_ASSOCIATE_ID);

  // Discover default assistant from API after login
  useEffect(() => {
    if (associateId) return; // already resolved
    if (!isAuthenticated) return; // not logged in yet
    import("../api/client").then(({ apiClient: api }) => {
      api<Array<Record<string, unknown>>>("/api/actors/?limit=100")
        .then((actors) => {
          const assistant = actors.find(
            (a) =>
              a.type === "associate" &&
              a.status === "active" &&
              a.mode === "reasoning" &&
              a.runtime_id != null &&
              (a.name as string)?.toLowerCase().includes("assistant")
          );
          if (assistant) setAssociateId(String(assistant._id || assistant.id));
        })
        .catch(() => {});
    });
  }, [isAuthenticated, associateId]);

  const togglePanel = useCallback(() => setIsOpen((o) => !o), []);

  // Persist messages to localStorage [P-12]
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
  }, [messages]);

  const clearMessages = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY);
    setMessages([]);
  }, []);

  // Connect to chat harness WebSocket
  const ensureConnected = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    if (!associateId) {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: "Error: No assistant configured for this organization. Create an associate actor named 'OS Assistant' with mode=reasoning and a runtime_id.",
        },
      ]);
      setIsStreaming(false);
      return;
    }

    const ws = new WebSocket(CHAT_HARNESS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      // First message: connect with auth + associate identity
      ws.send(
        JSON.stringify({
          type: "connect",
          associate_id: associateId,
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
  }, [associateId]);

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
  // Gives the assistant awareness of what the user is looking at.
  const buildContext = () => {
    const path = window.location.pathname;
    const parts = path.split("/").filter(Boolean);
    const entitySlug = parts[0];
    const entityId = parts.length >= 2 ? parts[1] : undefined;

    const context: Record<string, unknown> = {
      current_path: path,
      entity_type_slug: entitySlug || "queue",
    };

    if (entitySlug && entitySlug !== "queue" && entitySlug !== "roles" && entitySlug !== "observability") {
      context.view_type = entityId ? "detail" : "list";

      // Inject entity metadata (fields, state machine) from cache
      const metaCache = queryClient.getQueriesData<Record<string, unknown>>({
        queryKey: ["entity-meta-detail"],
      });
      const metaMatch = metaCache.find(([key]) => {
        if (!Array.isArray(key)) return false;
        const name = String(key[1] || "").toLowerCase();
        return entitySlug.startsWith(name);
      });
      if (metaMatch?.[1]) {
        const meta = metaMatch[1] as Record<string, unknown>;
        context.entity_name = meta.name;
        context.entity_fields = (meta.fields as Array<Record<string, unknown>>)?.map(
          (f) => `${f.name} (${f.type}${f.enum_values ? `, enum: ${(f.enum_values as string[]).join("/")}` : ""})`
        );
        context.entity_states = meta.state_machine ? Object.keys(meta.state_machine as Record<string, unknown>) : undefined;
      }

      // Inject full entity data on detail views
      if (entityId) {
        context.entity_id = entityId;
        const cached = queryClient.getQueriesData<Record<string, unknown>>({
          queryKey: ["entity"],
        });
        const match = cached.find(
          ([key]) => Array.isArray(key) && key[2] === entityId
        );
        if (match?.[1]) {
          context.entity_data = match[1];
        }
      }
    }

    return context;
  };

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      wsRef.current?.close();
    };
  }, []);

  return (
    <AssistantContext.Provider
      value={{ messages, isOpen, isStreaming, togglePanel, sendMessage, clearMessages }}
    >
      {children}
      <AssistantPanel />
    </AssistantContext.Provider>
  );
}
