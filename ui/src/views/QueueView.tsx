import { useMemo } from "react";
import { useQueue } from "../api/hooks";
import { useRealtimeEntity } from "../hooks/useRealtime";
import { MessageRow } from "../components/MessageRow";
import { CoalescedRow } from "../components/CoalescedRow";
import { apiClient } from "../api/client";
import type { QueueMessage } from "../api/types";

export function QueueView() {
  const { data: messages, refetch } = useQueue();

  useRealtimeEntity("Message");

  const groups = useMemo(() => {
    if (!messages) return [];
    const byCorrelation: Record<string, QueueMessage[]> = {};
    for (const msg of messages) {
      const key = msg.correlation_id || msg._id;
      if (!byCorrelation[key]) byCorrelation[key] = [];
      byCorrelation[key].push(msg);
    }
    return Object.entries(byCorrelation).map(([corrId, msgs]) => ({
      correlation_id: corrId,
      messages: msgs,
    }));
  }, [messages]);

  const handleClaim = async (messageId: string) => {
    await apiClient(`/api/queue/messages/${messageId}/claim`, {
      method: "POST",
    });
    refetch();
  };

  return (
    <div>
      <h1 className="text-xl font-semibold mb-4">Queue</h1>
      <div className="space-y-2">
        {groups.map((group) =>
          group.messages.length === 1 ? (
            <MessageRow
              key={group.messages[0]._id}
              message={group.messages[0]}
              onClaim={() => handleClaim(group.messages[0]._id)}
            />
          ) : (
            <CoalescedRow
              key={group.correlation_id}
              correlationId={group.correlation_id}
              messages={group.messages}
            />
          )
        )}
        {groups.length === 0 && (
          <p className="text-gray-400 text-sm py-8 text-center">
            No pending messages
          </p>
        )}
      </div>
    </div>
  );
}
