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

export default function AssociateRunsView() {
  const [associateFilter, setAssociateFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [page, setPage] = useState(0);

  // Get all associate names from Actor entities — independent of current page
  const { data: actors } = useEntities("Actor", { type: "associate" });
  const associateNames = useMemo(() => {
    return (actors || [])
      .map((a) => String(a.name || ""))
      .filter(Boolean)
      .sort();
  }, [actors]);

  const { data: traces, isLoading } = useTraces({
    associate_name: associateFilter !== "all" ? associateFilter : undefined,
    execution_status: statusFilter !== "all" ? statusFilter : undefined,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });

  const items = traces || [];
  const hasMore = items.length === PAGE_SIZE;
  const errorCount = items.filter((t) => t.execution_status === "error").length;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Topbar */}
      <div className="h-14 bg-white border-b border-gray-200 flex items-center px-6 gap-4 flex-shrink-0">
        <h2 className="text-base font-semibold text-gray-900">Associate Runs</h2>
        <span className="text-sm text-gray-400">
          Page {page + 1}{errorCount > 0 && ` · ${errorCount} errors`}
        </span>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Main content */}
        <div className="flex-1 overflow-y-auto p-5 space-y-4">
          {/* Activity timeline */}
          <ActivityTimeline
            traces={items}
            onSelectTrace={setSelectedTraceId}
            selectedTraceId={selectedTraceId || undefined}
          />

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

          {/* Table */}
          <EntityTable
            columns={columns}
            data={items}
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
