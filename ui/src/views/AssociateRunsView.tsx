import { useState, useMemo } from "react";
import { type ColumnDef } from "@tanstack/react-table";
import { Badge } from "@/components/ui/badge";
import { useTraces, useEntities, useActivitySummary } from "@/api/hooks";
import { ActivityTimeline } from "@/components/ActivityTimeline";
import { RunDetailPanel } from "@/components/RunDetailPanel";
import { EntityTable } from "@/components/EntityTable";
import { formatTime, formatDuration, formatTokens, shortId } from "@/lib/format";
import { associateColor, associateAbbrev } from "@/lib/colors";
import type { Trace } from "@/api/types";

const PAGE_SIZE = 25;

function evalBadge(feedbackStats: Trace["feedback_stats"] | undefined): React.ReactNode {
  if (!feedbackStats || typeof feedbackStats !== "object") return <span className="text-gray-300 text-xs">—</span>;
  const entries = Object.entries(feedbackStats).filter(([k]) => k !== "evaluation_passed");
  if (entries.length === 0) return <span className="text-gray-300 text-xs">—</span>;
  const passed = entries.filter(([, v]) => v?.passed).length;
  const total = entries.length;
  const allPassed = passed === total;
  return (
    <Badge
      variant={allPassed ? "outline" : "destructive"}
      className={allPassed ? "text-green-700 border-green-300 bg-green-50 text-[10px]" : "text-[10px]"}
    >
      {passed}/{total}
    </Badge>
  );
}

const columns: ColumnDef<Record<string, unknown>>[] = [
  {
    id: "time",
    header: "Time",
    accessorFn: (row) => row.start_time,
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-gray-500">{formatTime(getValue() as string)}</span>
    ),
    enableColumnFilter: false,
  },
  {
    id: "associate",
    header: "Associate",
    accessorFn: (row) => row.associate_name,
    cell: ({ getValue }) => {
      const name = String(getValue() || "");
      return (
        <span
          className="px-1.5 py-0.5 rounded text-[10px] font-semibold text-white"
          style={{ backgroundColor: associateColor(name) }}
        >
          {associateAbbrev(name)}
        </span>
      );
    },
    enableColumnFilter: false,
  },
  {
    id: "entity",
    header: "Entity",
    accessorFn: (row) => `${row.entity_type || ""} · ${shortId(row.entity_id as string)}`,
    cell: ({ row: r }) => (
      <div>
        <span className="font-mono text-xs">
          {String(r.original.entity_type || "")} · {shortId(r.original.entity_id as string)}
        </span>
        {r.original.execution_status === "error" && r.original.error ? (
          <div className="text-xs text-red-600 truncate max-w-[300px]">
            {String(r.original.error).slice(0, 80)}
          </div>
        ) : null}
      </div>
    ),
    enableColumnFilter: false,
  },
  {
    id: "status",
    header: "Status",
    accessorFn: (row) => row.execution_status,
    cell: ({ getValue }) => {
      const isError = getValue() === "error";
      return (
        <Badge
          variant={isError ? "destructive" : "outline"}
          className={isError ? "text-[10px]" : "text-green-700 border-green-300 bg-green-50 text-[10px]"}
        >
          {isError ? "error" : "success"}
        </Badge>
      );
    },
    meta: { enumValues: ["success", "error"] },
  },
  {
    id: "duration",
    header: "Duration",
    accessorFn: (row) => row.duration_ms,
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-gray-600">{formatDuration(getValue() as number)}</span>
    ),
    enableColumnFilter: false,
  },
  {
    id: "tokens",
    header: "Tokens",
    accessorFn: (row) => row.total_tokens,
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-gray-600">{formatTokens(getValue() as number)}</span>
    ),
    enableColumnFilter: false,
  },
  {
    id: "eval",
    header: "Eval",
    accessorFn: (row) => row.feedback_stats,
    cell: ({ getValue }) => evalBadge(getValue() as Trace["feedback_stats"]),
    enableColumnFilter: false,
    enableSorting: false,
  },
];

const TIME_RANGES = ["1h", "6h", "24h", "7d", "30d"] as const;

