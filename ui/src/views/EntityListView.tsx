import { useNavigate, useParams } from "react-router-dom";
import { useEntities, useEntityMeta } from "../api/hooks";
import { useRealtimeEntity } from "../hooks/useRealtime";
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
  const { data: entities, isLoading } = useEntities(entityName);

  useRealtimeEntity(entityName);

  if (!meta || isLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading...
      </div>
    );
  }

  const columns: ColumnDef<Record<string, unknown>>[] = [];

  if (meta.state_machine) {
    columns.push({
      accessorKey: "status",
      header: "Status",
      cell: ({ getValue }) => <StateIndicator state={String(getValue() || "")} />,
    });
  }

  const visibleFields = meta.fields
    .filter(
      (f) =>
        !f.name.startsWith("_") &&
        f.name !== "org_id" &&
        f.name !== "version"
    )
    .slice(0, 7);

  for (const field of visibleFields) {
    columns.push({
      accessorKey: field.name,
      header: field.description || field.name.replace(/_/g, " "),
      cell: ({ getValue }) => (
        <FieldRenderer type={field.type} value={getValue()} meta={field} />
      ),
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
