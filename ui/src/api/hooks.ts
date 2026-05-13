/** TanStack Query hooks for entity data fetching. */

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "./client";
import type { AuthEvent, ChangeRecord, EntityMeta, QueueMessage } from "./types";

export function useEntities(entityName: string, params?: Record<string, string>) {
  return useQuery({
    queryKey: ["entities", entityName, params],
    queryFn: () => {
      const qs = new URLSearchParams(params || {});
      const query = qs.toString() ? `?${qs}` : "";
      return apiClient<Record<string, unknown>[]>(
        `/api/${entityName.toLowerCase()}s/${query}`
      );
    },
    enabled: !!entityName,
  });
}

export function useEntity(entityName: string, entityId: string) {
  return useQuery({
    queryKey: ["entity", entityName, entityId],
    queryFn: () =>
      apiClient<Record<string, unknown>>(
        `/api/${entityName.toLowerCase()}s/${entityId}?depth=2&include_related=true`
      ),
    enabled: !!entityId,
  });
}

export function useEntityMeta(entityName: string) {
  return useQuery({
    queryKey: ["entity-meta-detail", entityName],
    queryFn: () => apiClient<EntityMeta>(`/api/_meta/entities/${entityName}`),
    enabled: !!entityName,
  });
}

export function useAllEntityMeta() {
  return useQuery({
    queryKey: ["entity-meta"],
    queryFn: () => apiClient<EntityMeta[]>("/api/_meta/entities"),
  });
}

export function useQueue() {
  return useQuery({
    queryKey: ["queue"],
    queryFn: () => apiClient<QueueMessage[]>("/api/queue/messages?status=pending"),
    refetchInterval: 10000,
  });
}

export function useChanges(entityName: string, entityId: string) {
  return useQuery({
    queryKey: ["changes", entityName, entityId],
    queryFn: async () => {
      const trace = await apiClient<{
        timeline: Array<Record<string, unknown>>;
        summary: Record<string, unknown>;
      }>(`/api/trace/entity/${entityName}/${entityId}?limit=20`);
      // Extract change entries from the unified timeline
      return (trace.timeline || [])
        .filter((e) => e.source === "changes")
        .map((e) => {
          const raw = (e.changes || []) as Array<Record<string, unknown>>;
          return {
            id: String(e.entity_id || e.id || ""),
            entity_type: String(e.entity_type || entityName),
            entity_id: String(e.entity_id || entityId),
            actor_id: String(e.actor_id || ""),
            timestamp: String(e.timestamp || ""),
            change_type: String(e.change_type || ""),
            method: e.method as string | undefined,
            changes: raw.map((c) => ({
              field: String(c.field || ""),
              old_value: c.old_value ?? c.old,
              new_value: c.new_value ?? c.new,
            })),
          } satisfies ChangeRecord;
        });
    },
    enabled: !!entityId && !!entityName,
  });
}

export function useAuthEvents(params?: { limit?: number; event_type?: string }) {
  return useQuery({
    queryKey: ["auth-events", params],
    queryFn: () => {
      const qs = new URLSearchParams();
      if (params?.limit) qs.set("limit", String(params.limit));
      if (params?.event_type) qs.set("event_type", params.event_type);
      return apiClient<AuthEvent[]>(`/api/auth-events?${qs}`);
    },
  });
}

export function useStateDistribution(entityName: string) {
  return useQuery({
    queryKey: ["metrics", "state-distribution", entityName],
    queryFn: () =>
      apiClient<Record<string, number>>(
        `/api/metrics/state-distribution/${entityName}`
      ),
    enabled: !!entityName,
  });
}

export function useQueueDepth() {
  return useQuery({
    queryKey: ["metrics", "queue-depth"],
    queryFn: () => apiClient<Record<string, number>>("/api/metrics/queue-depth"),
    refetchInterval: 15000,
  });
}

// --- Associate Runs / Traces ---

const TRACE_HEAVY_FIELDS = ["messages", "inputs", "outputs", "child_runs", "events"];

function stripHeavyFields(trace: Record<string, unknown>): Record<string, unknown> {
  const light: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(trace)) {
    if (!TRACE_HEAVY_FIELDS.includes(k)) light[k] = v;
  }
  return light;
}

function stripRedundantTraceFields(trace: Record<string, unknown>): Record<string, unknown> {
  const clean: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(trace)) {
    if (k !== "inputs" && k !== "outputs" && k !== "child_runs") clean[k] = v;
  }
  return clean;
}

export function useTraces(params?: {
  associate_name?: string;
  execution_status?: string;
  limit?: number;
}) {
  const filter: Record<string, string> = {};
  if (params?.associate_name) filter.associate_name = params.associate_name;
  if (params?.execution_status) filter.execution_status = params.execution_status;
  const filterStr = Object.keys(filter).length > 0 ? JSON.stringify(filter) : "";

  return useQuery({
    queryKey: ["traces", params],
    queryFn: async () => {
      const qs = new URLSearchParams();
      if (filterStr) qs.set("filter", filterStr);
      qs.set("sort", "-start_time");
      qs.set("limit", String(params?.limit ?? 50));
      const raw = await apiClient<Record<string, unknown>[]>(`/api/traces/?${qs}`);
      return raw.map(stripHeavyFields);
    },
    refetchInterval: 5000,
  });
}

export function useTraceDetail(traceId: string) {
  return useQuery({
    queryKey: ["trace-detail", traceId],
    queryFn: async () => {
      const raw = await apiClient<Record<string, unknown>>(`/api/traces/${traceId}`);
      return stripRedundantTraceFields(raw);
    },
    enabled: !!traceId,
  });
}

export function useEvalForTrace(traceId: string) {
  return useQuery({
    queryKey: ["eval-for-trace", traceId],
    queryFn: () => {
      const filter = JSON.stringify({ trace_id: traceId });
      return apiClient<Record<string, unknown>[]>(`/api/evaluation_results/?filter=${encodeURIComponent(filter)}&limit=1`);
    },
    enabled: !!traceId,
  });
}

export function useEvaluatorTrace(ecTraceId: string) {
  return useQuery({
    queryKey: ["evaluator-trace", ecTraceId],
    queryFn: async () => {
      const filter = JSON.stringify({ entity_id: ecTraceId, associate_name: "Evaluator" });
      const qs = new URLSearchParams({ filter, sort: "-start_time", limit: "1" });
      const raw = await apiClient<Record<string, unknown>[]>(`/api/traces/?${qs}`);
      if (raw.length === 0) return null;
      return stripRedundantTraceFields(raw[0]);
    },
    enabled: !!ecTraceId,
  });
}
