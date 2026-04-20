import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiClient } from "../api/client";

interface Props {
  entityType: string;
  entityId: string;
  className?: string;
}

/** Resolves an ObjectId to a human-readable name with a clickable link. */
export function ResolvedLink({ entityType, entityId, className }: Props) {
  const slug = entityType.toLowerCase() + "s";
  const { data } = useQuery({
    queryKey: ["resolved-name", entityType, entityId],
    queryFn: () => apiClient<Record<string, unknown>>(`/api/${slug}/${entityId}`),
    staleTime: 5 * 60 * 1000,
    enabled: !!entityId && entityId.length >= 12,
  });

  const displayName = data
    ? String(data.name || data.email || data.title || entityId.slice(-8))
    : "Loading\u2026";

  return (
    <Link
      to={`/${slug}/${entityId}`}
      className={className || "text-blue-600 hover:underline"}
    >
      {displayName}
    </Link>
  );
}
