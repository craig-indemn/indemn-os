import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";
import { ResolvedLink } from "../components/ResolvedLink";

interface FieldChange {
  field: string;
  old_value: unknown;
  new_value: unknown;
}

interface ActivityItem {
  id: string;
  timestamp: string | null;
  entity_type: string;
  entity_id: string;
  change_type: string;
  actor_id: string | null;
  method: string | null;
  correlation_id: string | null;
  changes: FieldChange[];
}

interface ActivityResponse {
  items: ActivityItem[];
  total: number;
  limit: number;
  skip: number;
}

const PAGE_SIZE = 50;

export function ActivityView() {
  const [entityFilter, setEntityFilter] = useState("");
  const [changeTypeFilter, setChangeTypeFilter] = useState("");
  const [page, setPage] = useState(0);

  const params = new URLSearchParams();
  if (entityFilter) params.set("entity_type", entityFilter);
  if (changeTypeFilter) params.set("change_type", changeTypeFilter);
  params.set("limit", String(PAGE_SIZE));
  params.set("skip", String(page * PAGE_SIZE));

  const { data, isLoading } = useQuery({
    queryKey: ["activity", entityFilter, changeTypeFilter, page],
    queryFn: () =>
      apiClient<ActivityResponse>(`/api/trace/activity?${params.toString()}`),
    refetchInterval: 10_000,
  });

  const entityTypes = [
    ...new Set((data?.items || []).map((i) => i.entity_type)),
  ].sort();

  return (
    <div>
      <h1 className="text-xl font-semibold mb-4">Activity</h1>

      <div className="flex gap-3 mb-4">
        <select
          value={entityFilter}
          onChange={(e) => {
            setEntityFilter(e.target.value);
            setPage(0);
          }}
          className="px-3 py-1.5 border rounded text-sm bg-white"
        >
          <option value="">All entities</option>
          {["Company", "Contact", "Deal", "Meeting", "Task", "Commitment", "Signal", "Decision", "SuccessPhase", "Conference", "AssociateDeployment", "Outcome", "Playbook", "AssociateType", "OutcomeType", "Stage", "Actor", "Role"].map(
            (t) => (
              <option key={t} value={t}>
                {t}
              </option>
            )
          )}
        </select>
        <select
          value={changeTypeFilter}
          onChange={(e) => {
            setChangeTypeFilter(e.target.value);
            setPage(0);
          }}
          className="px-3 py-1.5 border rounded text-sm bg-white"
        >
          <option value="">All types</option>
          <option value="create">Created</option>
          <option value="update">Updated</option>
          <option value="transition">Transitioned</option>
          <option value="delete">Deleted</option>
        </select>
        {data && (
          <span className="text-sm text-gray-400 self-center">
            {data.total} total changes
          </span>
        )}
      </div>

      {isLoading ? (
        <div className="text-gray-400 py-8 text-center">Loading...</div>
      ) : !data?.items.length ? (
        <div className="text-gray-400 py-8 text-center">No activity found</div>
      ) : (
        <div className="space-y-1">
          {data.items.map((item) => (
            <ActivityRow key={item.id} item={item} />
          ))}
        </div>
      )}

      {data && data.total > PAGE_SIZE && (
        <div className="flex items-center gap-4 mt-4 text-sm">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 border rounded disabled:opacity-30"
          >
            Previous
          </button>
          <span className="text-gray-500">
            Page {page + 1} of {Math.ceil(data.total / PAGE_SIZE)}
          </span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={(page + 1) * PAGE_SIZE >= data.total}
            className="px-3 py-1 border rounded disabled:opacity-30"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const [expanded, setExpanded] = useState(false);
  const slug = item.entity_type.toLowerCase() + "s";
  const ts = item.timestamp
    ? new Date(item.timestamp).toLocaleString()
    : "—";

  const changeTypeColors: Record<string, string> = {
    create: "bg-green-100 text-green-700",
    update: "bg-blue-100 text-blue-700",
    transition: "bg-purple-100 text-purple-700",
    delete: "bg-red-100 text-red-700",
  };

  const colorClass =
    changeTypeColors[item.change_type] || "bg-gray-100 text-gray-700";

  const summary =
    item.change_type === "create"
      ? "created"
      : item.changes.length > 0
        ? item.changes.map((c) => c.field).join(", ")
        : item.change_type;

  return (
    <div
      className="border rounded px-4 py-2.5 hover:bg-gray-50 cursor-pointer"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-center gap-3">
        <span className="text-xs text-gray-400 w-40 shrink-0">{ts}</span>
        <span
          className={`text-xs px-2 py-0.5 rounded font-medium w-20 text-center shrink-0 ${colorClass}`}
        >
          {item.change_type}
        </span>
        <span className="text-sm text-gray-500 w-28 shrink-0">
          {item.entity_type}
        </span>
        <Link
          to={`/${slug}/${item.entity_id}`}
          onClick={(e) => e.stopPropagation()}
          className="text-sm text-blue-600 hover:underline shrink-0"
        >
          <EntityName entityType={item.entity_type} entityId={item.entity_id} />
        </Link>
        <span className="text-sm text-gray-500 truncate flex-1">{summary}</span>
        {item.actor_id && (
          <span className="text-xs text-gray-400 shrink-0">
            by <ResolvedLink entityType="Actor" entityId={item.actor_id} className="text-xs text-gray-500 hover:underline" />
          </span>
        )}
        <span className="text-gray-300 text-xs">{expanded ? "▼" : "▶"}</span>
      </div>

      {expanded && item.changes.length > 0 && (
        <div className="mt-2 ml-44 space-y-1">
          {item.changes.map((c, i) => (
            <div key={i} className="text-xs flex gap-2">
              <span className="text-gray-500 font-medium w-32 shrink-0">
                {c.field}
              </span>
              {c.old_value !== null && c.old_value !== undefined ? (
                <>
                  <span className="text-red-400 line-through truncate max-w-48">
                    {formatValue(c.old_value)}
                  </span>
                  <span className="text-gray-300">→</span>
                </>
              ) : null}
              <span className="text-green-600 truncate max-w-96">
                {formatValue(c.new_value)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function EntityName({
  entityType,
  entityId,
}: {
  entityType: string;
  entityId: string;
}) {
  const slug = entityType.toLowerCase() + "s";
  const { data } = useQuery({
    queryKey: ["resolved-name", entityType, entityId],
    queryFn: () =>
      apiClient<Record<string, unknown>>(`/api/${slug}/${entityId}`),
    staleTime: 5 * 60 * 1000,
    enabled: !!entityId && entityId.length >= 12,
  });

  if (!data) return <span className="text-gray-400">{entityId.slice(-8)}</span>;
  return <>{String(data.name || data.title || data.deal_id || entityId.slice(-8))}</>;
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return v.length > 80 ? v.slice(0, 80) + "…" : v;
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
