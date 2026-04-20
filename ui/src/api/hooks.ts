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
