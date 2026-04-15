/** TanStack Query hooks for entity data fetching. */

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "./client";
import type { AuthEvent, ChangeRecord, EntityMeta, QueueMessage } from "./types";

export function useEntities(entityName: string, params?: Record<string, string>) {
  return useQuery({
    queryKey: ["entities", entityName, params],
    queryFn: () =>
      apiClient<Record<string, unknown>[]>(
        `/api/${entityName.toLowerCase()}s?${new URLSearchParams(params || {})}`
      ),
  });
}

export function useEntity(entityName: string, entityId: string) {
  return useQuery({
    queryKey: ["entity", entityName, entityId],
    queryFn: () =>
      apiClient<Record<string, unknown>>(
        `/api/${entityName.toLowerCase()}s/${entityId}`
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
    queryFn: () =>
      apiClient<ChangeRecord[]>(
        `/api/audit/changes?entity_type=${entityName}&entity_id=${entityId}&limit=20`
      ),
    enabled: !!entityId,
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
