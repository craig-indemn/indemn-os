import { useState, useEffect, useRef, type RefObject } from "react";
import { useLocation } from "react-router-dom";
import Markdown from "react-markdown";
import { useAssistant } from "./useAssistant";
import { CompactEntityTable } from "./CompactEntityTable";
import { EntityCard } from "./EntityCard";
import { CollapsibleToolCall } from "./CollapsibleToolCall";
import { ConversationHistory } from "./ConversationHistory";
import type { AssistantMessage } from "./useAssistant";

interface Props {
  width: number;
  inputRef: RefObject<HTMLInputElement | null>;
  onClose: () => void;
}

export function AssistantPanel({ width, inputRef, onClose }: Props) {
  const { messages, isStreaming, clearMessages, sendMessage, loadConversation, interactionId } = useAssistant();
  const bottomRef = useRef<HTMLDivElement>(null);
  const [input, setInput] = useState("");
  const location = useLocation();
  const parts = location.pathname.split("/").filter(Boolean);
  const contextLabel = parts.length >= 2 && parts[1] !== "new"
    ? `Viewing: ${parts[0]} detail`
    : parts.length >= 2 && parts[1] === "new"
      ? `Viewing: New ${parts[0]}`
      : parts.length === 1 && parts[0] !== "queue"
        ? `Viewing: ${parts[0]} list`
        : "Viewing: Queue";

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ESC key closes panel
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      style={{ width }}
      className="h-full bg-white border-l flex flex-col flex-shrink-0"
    >
      <div className="flex justify-between items-center p-4 border-b">
        <h2 className="font-semibold">Assistant</h2>
        <div className="flex items-center gap-2">
          <ConversationHistory
            currentInteractionId={interactionId}
            onSelect={loadConversation}
          />
          {messages.length > 0 && (
            <button
              onClick={clearMessages}
              className="text-gray-400 hover:text-gray-600 text-xs px-2 py-1 border rounded"
            >
              New Conversation
            </button>
          )}
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-sm"
          >
            ESC
          </button>
        </div>
      </div>
      <div className="text-xs text-gray-400 px-3 py-1 border-b">{contextLabel}</div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map((msg) => (
          <MessageBubble key={msg.id} msg={msg} />
        ))}
        {isStreaming && (
          <div className="flex items-center gap-2 text-gray-400 text-sm">
            <span className="animate-pulse">Thinking...</span>
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      <div className="border-t p-3">
        <div className="flex gap-2">
          <input
            ref={inputRef as React.RefObject<HTMLInputElement>}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && input.trim() && !isStreaming) {
                sendMessage(input.trim());
                setInput("");
              }
            }}
            placeholder="Type a message..."
            disabled={isStreaming}
            className="flex-1 px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400 disabled:opacity-50"
            autoFocus
          />
          <button
            onClick={() => {
              if (input.trim() && !isStreaming) {
                sendMessage(input.trim());
                setInput("");
              }
            }}
            disabled={!input.trim() || isStreaming}
            className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-30"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

/** Try to parse message content as entity data (JSON with _id fields). */
function tryDetectEntityData(
  content: string
): { type: "list" | "detail"; data: unknown } | null {
  if (!content || content.length < 10) return null;
  const trimmed = content.trim();

  // Must start with [ or { to be JSON
  if (trimmed[0] !== "[" && trimmed[0] !== "{") return null;

  try {
    const data = JSON.parse(trimmed);

    // Entity list: array of objects with _id
    if (
      Array.isArray(data) &&
      data.length > 0 &&
      typeof data[0] === "object" &&
      data[0] !== null &&
      "_id" in data[0]
    ) {
      return { type: "list", data };
    }

    // Single entity: object with _id
    if (
      typeof data === "object" &&
      data !== null &&
      !Array.isArray(data) &&
      "_id" in data
    ) {
      return { type: "detail", data };
    }
  } catch {
    // Not valid JSON — that's fine, render as text
  }

  return null;
}

function MessageBubble({ msg }: { msg: AssistantMessage }) {
  // User messages always get the blue bubble
  if (msg.role === "user") {
    return (
      <div className="text-right">
        <div className="inline-block p-3 rounded-lg max-w-[85%] text-sm bg-blue-100 text-blue-900 whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    );
  }

  // Assistant messages: switch on messageType
  switch (msg.messageType) {
    case "entity_list":
      return (
        <div className="max-w-[95%]">
          <CompactEntityTable
            data={msg.entityData as Record<string, unknown>[]}
            entityType={msg.entityType || ""}
          />
        </div>
      );

    case "entity_detail":
      return (
        <div className="max-w-[85%]">
          <EntityCard
            data={msg.entityData as Record<string, unknown>}
            entityType={msg.entityType || ""}
          />
        </div>
      );

    case "tool_call":
      return (
        <div className="max-w-[85%]">
          <CollapsibleToolCall
            name={msg.toolName || ""}
            args={msg.toolArgs || {}}
          />
        </div>
      );

    case "tool_result":
    default: {
      // Try to detect entity data in content (JSON with _id fields)
      const detected = tryDetectEntityData(msg.content);
      if (detected?.type === "list") {
        return (
          <div className="max-w-[95%]">
            <CompactEntityTable data={detected.data as Record<string, unknown>[]} entityType="" />
          </div>
        );
      }
      if (detected?.type === "detail") {
        return (
          <div className="max-w-[85%]">
            <EntityCard data={detected.data as Record<string, unknown>} entityType="" />
          </div>
        );
      }

      if (msg.messageType === "tool_result") {
        return (
          <div className="max-w-[85%]">
            <pre className="text-xs text-gray-500 font-mono bg-gray-50 p-2 rounded overflow-x-auto">
              {msg.content}
            </pre>
          </div>
        );
      }

      if (msg.messageType === "divider") {
        return (
          <div className="text-xs text-gray-400 text-center py-2 border-t border-b">
            &mdash; {msg.content} &mdash;
          </div>
        );
      }

      // Standard text/markdown
      return (
        <div>
          <div className="inline-block p-3 rounded-lg max-w-[85%] text-sm bg-gray-50 text-gray-800 prose prose-sm prose-gray max-w-none">
            <Markdown>{msg.content}</Markdown>
          </div>
        </div>
      );
    }
  }
}
