import { useNavigate, useParams } from "react-router-dom";
import { useEntities, useEntityMeta } from "../api/hooks";
import { useRealtimeEntity } from "../hooks/useRealtime";
import { apiClient } from "../api/client";
import { EntityTable } from "../components/EntityTable";
import { FieldRenderer } from "../components/FieldRenderer";
import { StateIndicator } from "../components/StateIndicator";
import type { ColumnDef } from "@tanstack/react-table";

export function EntityListView() {
  const { entityType } = useParams<{ entityType: string }>();
  const entityName = entityType
    ? entityType.replace(/s$/, "").replace(/^./, (c) => c.toUpperCase())
    : "";
  const navigate = useNavigate();
  const { data: meta } = useEntityMeta(entityName);
  const { data: entities, isLoading, refetch } = useEntities(entityName);

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
      header: "Status",
      cell: ({ getValue }) => <StateIndicator state={String(getValue() || "")} />,
    });
  }

  // Limit columns for readability
  const visibleFields = meta.fields
    .filter(
      (f) =>
        !f.name.startsWith("_") &&
        f.name !== "org_id" &&
        f.name !== "version"
    )
    .slice(0, 8);

  for (const field of visibleFields) {
    columns.push({
      accessorKey: field.name,
      header: field.description || field.name.replace(/_/g, " "),
      cell: ({ getValue }) => (
        <FieldRenderer type={field.type} value={getValue()} meta={field} />
      ),
    });
  }

  // Row actions column from permissions + state machine + capabilities
  const hasActions =
    (meta.permissions.write && meta.state_machine) ||
    (meta.capabilities?.length ?? 0) > 0 ||
    (meta.exposed_methods?.length ?? 0) > 0;

  if (hasActions) {
    columns.push({
      id: "actions",
      header: "Actions",
      cell: ({ row }) => {
        const entity = row.original;
        const currentState = String(entity.status || entity.stage || "");
        const transitions = meta.state_machine?.[currentState] || [];

        return (
          <div className="flex flex-wrap gap-1">
            {meta.permissions.write &&
              transitions.map((target) => (
                <button
                  key={target}
                  onClick={async (e) => {
                    e.stopPropagation();
                    await apiClient(
                      `/api/${entityType}/${entity._id}/transition`,
                      { method: "POST", body: JSON.stringify({ to: target }) }
                    );
                    refetch();
                  }}
                  className="px-1.5 py-0.5 text-xs border rounded hover:bg-gray-50"
                >
                  {"\u2192"} {target}
                </button>
              ))}
            {meta.capabilities?.map((cap) => (
              <button
                key={cap.name}
                onClick={async (e) => {
                  e.stopPropagation();
                  await apiClient(
                    `/api/${entityType}/${entity._id}/${cap.name.replace(/_/g, "-")}?auto=true`,
                    { method: "POST", body: "{}" }
                  );
                  refetch();
                }}
                className="px-1.5 py-0.5 text-xs border rounded hover:bg-blue-50 text-blue-600"
              >
                {cap.name.replace(/_/g, " ")}
              </button>
            ))}
            {meta.exposed_methods?.map((method) => (
              <button
                key={method.name}
                onClick={async (e) => {
                  e.stopPropagation();
                  await apiClient(
                    `/api/${entityType}/${entity._id}/${method.name.replace(/_/g, "-")}`,
                    { method: "POST", body: "{}" }
                  );
                  refetch();
                }}
                className="px-1.5 py-0.5 text-xs border rounded hover:bg-green-50 text-green-600"
              >
                {method.name.replace(/_/g, " ")}
              </button>
            ))}
          </div>
        );
      },
    });
  }

  return (
    <div>
      <h1 className="text-xl font-semibold mb-4">{entityName} List</h1>
      <EntityTable
        columns={columns}
        data={entities || []}
        onRowClick={(row) => navigate(`/${entityType}/${row._id}`)}
      />
    </div>
  );
}
