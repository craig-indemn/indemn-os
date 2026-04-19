import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiClient } from "../api/client";
import type { FieldMeta } from "../api/types";

interface Props {
  type: string;
  value: unknown;
  meta?: FieldMeta;
}

function ResolvedLink({ entityType, entityId }: { entityType: string; entityId: string }) {
  const slug = entityType.toLowerCase() + "s";
  const { data } = useQuery({
    queryKey: ["resolved-name", entityType, entityId],
    queryFn: () => apiClient<Record<string, unknown>>(`/api/${slug}/${entityId}`),
    staleTime: 5 * 60 * 1000,
    enabled: !!entityId && entityId.length >= 12,
  });

  const displayName = data
    ? String(data.name || data.email || data.title || entityId.slice(-8))
    : entityId.slice(-8) + "…";

  return (
    <Link
      to={`/${slug}/${entityId}`}
      className="text-blue-600 hover:underline"
    >
      {displayName}
    </Link>
  );
}

export function FieldRenderer({ type, value, meta }: Props) {
  if (value === null || value === undefined) return <span className="text-gray-300">—</span>;

  switch (type) {
    case "str": return <span>{String(value)}</span>;
    case "int": return <span>{Number(value).toLocaleString()}</span>;
    case "float": return <span>{Number(value).toFixed(2)}</span>;
    case "decimal": return <span>{Number(value).toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>;
    case "bool": return <span className={value ? "text-green-600" : "text-gray-400"}>{value ? "Yes" : "No"}</span>;
    case "date": return <span>{new Date(String(value)).toLocaleDateString()}</span>;
    case "datetime": return <span>{new Date(String(value)).toLocaleString()}</span>;
    case "objectid":
      if (meta?.relationship_target && value) {
        return <ResolvedLink entityType={meta.relationship_target} entityId={String(value)} />;
      }
      return <span className="font-mono text-sm">{String(value)}</span>;
    case "list": return <span>{Array.isArray(value) ? value.join(", ") : ""}</span>;
    case "dict": return <pre className="text-xs bg-gray-50 p-1 rounded">{JSON.stringify(value, null, 2)}</pre>;
    case "enum": return <span className="px-2 py-0.5 bg-gray-100 rounded text-sm">{String(value)}</span>;
    default: return <span>{String(value)}</span>;
  }
}
