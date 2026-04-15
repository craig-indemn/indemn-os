interface Props {
  state: string;
  availableTransitions?: string[];
  onTransition?: (to: string, reason?: string) => void;
  canTransition?: boolean;
}

export function StateIndicator({ state, availableTransitions, onTransition, canTransition }: Props) {
  const colorMap: Record<string, string> = {
    active: "bg-green-100 text-green-700",
    pending: "bg-yellow-100 text-yellow-700",
    processing: "bg-blue-100 text-blue-700",
    completed: "bg-gray-100 text-gray-600",
    failed: "bg-red-100 text-red-700",
    suspended: "bg-orange-100 text-orange-700",
    revoked: "bg-red-100 text-red-700",
    expired: "bg-gray-100 text-gray-500",
    provisioned: "bg-purple-100 text-purple-700",
    onboarding: "bg-blue-100 text-blue-700",
    abandoned: "bg-red-50 text-red-600",
    closed: "bg-gray-100 text-gray-500",
  };

  const color = colorMap[state] || "bg-gray-100 text-gray-600";

  return (
    <div className="space-y-2">
      <span className={`px-2.5 py-1 rounded-full text-xs font-medium ${color}`}>
        {state}
      </span>
      {canTransition && availableTransitions && availableTransitions.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {availableTransitions.map((target) => (
            <button
              key={target}
              onClick={() => onTransition?.(target)}
              className="px-2 py-0.5 text-xs border rounded hover:bg-gray-50"
            >
              → {target}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
