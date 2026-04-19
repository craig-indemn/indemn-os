import { useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import { useEntities, useEntityMeta } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { useRealtimeEntity } from "../hooks/useRealtime";
import { EntityTable } from "../components/EntityTable";
import { FieldRenderer } from "../components/FieldRenderer";
import { StateIndicator } from "../components/StateIndicator";
import type { ColumnDef } from "@tanstack/react-table";

const PAGE_SIZE = 50;

export function EntityListView() {
  const { entityType } = useParams<{ entityType: string }>();
  const entityName = useEntityNameFromSlug(entityType) || "";
  const navigate = useNavigate();
  const { data: meta } = useEntityMeta(entityName);

  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [page, setPage] = useState(0);

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

  // Limit columns for readability
  const visibleFields = meta.fields
    .filter(
      (f) =>
        !f.name.startsWith("_") &&
        f.name !== "org_id" &&
        f.name !== "version" &&
        !f.is_state_field
    )
    .slice(0, 6);

  for (const field of visibleFields) {
    columns.push({
      accessorKey: field.name,
      header: field.description || field.name.replace(/_/g, " "),
      cell: ({ getValue }) => (
        <FieldRenderer type={field.type} value={getValue()} meta={field} />
      ),
    });
  }

  const allStates = meta.state_machine ? Object.keys(meta.state_machine) : [];

  return (
    <div>
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

      <EntityTable
        columns={columns}
        data={entities || []}
        onRowClick={(row) => navigate(`/${entityType}/${row._id}`)}
      />

      {/* Pagination */}
      <div className="flex items-center justify-between mt-4 text-sm text-gray-500">
        <span>
          Showing {page * PAGE_SIZE + 1}–
          {page * PAGE_SIZE + (entities?.length || 0)}
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 border rounded disabled:opacity-30 hover:bg-gray-50"
          >
            ← Previous
          </button>
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
