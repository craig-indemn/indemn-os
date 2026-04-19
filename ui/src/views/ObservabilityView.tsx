import { useAllEntityMeta } from "../api/hooks";
import { IntegrationHealth } from "../components/IntegrationHealth";
import { PipelineMetrics } from "../components/PipelineMetrics";

export function ObservabilityView() {
  const { data: allMeta } = useAllEntityMeta();

  // Show state distribution for entities that have state machines
  const statefulEntities = allMeta
    ?.filter((e) => e.state_machine && !e.is_kernel_entity)
    .map((e) => e.name) || [];

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Observability</h1>

      {/* Queue depth across all roles */}
      <div className="bg-white border rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3">Queue Depth</h2>
        <PipelineMetrics />
      </div>

      {/* State distribution per entity type */}
      {statefulEntities.length > 0 && (
        <div className="bg-white border rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-3">State Distribution</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {statefulEntities.map((name) => (
              <div key={name} className="border rounded p-3">
                <h3 className="text-sm font-medium text-gray-700 mb-2">{name}</h3>
                <PipelineMetrics entityName={name} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Integration health */}
      <div className="bg-white border rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-3">Integrations</h2>
        <IntegrationHealth />
      </div>
    </div>
  );
}
