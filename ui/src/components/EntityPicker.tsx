import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";

interface Props {
  entityType: string;
  value?: string;
  onChange: (value: string) => void;
  name: string;
}

/** Searchable dropdown for relationship fields (objectid type). */
export function EntityPicker({ entityType, value, onChange, name }: Props) {
  const [search, setSearch] = useState("");
  const [options, setOptions] = useState<{ _id: string; label: string }[]>([]);
  const [open, setOpen] = useState(false);
  const [displayValue, setDisplayValue] = useState("");

  const slug = entityType.toLowerCase() + "s";

  // Resolve current value to display name
  const { data: resolved } = useQuery({
    queryKey: ["resolved-name", entityType, value],
    queryFn: () => apiClient<Record<string, unknown>>(`/api/${slug}/${value}`),
    staleTime: 5 * 60 * 1000,
    enabled: !!value && value.length >= 12 && !displayValue,
  });

  useEffect(() => {
    if (resolved && !displayValue) {
      setDisplayValue(
        String(resolved.name || resolved.email || resolved.title || "")
      );
    }
  }, [resolved, displayValue]);

  // Search for options when dropdown is open
  useEffect(() => {
    if (!entityType || !open) return;
    const timer = setTimeout(async () => {
      try {
        const results = await apiClient<Record<string, unknown>[]>(
          `/api/${slug}/?limit=15${search ? `&search=${encodeURIComponent(search)}` : ""}`
        );
        setOptions(
          results.map((r) => ({
            _id: String(r._id),
            label: String(r.name || r.email || r.title || r._id),
          }))
        );
      } catch {
        setOptions([]);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [entityType, search, open, slug]);

  return (
    <div className="relative">
      <input
        type="text"
        name={name}
        value={open ? search : displayValue || (value ? value.slice(-8) + "…" : "")}
        onChange={(e) => {
          setSearch(e.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          setSearch("");
          setOpen(true);
        }}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
        placeholder={`Select ${entityType}...`}
        className="w-full px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400"
      />
      {value && !open && (
        <button
          type="button"
          onClick={() => {
            onChange("");
            setDisplayValue("");
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-xs"
        >
          ✕
        </button>
      )}
      {open && options.length > 0 && (
        <ul className="absolute z-10 w-full mt-1 bg-white border rounded shadow-lg max-h-48 overflow-y-auto">
          {options.map((opt) => (
            <li
              key={opt._id}
              onMouseDown={() => {
                onChange(opt._id);
                setDisplayValue(opt.label);
                setOpen(false);
              }}
              className={`px-3 py-2 text-sm hover:bg-blue-50 cursor-pointer ${
                opt._id === value ? "bg-blue-50 font-medium" : ""
              }`}
            >
              {opt.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
