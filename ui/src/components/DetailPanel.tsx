import { useEntity, useEntityMeta, useChanges } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { Link } from "react-router-dom";
import { apiClient } from "../api/client";
import { InlineField } from "./InlineField";
import { StateIndicator } from "./StateIndicator";
import { ChangeTimeline } from "./ChangeTimeline";
import { useToast } from "../context/ToastContext";

const SYSTEM_FIELDS = new Set([
  "_id", "org_id", "version", "created_at", "updated_at", "created_by",
]);

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

interface Props {
  entitySlug: string;
  entityId: string;
  onClose: () => void;
}

export function DetailPanel({ entitySlug, entityId, onClose }: Props) {
  const entityName = useEntityNameFromSlug(entitySlug) || "";
  const { data: meta } = useEntityMeta(entityName);
  const { data: entity, refetch } = useEntity(entityName, entityId);
  const { data: changes } = useChanges(entityName, entityId);
  const { toast } = useToast();

  if (!meta || !entity) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400">
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

  const saveField = async (fieldName: string, value: unknown) => {
    try {
      await apiClient(`/api/${entitySlug}/${entityId}`, {
        method: "PUT",
        body: JSON.stringify({ [fieldName]: value }),
      });
      toast(`Updated ${fieldName.replace(/_/g, " ")}`, "success");
      refetch();
    } catch (err) {
      toast(`Failed: ${err instanceof Error ? err.message : String(err)}`, "error");
      throw err;
    }
  };

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Header */}
      <div className="px-4 py-3 border-b flex items-center gap-3 shrink-0">
        <div className="flex-1 min-w-0">
          <h2 className="font-semibold text-lg truncate">{displayName}</h2>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-xs text-gray-400">{entityName}</span>
            {currentState && meta.state_machine && (
              <StateIndicator
                state={stateValue}
                entityName={displayName}
                availableTransitions={meta.state_machine[stateValue] || []}
                onTransition={async (to) => {
                  try {
                    await apiClient(`/api/${entitySlug}/${entityId}/transition`, {
                      method: "POST",
                      body: JSON.stringify({ to }),
                    });
                    toast(`Transitioned to ${to}`, "success");
                    refetch();
                  } catch (err) {
                    toast(`Transition failed: ${err instanceof Error ? err.message : String(err)}`, "error");
                  }
                }}
                canTransition={meta.permissions.write}
              />
            )}
          </div>
        </div>
        <Link
          to={`/${entitySlug}/${entityId}`}
          className="text-xs text-blue-600 hover:underline shrink-0"
          title="Open full page"
        >
          Full view →
        </Link>
        <button
          onClick={onClose}
          className="w-7 h-7 rounded hover:bg-gray-100 flex items-center justify-center text-gray-400 shrink-0"
          title="Close panel"
        >
          ✕
        </button>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto px-4 py-3">
        {editableFields.map((field) => (
          <div
            key={field.name}
            className="flex items-start py-1.5 border-b border-gray-50 last:border-b-0"
          >
            <label className="text-xs text-gray-400 w-32 shrink-0 pt-1.5 truncate" title={field.name}>
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

        {/* Metadata */}
        <div className="mt-4 pt-3 border-t text-xs text-gray-300 space-y-0.5">
          <div>Created: {entity.created_at ? new Date(String(entity.created_at)).toLocaleString() : "—"}</div>
          <div>Updated: {entity.updated_at ? new Date(String(entity.updated_at)).toLocaleString() : "—"}</div>
          <div>Version: {String(entity.version || 1)}</div>
        </div>

        {/* Changes timeline */}
        <div className="mt-4">
          <ChangeTimeline changes={changes || []} />
        </div>
      </div>
    </div>
  );
}
