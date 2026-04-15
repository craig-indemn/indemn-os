import { useState, useCallback, type ReactNode } from "react";
import { AssistantContext, type AssistantMessage } from "./useAssistant";
import { AssistantPanel } from "./AssistantPanel";
import { getToken } from "../api/client";

export function AssistantProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);

  const togglePanel = useCallback(() => setIsOpen((o) => !o), []);

  const sendMessage = useCallback(async (content: string) => {
    const userMsg: AssistantMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content,
    };
    setMessages((prev) => [...prev, userMsg]);
    setIsStreaming(true);

    try {
      const response = await fetch("/api/assistant/message", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify({ content, context: {} }),
      });

      const reader = response.body?.getReader();
      let assistantContent = "";
      const assistantId = crypto.randomUUID();

      while (reader) {
        const { done, value } = await reader.read();
        if (done) break;
        assistantContent += new TextDecoder().decode(value);
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant" && last.id === assistantId) {
            return updated.map((m) =>
              m.id === assistantId ? { ...m, content: assistantContent } : m
            );
          }
          return [
            ...updated,
            { id: assistantId, role: "assistant", content: assistantContent },
          ];
        });
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: "Error: could not reach assistant",
        },
      ]);
    } finally {
      setIsStreaming(false);
    }
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
