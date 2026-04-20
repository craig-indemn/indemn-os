import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";

interface Props {
  currentInteractionId: string | null;
  onSelect: (interactionId: string, createdAt: string) => void;
}

export function ConversationHistory({ currentInteractionId, onSelect }: Props) {
  const [open, setOpen] = useState(false);

  const { data: conversations } = useQuery({
    queryKey: ["conversations"],
    queryFn: () =>
      apiClient<Array<Record<string, unknown>>>(
        "/api/interactions/?limit=20&sort=-created_at"
      ),
    enabled: open,
    staleTime: 30000,
  });

  // Filter to chat interactions only and exclude current
  const items = (conversations || []).filter(
    (c) => c._id !== currentInteractionId && c.channel_type === "chat"
  );

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-gray-400 hover:text-gray-600 text-xs px-2 py-1 border rounded"
        title="Conversation history"
      >
        History
      </button>
      {open && (
        <div className="absolute right-0 top-8 z-30 bg-white border rounded-lg shadow-lg w-72 max-h-64 overflow-y-auto">
          {items.length === 0 ? (
            <div className="p-3 text-xs text-gray-400 text-center">
              No previous conversations
            </div>
          ) : (
            items.map((conv) => {
              const preview = String(
                conv.first_message_preview || "Untitled conversation"
              );
              const date = conv.created_at
                ? new Date(String(conv.created_at)).toLocaleDateString()
                : "";
              return (
                <button
                  key={String(conv._id)}
                  onClick={() => {
                    onSelect(String(conv._id), String(conv.created_at || ""));
                    setOpen(false);
                  }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 border-b last:border-b-0"
                >
                  <div className="text-gray-700 truncate">{preview}</div>
                  <div className="text-xs text-gray-400">{date}</div>
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
