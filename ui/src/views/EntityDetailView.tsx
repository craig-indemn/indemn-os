import { useParams, Link } from "react-router-dom";
import { useEntity, useEntityMeta, useChanges } from "../api/hooks";
import { useRealtimeEntityDetail } from "../hooks/useRealtime";
import { apiClient } from "../api/client";
import { EntityForm } from "../components/EntityForm";
import { StateIndicator } from "../components/StateIndicator";
import { ChangeTimeline } from "../components/ChangeTimeline";

export function EntityDetailView() {
  const { entityType, entityId } = useParams<{
    entityType: string;
    entityId: string;
  }>();
  const entityName = entityType
    ? entityType.replace(/s$/, "").replace(/^./, (c) => c.toUpperCase())
    : "";
  const { data: meta } = useEntityMeta(entityName);
  const { data: entity, refetch } = useEntity(entityName, entityId || "");
  const { data: changes } = useChanges(entityName, entityId || "");

  useRealtimeEntityDetail(entityName, entityId);

  if (!meta || !entity) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading...
      </div>
    );
  }

  const currentState = String(entity.status || entity.stage || "");

  return (
    <div>
      <div className="mb-4">
        <Link
          to={`/${entityType}`}
          className="text-blue-600 hover:underline text-sm"
        >
          &larr; Back to {entityName} list
        </Link>
      </div>
      <h1 className="text-xl font-semibold mb-6">{entityName} Detail</h1>
      <div className="grid grid-cols-3 gap-6">
        <div className="col-span-2">
          <EntityForm
            meta={meta}
            entity={entity}
            onSave={async (data) => {
              await apiClient(`/api/${entityType}/${entityId}`, {
                method: "PUT",
                body: JSON.stringify(data),
              });
              refetch();
            }}
          />
        </div>
        <div className="space-y-6">
          {meta.state_machine && (
            <div className="bg-white p-4 rounded-lg border">
              <h3 className="text-sm font-medium text-gray-700 mb-2">State</h3>
              <StateIndicator
                state={currentState}
                availableTransitions={meta.state_machine[currentState] || []}
                onTransition={async (to) => {
                  await apiClient(
                    `/api/${entityType}/${entityId}/transition`,
                    {
                      method: "POST",
                      body: JSON.stringify({ to }),
                    }
                  );
                  refetch();
                }}
                canTransition={meta.permissions.write}
              />
            </div>
          )}
          {/* Capability buttons */}
          {meta.capabilities?.map((cap) => (
            <button
              key={cap.name}
              onClick={async () => {
                await apiClient(
                  `/api/${entityType}/${entityId}/${cap.name.replace(/_/g, "-")}?auto=true`,
                  { method: "POST", body: "{}" }
                );
                refetch();
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
                await apiClient(
                  `/api/${entityType}/${entityId}/${method.name.replace(/_/g, "-")}`,
                  { method: "POST", body: "{}" }
                );
                refetch();
              }}
              className="w-full px-3 py-2 text-sm border rounded hover:bg-green-50 text-green-600 text-left"
            >
              {method.name.replace(/_/g, " ")}
            </button>
          ))}

          {/* Related entities */}
          {meta.fields
            .filter((f) => f.is_relationship && entity[f.name])
            .map((f) => (
              <div key={f.name} className="text-sm">
                <span className="text-gray-500">{f.name}: </span>
                <Link
                  to={`/${f.relationship_target?.toLowerCase()}s/${entity[f.name]}`}
                  className="text-blue-600 hover:underline"
                >
                  {String(entity[f.name]).slice(-8)}
                </Link>
              </div>
            ))}

          {/* Recent changes */}
          <ChangeTimeline changes={changes || []} />
        </div>
      </div>
    </div>
  );
}
