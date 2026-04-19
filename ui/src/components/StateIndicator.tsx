interface Props {
  state: string;
  entityName?: string;
  availableTransitions?: string[];
  onTransition?: (to: string, reason?: string) => void;
  canTransition?: boolean;
}

export function StateIndicator({ state, entityName, availableTransitions, onTransition, canTransition }: Props) {
  const colorMap: Record<string, string> = {
    active: "bg-green-100 text-green-700",
    customer: "bg-green-100 text-green-700",
    live: "bg-green-100 text-green-700",
    expanding: "bg-emerald-100 text-emerald-700",
    pending: "bg-yellow-100 text-yellow-700",
    prospect: "bg-yellow-100 text-yellow-700",
    open: "bg-yellow-100 text-yellow-700",
    planning: "bg-yellow-100 text-yellow-700",
    contact: "bg-yellow-100 text-yellow-700",
    processing: "bg-blue-100 text-blue-700",
    in_progress: "bg-blue-100 text-blue-700",
    building: "bg-blue-100 text-blue-700",
    pilot: "bg-blue-100 text-blue-700",
    testing: "bg-blue-100 text-blue-700",
    discovery: "bg-blue-100 text-blue-700",
    demo: "bg-indigo-100 text-indigo-700",
    proposal: "bg-indigo-100 text-indigo-700",
    negotiation: "bg-purple-100 text-purple-700",
    verbal: "bg-purple-100 text-purple-700",
    signed: "bg-green-100 text-green-700",
    completed: "bg-gray-100 text-gray-600",
    fulfilled: "bg-green-100 text-green-700",
    failed: "bg-red-100 text-red-700",
    churned: "bg-red-100 text-red-700",
    lost: "bg-red-100 text-red-700",
    missed: "bg-red-100 text-red-700",
    blocked: "bg-red-100 text-red-700",
    suspended: "bg-orange-100 text-orange-700",
    paused: "bg-orange-100 text-orange-700",
    parked: "bg-orange-100 text-orange-700",
    revoked: "bg-red-100 text-red-700",
    expired: "bg-gray-100 text-gray-500",
    provisioned: "bg-purple-100 text-purple-700",
    onboarding: "bg-blue-100 text-blue-700",
    cancelled: "bg-gray-100 text-gray-500",
    dismissed: "bg-gray-100 text-gray-500",
    retired: "bg-gray-100 text-gray-500",
    closed: "bg-gray-100 text-gray-500",
    draft: "bg-gray-100 text-gray-500",
  };

  const color = colorMap[state] || "bg-gray-100 text-gray-600";

  const handleTransition = (target: string) => {
    const label = entityName || "this entity";
    if (window.confirm(`Transition ${label} from "${state}" to "${target}"?`)) {
      onTransition?.(target);
    }
  };

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
              onClick={() => handleTransition(target)}
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
