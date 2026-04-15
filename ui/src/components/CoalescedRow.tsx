import { useState } from "react";
import { MessageRow } from "./MessageRow";
import type { QueueMessage } from "../api/types";

interface Props {
  correlationId: string;
  messages: QueueMessage[];
}

export function CoalescedRow({ correlationId, messages }: Props) {
  const [expanded, setExpanded] = useState(false);
  const representative = messages[0];

  if (messages.length === 1) {
    return <MessageRow message={representative} />;
  }

  return (
    <div className="border rounded">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between p-3 bg-white hover:bg-gray-50"
      >
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900">
            {representative.summary?.display || representative.entity_type}
          </span>
          <span className="px-1.5 py-0.5 bg-gray-100 rounded text-xs text-gray-600">
            {messages.length} events
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="border-t divide-y">
          {messages.map((msg) => (
            <div key={msg._id} className="p-2 pl-6">
              <MessageRow message={msg} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
