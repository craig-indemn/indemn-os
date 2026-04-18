import { IntegrationHealth } from "../components/IntegrationHealth";
import { PipelineMetrics } from "../components/PipelineMetrics";

export function ObservabilityView() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Observability</h1>
      <PipelineMetrics />
      <IntegrationHealth />
    </div>
  );
}
