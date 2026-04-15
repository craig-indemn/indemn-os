import { useEffect, useRef } from "react";
import { useAssistant } from "./useAssistant";

export function AssistantPanel() {
  const { messages, isOpen, togglePanel, isStreaming } = useAssistant();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ESC key closes panel [G-59]
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") togglePanel();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, togglePanel]);

  if (!isOpen) return null;

  return (
    <div className="fixed right-0 top-0 h-full w-[450px] bg-white shadow-xl border-l z-50 flex flex-col">
      <div className="flex justify-between items-center p-4 border-b">
        <h2 className="font-semibold">Assistant</h2>
        <button
          onClick={togglePanel}
          className="text-gray-400 hover:text-gray-600 text-sm"
        >
          ESC
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map((msg) => (
          <div key={msg.id} className={msg.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block p-3 rounded-lg max-w-[85%] text-sm whitespace-pre-wrap ${
                msg.role === "user"
                  ? "bg-blue-100 text-blue-900"
                  : "bg-gray-50 text-gray-800"
              }`}
            >
              {msg.content}
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
    </div>
  );
}
