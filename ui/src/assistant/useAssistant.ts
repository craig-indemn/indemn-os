import { createContext, useContext } from "react";

export interface AssistantMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export interface AssistantContextType {
  messages: AssistantMessage[];
  isOpen: boolean;
  isStreaming: boolean;
  togglePanel: () => void;
  sendMessage: (content: string) => void;
  clearMessages: () => void;
}

export const AssistantContext = createContext<AssistantContextType>({
  messages: [],
  isOpen: false,
  isStreaming: false,
  togglePanel: () => {},
  sendMessage: () => {},
  clearMessages: () => {},
});

export function useAssistant() {
  return useContext(AssistantContext);
}
