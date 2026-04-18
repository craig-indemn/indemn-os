import { useParams } from "react-router-dom";
import { CascadeViewer } from "./CascadeViewer";

export function CascadeViewerRoute() {
  const { correlationId } = useParams<{ correlationId: string }>();

  if (!correlationId) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        No correlation ID provided
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Trace Viewer</h1>
      <p className="text-sm text-gray-500">
        Correlation ID: <code className="font-mono">{correlationId}</code>
      </p>
      <CascadeViewer correlationId={correlationId} />
    </div>
  );
}
