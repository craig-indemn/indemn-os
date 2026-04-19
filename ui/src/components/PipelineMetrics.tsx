import { useStateDistribution, useQueueDepth } from "../api/hooks";

interface Props {
  entityName?: string;
  showQueueDepth?: boolean;
}

export function PipelineMetrics({ entityName, showQueueDepth = !entityName }: Props) {
  const { data: distribution } = useStateDistribution(entityName || "");

  return (
    <div className="space-y-4">
      {distribution && Object.keys(distribution).length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 mb-2">State Distribution</h3>
          <div className="grid grid-cols-2 gap-2">
            {Object.entries(distribution).map(([state, count]) => (
              <div key={state} className="flex justify-between bg-gray-50 rounded px-3 py-2">
                <span className="text-sm text-gray-600">{state}</span>
                <span className="text-sm font-medium">{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {showQueueDepth && <QueueDepthSection />}
    </div>
  );
}

function QueueDepthSection() {
  const { data: queueDepth } = useQueueDepth();

  if (!queueDepth || Object.keys(queueDepth).length === 0) return null;

  return (
    <div>
      <h3 className="text-sm font-medium text-gray-700 mb-2">Queue Depth</h3>
      <div className="grid grid-cols-2 gap-2">
        {Object.entries(queueDepth).map(([role, count]) => (
          <div key={role} className="flex justify-between bg-gray-50 rounded px-3 py-2">
            <span className="text-sm text-gray-600">{role}</span>
            <span className="text-sm font-medium">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
