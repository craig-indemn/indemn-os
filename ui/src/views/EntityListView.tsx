import { useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { useEntities, useEntityMeta } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { useRealtimeEntity } from "../hooks/useRealtime";
import { apiClient } from "../api/client";
import { EntityTable } from "../components/EntityTable";
import { FieldRenderer } from "../components/FieldRenderer";
import { StateIndicator } from "../components/StateIndicator";
import type { ColumnDef } from "@tanstack/react-table";

const PAGE_SIZE = 100;

export function EntityListView() {
  const { entityType } = useParams<{ entityType: string }>();
  const entityName = useEntityNameFromSlug(entityType) || "";
  const navigate = useNavigate();
  const { data: meta } = useEntityMeta(entityName);

  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [page, setPage] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const params: Record<string, string> = {
    limit: String(PAGE_SIZE),
    offset: String(page * PAGE_SIZE),
  };
  if (stateFilter) params.status = stateFilter;
  if (search) params.search = search;

  const { data: entities, isLoading, refetch } = useEntities(entityName, params);

  useRealtimeEntity(entityName);

  if (!meta || isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading...
      </div>
    );
  }

  const columns: ColumnDef<Record<string, unknown>>[] = [];

  // State badge as first column if state machine exists
  if (meta.state_machine) {
    columns.push({
      accessorKey: "status",
      header: "State",
      cell: ({ getValue }) => <StateIndicator state={String(getValue() || "")} />,
    });
  }

  // All fields as columns — sorted by priority, scrollable like a spreadsheet
  const priorityScore = (f: typeof meta.fields[0]) => {
    if (f.name === "name" || f.name === "title") return 0;
    if (f.type === "str" && f.enum_values?.length) return 1;
    if (f.name.includes("arr") || f.name.includes("score")) return 2;
    if (f.type === "str" && !f.is_relationship) return 3;
    if (f.type === "int" || f.type === "decimal" || f.type === "float") return 4;
    if (f.type === "date" || f.type === "datetime") return 5;
    if (f.is_relationship) return 6;
    if (f.type === "list") return 7;
    return 8;
  };

  const allFields = meta.fields
    .filter(
      (f) =>
        !f.name.startsWith("_") &&
        f.name !== "org_id" &&
        f.name !== "version" &&
        !f.is_state_field
    )
    .sort((a, b) => priorityScore(a) - priorityScore(b));

  for (const field of allFields) {
    columns.push({
      accessorKey: field.name,
      header: field.description || field.name.replace(/_/g, " "),
      cell: ({ getValue }) => (
        <FieldRenderer type={field.type} value={getValue()} meta={field} />
      ),
      meta: { fieldType: field.type, enumValues: field.enum_values },
      filterFn: field.enum_values?.length
        ? "equals"
        : field.type === "int" || field.type === "float" || field.type === "decimal"
          ? "inNumberRange" as const
          : "includesString",
    });
  }

  const allStates = meta.state_machine ? Object.keys(meta.state_machine) : [];
  const canBulk = !!meta.state_machine && meta.permissions.write;

  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">{entityName} List</h1>
        {meta.permissions.write && (
          <Link
            to={`/${entityType}/new`}
            className="px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm"
          >
            + New {entityName}
          </Link>
        )}
      </div>

      {/* Search + Filter bar */}
      <div className="flex gap-3 mb-4">
        <input
          type="text"
          placeholder={`Search ${entityName.toLowerCase()}s...`}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          className="flex-1 px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400"
        />
        {allStates.length > 0 && (
          <select
            value={stateFilter}
            onChange={(e) => {
              setStateFilter(e.target.value);
              setPage(0);
            }}
            className="px-3 py-1.5 border rounded text-sm"
          >
            <option value="">All states</option>
            {allStates.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        )}
      </div>

      {/* Bulk action bar */}
      {selectedIds.length > 0 && canBulk && (
        <div className="flex items-center gap-3 mb-3 p-3 bg-blue-50 rounded border border-blue-200">
          <span className="text-sm font-medium">{selectedIds.length} selected</span>
          {allStates.map((target) => (
            <button
              key={target}
              onClick={async () => {
                if (!window.confirm(`Transition ${selectedIds.length} items to "${target}"?`)) return;
                for (const id of selectedIds) {
                  try {
                    await apiClient(`/api/${entityType}/${id}/transition`, {
                      method: "POST", body: JSON.stringify({ to: target }),
                    });
                  } catch { /* skip */ }
                }
                setSelectedIds([]);
                refetch();
              }}
              className="px-2 py-1 text-xs border rounded hover:bg-white"
            >
              → {target}
            </button>
          ))}
          <button onClick={() => setSelectedIds([])} className="ml-auto text-xs text-gray-500">Clear</button>
        </div>
      )}

      <EntityTable
        columns={columns}
        data={entities || []}
        onRowClick={(row) => navigate(`/${entityType}/${row._id}`)}
        enableSelection={canBulk}
        onSelectionChange={setSelectedIds}
        storageKey={entityName}
      />

      {/* Pagination */}
      <div className="flex items-center justify-between mt-4 text-sm text-gray-500">
        <span>
          {(entities?.length || 0) > 0
            ? `Showing ${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + (entities?.length || 0)}`
            : "No results"}
        </span>
        <div className="flex gap-2 items-center">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 border rounded disabled:opacity-30 hover:bg-gray-50"
          >
            ← Previous
          </button>
          <span className="px-2 py-1 text-gray-400">Page {page + 1}</span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={(entities?.length || 0) < PAGE_SIZE}
            className="px-3 py-1 border rounded disabled:opacity-30 hover:bg-gray-50"
          >
            Next →
          </button>
        </div>
      </div>
    </div>
  );
}
