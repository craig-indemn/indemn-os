import type { QueueMessage } from "../api/types";

interface Props {
  message: QueueMessage;
  onClaim?: () => void;
}

export function MessageRow({ message, onClaim }: Props) {
  return (
    <div className="flex items-center justify-between p-3 bg-white border rounded hover:shadow-sm">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-gray-900 truncate">
            {message.summary?.display || `${message.entity_type}: ${message.event_type}`}
          </span>
          <span className="text-xs text-gray-400">{message.target_role}</span>
        </div>
        <div className="text-xs text-gray-500 mt-0.5">
          {new Date(message.created_at).toLocaleString()}
        </div>
      </div>
      {onClaim && (
        <button
          onClick={onClaim}
          className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Claim
        </button>
      )}
    </div>
  );
}
