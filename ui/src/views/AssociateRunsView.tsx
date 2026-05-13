import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useTraces } from "@/api/hooks";
import { ActivityTimeline, ASSOCIATE_COLORS } from "@/components/ActivityTimeline";
import { RunDetailPanel } from "@/components/RunDetailPanel";

function formatTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
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
  return String(id || "").slice(0, 8);
}

function associateAbbrev(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase();
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

export default function AssociateRunsView() {
  const [associateFilter, setAssociateFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);

  const { data: traces, isLoading } = useTraces({
    associate_name: associateFilter !== "all" ? associateFilter : undefined,
    execution_status: statusFilter !== "all" ? statusFilter : undefined,
    limit: 100,
  });

  const items = traces || [];
  const associates = Array.from(new Set(items.map((t) => String(t.associate_name || "")))).sort();
  const errorCount = items.filter((t) => t.execution_status === "error").length;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Topbar */}
      <div className="h-14 bg-white border-b border-gray-200 flex items-center px-6 gap-4 flex-shrink-0">
        <h2 className="text-base font-semibold text-gray-900">Associate Runs</h2>
        <span className="text-sm text-gray-400">
          {items.length} runs{errorCount > 0 && ` · ${errorCount} errors`}
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
            <Select value={associateFilter} onValueChange={(v) => setAssociateFilter(v || "all")}>
              <SelectTrigger className="w-44 h-8 text-xs">
                <SelectValue placeholder="All Associates" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Associates</SelectItem>
                {associates.map((a) => (
                  <SelectItem key={a} value={a}>
                    {a}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v || "all")}>
              <SelectTrigger className="w-32 h-8 text-xs">
                <SelectValue placeholder="All Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Status</SelectItem>
                <SelectItem value="success">Success</SelectItem>
                <SelectItem value="error">Error</SelectItem>
              </SelectContent>
            </Select>

            <span className="ml-auto text-xs text-gray-400">{items.length} runs</span>
          </div>

          <Separator />

          {/* Table */}
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="text-[10px] uppercase tracking-wide w-20">Time</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide w-16">Assoc</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide">Entity</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide w-20">Status</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide w-16">Duration</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide w-16">Tokens</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wide w-16">Eval</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-sm text-gray-400 py-8">
                      Loading...
                    </TableCell>
                  </TableRow>
                )}
                {!isLoading && items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-sm text-gray-400 py-8">
                      No traces found
                    </TableCell>
                  </TableRow>
                )}
                {items.map((trace) => {
                  const id = String(trace._id || "");
                  const isError = trace.execution_status === "error";
                  const isSelected = id === selectedTraceId;
                  const assocName = String(trace.associate_name || "");

                  return (
                    <TableRow
                      key={id}
                      className={`cursor-pointer ${isError ? "bg-red-50 hover:bg-red-100" : isSelected ? "bg-blue-50" : "hover:bg-gray-50"}`}
                      onClick={() => setSelectedTraceId(id)}
                    >
                      <TableCell className="font-mono text-xs text-gray-500 py-2">
                        {formatTime(String(trace.start_time || ""))}
                      </TableCell>
                      <TableCell className="py-2">
                        <span
                          className="px-1.5 py-0.5 rounded text-[10px] font-semibold text-white"
                          style={{ backgroundColor: associateColor(assocName) }}
                        >
                          {associateAbbrev(assocName)}
                        </span>
                      </TableCell>
                      <TableCell className="py-2">
                        <div className="font-mono text-xs">
                          {String(trace.entity_type || "")} · {shortId(trace.entity_id)}
                        </div>
                        {isError && trace.error ? (
                          <div className="text-xs text-red-600 truncate max-w-[300px]">
                            {String(trace.error).slice(0, 80)}
                          </div>
                        ) : null}
                      </TableCell>
                      <TableCell className="py-2">
                        <Badge
                          variant={isError ? "destructive" : "outline"}
                          className={isError ? "text-[10px]" : "text-green-700 border-green-300 bg-green-50 text-[10px]"}
                        >
                          {isError ? "error" : "success"}
                        </Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-gray-600 py-2">
                        {formatDuration(trace.duration_ms)}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-gray-600 py-2">
                        {formatTokens(trace.total_tokens)}
                      </TableCell>
                      <TableCell className="py-2">
                        {evalBadge(trace.feedback_stats)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </div>

        {/* Detail panel */}
        {selectedTraceId && (
          <RunDetailPanel traceId={selectedTraceId} onClose={() => setSelectedTraceId(null)} />
        )}
      </div>
    </div>
  );
}
