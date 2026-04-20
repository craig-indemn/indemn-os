import { Link } from "react-router-dom";
import { StateIndicator } from "../components/StateIndicator";

interface Props {
  data: Record<string, unknown>;
  entityType: string;
}

export function EntityCard({ data, entityType }: Props) {
  const slug = entityType ? entityType.toLowerCase() + "s" : "";
  const id = String(data._id || "");
  const nameField = "name" in data ? "name" : "title" in data ? "title" : null;
  const stateField = "status" in data ? "status" : "stage" in data ? "stage" : null;
  const displayName = nameField ? String(data[nameField]) : id.slice(-8);

  // Pick key display fields
  const skipFields = new Set(
    ["_id", "org_id", "version", "created_at", "updated_at", "created_by", nameField, stateField].filter(Boolean) as string[]
  );
  const displayFields = Object.entries(data)
    .filter(([k, v]) => !skipFields.has(k) && !k.endsWith("_id") && !k.startsWith("_") && v != null && v !== "" && !Array.isArray(v) && typeof v !== "object")
    .slice(0, 5);

  return (
    <div className="border rounded-lg p-3 bg-white text-sm">
      <div className="flex items-center justify-between mb-2">
        <Link to={`/${slug}/${id}`} className="text-blue-600 hover:underline font-medium">
          {displayName}
        </Link>
        {stateField && <StateIndicator state={String(data[stateField] || "")} />}
      </div>
      {entityType && (
        <div className="text-xs text-gray-400 mb-2">{entityType}</div>
      )}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        {displayFields.map(([k, v]) => (
          <div key={k}>
            <span className="text-gray-400 text-xs">{k.replace(/_/g, " ")}: </span>
            <span className="text-gray-700 text-xs">{String(v)}</span>
          </div>
        ))}
      </div>
      <div className="mt-2 pt-2 border-t">
        <Link to={`/${slug}/${id}`} className="text-xs text-blue-600 hover:underline">
          View details →
        </Link>
      </div>
    </div>
  );
}
