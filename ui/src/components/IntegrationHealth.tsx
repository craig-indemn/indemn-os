import { useEntities } from "../api/hooks";

export function IntegrationHealth() {
  const { data: integrations } = useEntities("Integration");

  if (!integrations) return null;

  return (
    <div>
      <h3 className="text-sm font-medium text-gray-700 mb-2">Integrations</h3>
      <div className="space-y-1">
        {integrations.map((integration) => (
          <div
            key={String(integration._id)}
            className="flex items-center justify-between text-sm py-1"
          >
            <span>{String(integration.name)}</span>
            <span
              className={`px-2 py-0.5 rounded text-xs ${
                integration.status === "active"
                  ? "bg-green-100 text-green-700"
                  : "bg-gray-100 text-gray-500"
              }`}
            >
              {String(integration.status)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
