import type { ChangeRecord } from "../api/types";

interface Props {
  changes: ChangeRecord[];
}

export function ChangeTimeline({ changes }: Props) {
  if (!changes.length) return <p className="text-sm text-gray-400">No changes</p>;

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-gray-700">Recent Changes</h3>
      <div className="space-y-2">
        {changes.map((change, i) => (
          <div key={change.id || i} className="text-xs border-l-2 border-gray-200 pl-3 py-1">
            <div className="text-gray-500">
              {change.timestamp ? new Date(change.timestamp).toLocaleString() : ""}
            </div>
            <div className="text-gray-700 font-medium">
              {change.change_type}
              {change.method && (
                <span className="text-gray-400 font-normal"> via {change.method}</span>
              )}
            </div>
            {change.changes?.map((fc, j) => (
              <div key={j} className="text-gray-500 ml-2">
                <span className="font-medium">{fc.field}</span>:{" "}
                <span className="text-red-400 line-through">
                  {fc.old != null ? String(fc.old) : "∅"}
                </span>{" "}
                →{" "}
                <span className="text-green-600">
                  {fc.new != null ? String(fc.new) : "∅"}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
