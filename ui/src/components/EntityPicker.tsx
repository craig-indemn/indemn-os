import { useState, useEffect } from "react";
import { apiClient } from "../api/client";

interface Props {
  entityType: string;
  value?: string;
  onChange: (value: string) => void;
  name: string;
}

/** Searchable dropdown for relationship fields (objectid type). [G-32] */
export function EntityPicker({ entityType, value, onChange, name }: Props) {
  const [search, setSearch] = useState("");
  const [options, setOptions] = useState<{ _id: string; label: string }[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!entityType || !open) return;
    const timer = setTimeout(async () => {
      try {
        const slug = entityType.toLowerCase() + "s";
        const results = await apiClient<Record<string, unknown>[]>(
          `/api/${slug}?limit=10${search ? `&search=${encodeURIComponent(search)}` : ""}`
        );
        setOptions(
          results.map((r) => ({
            _id: String(r._id),
            label: String(r.name || r.email || r._id).slice(0, 40),
          }))
        );
      } catch {
        setOptions([]);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [entityType, search, open]);

  return (
    <div className="relative">
      <input
        type="text"
        name={name}
        value={search || (value ? String(value).slice(-8) : "")}
        onChange={(e) => {
          setSearch(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
        placeholder={`Select ${entityType}...`}
        className="w-full px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400 font-mono"
      />
      {open && options.length > 0 && (
        <ul className="absolute z-10 w-full mt-1 bg-white border rounded shadow-lg max-h-48 overflow-y-auto">
          {options.map((opt) => (
            <li
              key={opt._id}
              onMouseDown={() => {
                onChange(opt._id);
                setSearch(opt.label);
                setOpen(false);
              }}
              className="px-3 py-2 text-sm hover:bg-blue-50 cursor-pointer"
            >
              <span className="font-mono text-xs text-gray-400 mr-2">
                {opt._id.slice(-6)}
              </span>
              {opt.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
