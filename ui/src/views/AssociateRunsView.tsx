import { useState, useMemo } from "react";
import { type ColumnDef } from "@tanstack/react-table";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { useTraces, useEntities } from "@/api/hooks";
import { ActivityTimeline, ASSOCIATE_COLORS } from "@/components/ActivityTimeline";
import { RunDetailPanel } from "@/components/RunDetailPanel";
import { EntityTable } from "@/components/EntityTable";

const PAGE_SIZE = 25;

function formatTime(iso: unknown): string {
  if (!iso) return "—";
  const d = new Date(String(iso));
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(ms: unknown): string {
  if (ms == null) return "—";
  const n = Number(ms);
  if (isNaN(n)) return "—";
  if (n < 1000) return `${n}ms`;
  return `${(n / 1000).toFixed(1)}s`;
}

function formatTokens(n: unknown): string {
  if (n == null) return "0";
  const v = Number(n);
  if (isNaN(v)) return "0";
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1000) return `${Math.round(v / 1000)}K`;
  return String(v);
}

function shortId(id: unknown): string {
  const s = String(id || "");
  return s.length > 12 ? s.slice(0, 6) + "…" + s.slice(-4) : s;
}

function associateAbbrev(name: string): string {
  return name.split(" ").map((w) => w[0]).join("").toUpperCase();
}

function associateColor(name: string): string {
  const key = name.toLowerCase().replace(/\s+/g, "_");
  return ASSOCIATE_COLORS[key] || ASSOCIATE_COLORS._default;
}

function evalBadge(feedbackStats: unknown): React.ReactNode {
  if (!feedbackStats || typeof feedbackStats !== "object") return <span className="text-gray-300 text-xs">—</span>;
  const stats = feedbackStats as Record<string, { passed?: boolean }>;
  const entries = Object.entries(stats).filter(([k]) => k !== "evaluation_passed");
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
      <span className="font-mono text-xs text-gray-500">{formatTime(getValue())}</span>
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
    accessorFn: (row) => `${row.entity_type || ""} · ${shortId(row.entity_id)}`,
    cell: ({ row: r }) => (
      <div>
        <span className="font-mono text-xs">
          {String(r.original.entity_type || "")} · {shortId(r.original.entity_id)}
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
      <span className="font-mono text-xs text-gray-600">{formatDuration(getValue())}</span>
    ),
    enableColumnFilter: false,
  },
  {
    id: "tokens",
    header: "Tokens",
    accessorFn: (row) => row.total_tokens,
    cell: ({ getValue }) => (
      <span className="font-mono text-xs text-gray-600">{formatTokens(getValue())}</span>
    ),
    enableColumnFilter: false,
  },
  {
    id: "eval",
    header: "Eval",
    accessorFn: (row) => row.feedback_stats,
    cell: ({ getValue }) => evalBadge(getValue()),
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

  // Chart data — all runs in window, independent of table filters/pagination
  const { data: chartTraces } = useTraces({
    since,
    limit: 100,
  });

  // Table data — paginated, filtered within same window
  const { data: tableTraces, isLoading } = useTraces({
    since,
    associate_name: associateFilter !== "all" ? associateFilter : undefined,
    execution_status: statusFilter !== "all" ? statusFilter : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const chartItems = chartTraces || [];
  const tableItems = tableTraces || [];
  const hasMore = tableItems.length === PAGE_SIZE;
  const errorCount = tableItems.filter((t) => t.execution_status === "error").length;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Topbar */}
      <div className="h-14 bg-white border-b border-gray-200 flex items-center px-6 gap-4 flex-shrink-0">
        <h2 className="text-base font-semibold text-gray-900">Associate Runs</h2>

        {/* Time range selector */}
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
          {chartItems.length} total{errorCount > 0 && ` · ${errorCount} errors on page`}
        </span>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Main content */}
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {/* Activity timeline — shows ALL runs in window */}
          <ActivityTimeline traces={chartItems} timeRange={timeRange} />

          {/* Filters */}
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

          <Separator />

          {/* Table — paginated within time window */}
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

        {/* Detail panel */}
        {selectedTraceId && (
          <RunDetailPanel traceId={selectedTraceId} onClose={() => setSelectedTraceId(null)} />
        )}
      </div>
    </div>
  );
}
