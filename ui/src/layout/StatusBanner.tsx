import { useQuery } from "@tanstack/react-query";
import type { HealthStatus } from "../api/types";

export function StatusBanner() {
  const { data: health } = useQuery<HealthStatus>({
    queryKey: ["health"],
    queryFn: () => fetch("/health").then((r) => r.json()),
    refetchInterval: 30000,
  });

  if (!health || health.status === "healthy") return null;

  const degraded = Object.entries(health.checks)
    .filter(([, status]) => status !== "ok")
    .map(([name, status]) => `${name} (${status})`);

  return (
    <div className="bg-yellow-50 border-b border-yellow-200 px-4 py-2 text-sm text-yellow-800">
      <strong>System degraded:</strong> {degraded.join(", ")}.
      Some features may be temporarily unavailable.
    </div>
  );
}
