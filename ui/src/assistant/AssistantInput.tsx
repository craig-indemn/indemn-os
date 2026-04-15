import { useRef, useState, useEffect } from "react";
import { useAssistant } from "./useAssistant";

export function AssistantInput() {
  const [input, setInput] = useState("");
  const { sendMessage, isOpen, togglePanel } = useAssistant();
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSubmit = () => {
    if (input.trim()) {
      sendMessage(input.trim());
      setInput("");
      if (!isOpen) togglePanel();
    }
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = document.activeElement?.tagName;
      if (
        e.key === "/" &&
        !e.ctrlKey &&
        !e.metaKey &&
        tag !== "INPUT" &&
        tag !== "TEXTAREA"
      ) {
        e.preventDefault();
        inputRef.current?.focus();
      }
      if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  return (
    <div className="flex-1 max-w-xl mx-auto">
      <input
        ref={inputRef}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
        placeholder="Ask or tell me to do something..."
        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 text-sm"
      />
    </div>
  );
}
