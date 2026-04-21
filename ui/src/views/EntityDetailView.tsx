import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { Breadcrumb } from "../components/Breadcrumb";
import { useEntity, useEntityMeta, useChanges } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { useRealtimeEntityDetail } from "../hooks/useRealtime";
import { apiClient } from "../api/client";
import { InlineField } from "../components/InlineField";
import { ResolvedLink } from "../components/ResolvedLink";
import { StateIndicator } from "../components/StateIndicator";
import { ChangeTimeline } from "../components/ChangeTimeline";
import { useToast } from "../context/ToastContext";

const SYSTEM_FIELDS = new Set([
  "_id",
  "org_id",
  "version",
  "created_at",
  "updated_at",
  "created_by",
]);

/** Smart field ordering: name/title first, state, key fields, then the rest. */
function orderFields(fields: { name: string; is_state_field?: boolean; type: string }[]) {
  const priority: Record<string, number> = {
    name: 0, title: 1, deal_id: 2, stage: 3, status: 3,
    company: 5, owner: 6, next_step: 7, next_step_owner: 8,
    use_case: 10, primary_outcome: 11, proposal_candidate: 12,
  };
  return [...fields].sort((a, b) => {
    const pa = priority[a.name] ?? 50;
    const pb = priority[b.name] ?? 50;
    if (pa !== pb) return pa - pb;
    return a.name.localeCompare(b.name);
  });
}

