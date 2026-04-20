import { createContext, useContext } from "react";

export interface AssistantMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  messageType?: "text" | "entity_list" | "entity_detail" | "tool_call" | "tool_result" | "divider";
  entityData?: Record<string, unknown>[] | Record<string, unknown>;
  entityType?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  callId?: string;
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
