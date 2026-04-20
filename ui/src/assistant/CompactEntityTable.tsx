import { Link } from "react-router-dom";
import { StateIndicator } from "../components/StateIndicator";

interface Props {
  data: Record<string, unknown>[];
  entityType: string;
  maxRows?: number;
}

export function CompactEntityTable({ data, entityType, maxRows = 10 }: Props) {
  const slug = entityType ? entityType.toLowerCase() + "s" : "";
  const rows = data.slice(0, maxRows);

  // Auto-detect key fields from first row
  const firstRow = data[0] || {};
  const nameField = "name" in firstRow ? "name" : "title" in firstRow ? "title" : null;
  const stateField = "status" in firstRow ? "status" : "stage" in firstRow ? "stage" : null;

  // Pick 2-3 extra fields (strings/numbers, not IDs or system fields)
  const skipFields = new Set(
    ["_id", "org_id", "version", "created_at", "updated_at", "created_by", nameField, stateField].filter(Boolean) as string[]
  );
  const extraFields = Object.keys(firstRow)
    .filter((k) => !skipFields.has(k) && !k.endsWith("_id") && !k.startsWith("_"))
    .filter((k) => {
      const v = firstRow[k];
      return typeof v === "string" || typeof v === "number";
    })
    .slice(0, 3);

  return (
    <div className="border rounded-lg overflow-hidden text-sm">
      <table className="w-full divide-y divide-gray-200">
        <thead className="bg-gray-50">
          <tr>
            {nameField && <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">{nameField}</th>}
            {stateField && <th className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">State</th>}
            {extraFields.map((f) => (
              <th key={f} className="px-3 py-1.5 text-left text-xs font-medium text-gray-500">{f.replace(/_/g, " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((row) => (
            <tr key={String(row._id || crypto.randomUUID())} className="hover:bg-gray-50">
              {nameField && (
                <td className="px-3 py-1.5">
                  <Link to={`/${slug}/${row._id}`} className="text-blue-600 hover:underline">
                    {String(row[nameField] || "")}
                  </Link>
                </td>
              )}
              {stateField && (
                <td className="px-3 py-1.5">
                  <StateIndicator state={String(row[stateField] || "")} />
                </td>
              )}
              {extraFields.map((f) => (
                <td key={f} className="px-3 py-1.5 text-gray-600">{String(row[f] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.length > maxRows && (
        <div className="px-3 py-2 text-xs text-center border-t bg-gray-50">
          <Link to={`/${slug}`} className="text-blue-600 hover:underline">
            Show all ({data.length})
          </Link>
        </div>
      )}
    </div>
  );
}
