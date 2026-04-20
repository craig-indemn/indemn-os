import { useState, useEffect, useRef, type RefObject } from "react";
import { useLocation } from "react-router-dom";
import Markdown from "react-markdown";
import { useAssistant } from "./useAssistant";

interface Props {
  width: number;
  inputRef: RefObject<HTMLInputElement | null>;
  onClose: () => void;
}

export function AssistantPanel({ width, inputRef, onClose }: Props) {
  const { messages, isStreaming, clearMessages, sendMessage } = useAssistant();
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
          <div key={msg.id} className={msg.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block p-3 rounded-lg max-w-[85%] text-sm ${
                msg.role === "user"
                  ? "bg-blue-100 text-blue-900 whitespace-pre-wrap"
                  : "bg-gray-50 text-gray-800 prose prose-sm prose-gray max-w-none"
              }`}
            >
              {msg.role === "user" ? (
                msg.content
              ) : (
                <Markdown>{msg.content}</Markdown>
              )}
            </div>
          </div>
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
