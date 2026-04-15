import type { FieldMeta } from "../api/types";

interface Props {
  type: string;
  value: unknown;
  meta?: FieldMeta;
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
      if (meta?.relationship_target) {
        return <a href={`/${meta.relationship_target.toLowerCase()}s/${value}`} className="text-blue-600 underline">{String(value).slice(-8)}</a>;
      }
      return <span className="font-mono text-sm">{String(value).slice(-8)}</span>;
    case "list": return <span>{Array.isArray(value) ? value.join(", ") : ""}</span>;
    case "dict": return <pre className="text-xs bg-gray-50 p-1 rounded max-w-xs truncate">{JSON.stringify(value)}</pre>;
    case "enum": return <span className="px-2 py-0.5 bg-gray-100 rounded text-sm">{String(value)}</span>;
    default: return <span>{String(value)}</span>;
  }
}