function computeSince(range: string): string {
  const now = new Date();
  switch (range) {
    case "1h": return new Date(now.getTime() - 60 * 60 * 1000).toISOString();
    case "6h": return new Date(now.getTime() - 6 * 60 * 60 * 1000).toISOString();
    case "24h": return new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString();
    case "7d": return new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000).toISOString();
    case "30d": return new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000).toISOString();
    default: return new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString();
  }
}

export default function AssociateRunsView() {
  const [timeRange, setTimeRange] = useState<string>("24h");
  const [associateFilter, setAssociateFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  const since = useMemo(() => computeSince(timeRange), [timeRange]);

  const { data: actors } = useEntities("Actor", { type: "associate" });
  const associateNames = useMemo(() => {
    return (actors || [])
      .map((a) => String(a.name || ""))
      .filter(Boolean)
      .sort();
  }, [actors]);

  const { data: summaryData } = useActivitySummary({
    since,
    timeRange,
  });

  const { data: tableTraces, isLoading } = useTraces({
    since,
    associate_name: associateFilter !== "all" ? associateFilter : undefined,
    execution_status: statusFilter !== "all" ? statusFilter : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const tableItems = (tableTraces || []) as unknown as Record<string, unknown>[];
  const hasMore = tableItems.length === PAGE_SIZE;
  const totalCount = summaryData?.total_count ?? 0;
  const errorCount = summaryData?.error_count ?? 0;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Topbar */}
      <div className="h-14 bg-white border-b border-gray-200 flex items-center px-6 gap-4 flex-shrink-0">
        <h2 className="text-base font-semibold text-gray-900">Associate Runs</h2>

        <div className="flex gap-0.5 bg-gray-100 rounded-md p-0.5">
          {TIME_RANGES.map((r) => (
            <button
              key={r}
              onClick={() => { setTimeRange(r); setPage(0); }}
              className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                r === timeRange
                  ? "bg-white text-gray-900 shadow-sm"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {r}
            </button>
          ))}
        </div>

        <span className="text-sm text-gray-400">
          {totalCount.toLocaleString()} total{errorCount > 0 && ` · ${errorCount.toLocaleString()} errors`}
        </span>
      </div>

      <div className="flex flex-1 min-h-0">
        {/* Left column: chart (pinned) + filters (pinned) + table (scrolls) */}
        <div className="flex-1 flex flex-col min-w-0 min-h-0">
          {/* Chart — pinned */}
          <div className="flex-shrink-0 p-5 pb-0">
            <ActivityTimeline data={summaryData} timeRange={timeRange} />
          </div>

          {/* Filters — pinned */}
          <div className="flex-shrink-0 px-5 py-3">
            <div className="flex items-center gap-3">
              <select
                value={associateFilter}
                onChange={(e) => { setAssociateFilter(e.target.value); setPage(0); }}
                className="h-8 text-xs border border-gray-200 rounded-md px-2 bg-white text-gray-700"
              >
                <option value="all">All Associates</option>
                {associateNames.map((a) => (
                  <option key={a} value={a}>{a}</option>
                ))}
              </select>

              <select
                value={statusFilter}
                onChange={(e) => { setStatusFilter(e.target.value); setPage(0); }}
                className="h-8 text-xs border border-gray-200 rounded-md px-2 bg-white text-gray-700"
              >
                <option value="all">All Status</option>
                <option value="success">Success</option>
                <option value="error">Error</option>
              </select>
            </div>
          </div>

          {/* Table — scrolls independently */}
          <div className="flex-1 min-h-0 px-5 pb-5">
            <EntityTable
              columns={columns}
              data={tableItems}
              onRowClick={(row) => setSelectedTraceId(String(row._id || ""))}
              activeRowId={selectedTraceId}
              storageKey="associate-runs"
              pageIndex={page}
              hasNextPage={hasMore}
              hasPrevPage={page > 0}
              onNextPage={() => setPage((p) => p + 1)}
              onPrevPage={() => setPage((p) => Math.max(0, p - 1))}
              isLoading={isLoading}
              rowClassName={(row) => row.execution_status === "error" ? "bg-red-50" : ""}
            />
          </div>
        </div>

        {/* Detail panel — scrolls independently */}
        {selectedTraceId && (
          <RunDetailPanel traceId={selectedTraceId} onClose={() => setSelectedTraceId(null)} />
        )}
      </div>
    </div>
  );
}
