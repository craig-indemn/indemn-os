import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { wsManager, type EntityChange } from "../api/websocket";
import { useAuth } from "../auth/useAuth";

/** Connect WebSocket on auth and manage lifecycle. */
export function useRealtimeConnection() {
  const { isAuthenticated } = useAuth();
  const connected = useRef(false);

  useEffect(() => {
    if (isAuthenticated && !connected.current) {
      wsManager.connect();
      connected.current = true;
    }
    return () => {
      if (connected.current) {
        wsManager.disconnect();
        connected.current = false;
      }
    };
  }, [isAuthenticated]);
}

/** Subscribe to real-time updates for an entity type and auto-invalidate queries. */
export function useRealtimeEntity(entityType: string | undefined) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!entityType) return;

    const subId = wsManager.subscribe(
      { entity_type: entityType },
      (_change: EntityChange) => {
        queryClient.invalidateQueries({ queryKey: ["entities", entityType] });
      }
    );

    return () => wsManager.unsubscribe(subId);
  }, [entityType, queryClient]);
}

/** Subscribe to a specific entity's changes. */
export function useRealtimeEntityDetail(
  entityType: string | undefined,
  entityId: string | undefined
) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!entityType || !entityId) return;

    const subId = wsManager.subscribe(
      { entity_type: entityType, entity_id: entityId },
      (_change: EntityChange) => {
        queryClient.invalidateQueries({
          queryKey: ["entity", entityType, entityId],
        });
        queryClient.invalidateQueries({
          queryKey: ["changes", entityType, entityId],
        });
      }
    );

    return () => wsManager.unsubscribe(subId);
  }, [entityType, entityId, queryClient]);
}