export function EntityDetailView() {
  const { entityType, entityId } = useParams<{
    entityType: string;
    entityId: string;
  }>();
  const entityName = useEntityNameFromSlug(entityType) || "";
  const { data: meta } = useEntityMeta(entityName);
  const { data: entity, refetch } = useEntity(entityName, entityId || "");
  const { data: changes } = useChanges(entityName, entityId || "");
  const { toast } = useToast();

  useRealtimeEntityDetail(entityName, entityId);

  useEffect(() => {
    if (entity) {
      const name = String(entity.name || entity.title || entity.deal_id || entityId);
      document.title = `${name} - ${entityName} - Indemn OS`;
    }
  }, [entity, entityName, entityId]);

  if (!meta || !entity) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading...
      </div>
    );
  }

  const currentState = meta.fields.find(f => f.is_state_field);
  const stateValue = currentState ? String(entity[currentState.name] || "") : "";
  const displayName = String(entity.name || entity.title || entity.deal_id || entityId);

  const editableFields = orderFields(
    meta.fields.filter(
      (f) => !f.name.startsWith("_") && !SYSTEM_FIELDS.has(f.name) && !f.is_state_field
    )
  );

  // Split into primary (key fields) and secondary (notes, long text, less important)
  const primaryFields = editableFields.filter(f =>
    f.type !== "list" && f.type !== "dict" &&
    !["notes", "competitive_notes", "lost_reason"].includes(f.name)
  );
  const secondaryFields = editableFields.filter(f =>
    f.type === "list" || f.type === "dict" ||
    ["notes", "competitive_notes", "lost_reason"].includes(f.name)
  );

  const saveField = async (fieldName: string, value: unknown) => {
    try {
      await apiClient(`/api/${entityType}/${entityId}`, {
        method: "PUT",
        body: JSON.stringify({ [fieldName]: value }),
      });
      toast(`Updated ${fieldName.replace(/_/g, " ")}`, "success");
      refetch();
    } catch (err) {
      toast(
        `Failed: ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
      throw err;
    }
  };

  return (
    <div>
      <Breadcrumb
        crumbs={[
          { label: entityName, to: `/${entityType}` },
          { label: displayName },
        ]}
      />

      {/* Header: name + state */}
      <div className="flex items-center gap-4 mb-6">
        <h1 className="text-xl font-semibold">{displayName}</h1>
        {currentState && meta.state_machine && (
          <StateIndicator
            state={stateValue}
            entityName={displayName}
            availableTransitions={meta.state_machine[stateValue] || []}
            onTransition={async (to) => {
              try {
                await apiClient(`/api/${entityType}/${entityId}/transition`, {
                  method: "POST",
                  body: JSON.stringify({ to }),
                });
                toast(`Transitioned to ${to}`, "success");
                refetch();
              } catch (err) {
                toast(
                  `Transition failed: ${err instanceof Error ? err.message : String(err)}`,
                  "error"
                );
              }
            }}
            canTransition={meta.permissions.write}
          />
        )}
      </div>

      <div className="grid grid-cols-3 gap-6">
        {/* Main content: fields as read view with inline edit */}
        <div className="col-span-2 space-y-0.5">
          {/* Primary fields in a clean grid */}
          <div className="bg-white rounded-lg border p-4">
            {primaryFields.map((field) => (
              <div
                key={field.name}
                className="flex items-start py-2 border-b border-gray-50 last:border-b-0"
              >
                <label className="text-sm text-gray-500 w-40 shrink-0 pt-1.5">
                  {field.description || field.name.replace(/_/g, " ")}
                </label>
                <div className="flex-1 min-w-0">
                  <InlineField
                    field={field}
                    value={entity[field.name]}
                    onSave={(v) => saveField(field.name, v)}
                    canEdit={meta.permissions.write}
                  />
                </div>
              </div>
            ))}
          </div>

          {/* Secondary fields (notes, lists) */}
          {secondaryFields.length > 0 && (
            <div className="bg-white rounded-lg border p-4 mt-4">
              {secondaryFields.map((field) => (
                <div key={field.name} className="py-2 border-b border-gray-50 last:border-b-0">
                  <label className="text-sm text-gray-500 block mb-1">
                    {field.description || field.name.replace(/_/g, " ")}
                  </label>
                  <InlineField
                    field={field}
                    value={entity[field.name]}
                    onSave={(v) => saveField(field.name, v)}
                    canEdit={meta.permissions.write}
                  />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-4">
          {/* Capability buttons */}
          {meta.capabilities?.map((cap) => (
            <button
              key={cap.name}
              onClick={async () => {
                try {
                  await apiClient(
                    `/api/${entityType}/${entityId}/${cap.name.replace(/_/g, "-")}?auto=true`,
                    { method: "POST", body: "{}" }
                  );
                  refetch();
                } catch (err) {
                  toast(
                    `${cap.name} failed: ${err instanceof Error ? err.message : String(err)}`,
                    "error"
                  );
                }
              }}
              className="w-full px-3 py-2 text-sm border rounded hover:bg-blue-50 text-blue-600 text-left"
            >
              {cap.name.replace(/_/g, " ")}
            </button>
          ))}

          {/* @exposed method buttons */}
          {meta.exposed_methods?.map((method) => (
            <button
              key={method.name}
              onClick={async () => {
                try {
                  await apiClient(
                    `/api/${entityType}/${entityId}/${method.name.replace(/_/g, "-")}`,
                    { method: "POST", body: "{}" }
                  );
                  refetch();
                } catch (err) {
                  toast(
                    `${method.name} failed: ${err instanceof Error ? err.message : String(err)}`,
                    "error"
                  );
                }
              }}
              className="w-full px-3 py-2 text-sm border rounded hover:bg-green-50 text-green-600 text-left"
            >
              {method.name.replace(/_/g, " ")}
            </button>
          ))}

          {/* Metadata */}
          <div className="bg-white rounded-lg border p-4 text-xs text-gray-400 space-y-1">
            <div>ID: <span className="font-mono">{String(entity._id)}</span></div>
            <div>Created: {entity.created_at ? new Date(String(entity.created_at)).toLocaleString() : "—"}</div>
            <div>Updated: {entity.updated_at ? new Date(String(entity.updated_at)).toLocaleString() : "—"}</div>
            <div>Version: {String(entity.version || 1)}</div>
          </div>

          {/* Recent changes */}
          <ChangeTimeline changes={changes || []} />
        </div>
      </div>
    </div>
  );
}
