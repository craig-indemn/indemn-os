import type { ChangeRecord } from "../api/types";

interface Props {
  changes: ChangeRecord[];
}

export function ChangeTimeline({ changes }: Props) {
  if (!changes.length) return <p className="text-sm text-gray-400">No changes</p>;

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-gray-700">Recent Changes</h3>
      <div className="space-y-1">
        {changes.map((change) => (
          <div key={change.id} className="text-xs border-l-2 border-gray-200 pl-3 py-1">
            <div className="text-gray-500">{new Date(change.timestamp).toLocaleString()}</div>
            <div className="text-gray-700 font-medium">{change.change_type}</div>
            {change.method && (
              <div className="text-gray-400">via {change.method}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
