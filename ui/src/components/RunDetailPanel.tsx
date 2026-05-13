import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useTraceDetail, useEvalForTrace, useEvaluatorTrace, useEntity } from "@/api/hooks";
import { TraceSteps } from "./TraceSteps";
import { EvalScores } from "./EvalScores";
import { ASSOCIATE_COLORS } from "./ActivityTimeline";

interface RunDetailPanelProps {
  traceId: string;
  onClose: () => void;
}

function formatDuration(ms: number | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTokens(n: number | undefined): string {
  if (n == null) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

function shortId(id: unknown): string {
  const s = String(id || "");
  return s.length > 12 ? s.slice(0, 6) + "…" + s.slice(-4) : s;
}

export function RunDetailPanel({ traceId, onClose }: RunDetailPanelProps) {
  const { data: trace, isLoading: traceLoading } = useTraceDetail(traceId);
  const { data: evalResults } = useEvalForTrace(traceId);
  const { data: evaluatorTrace } = useEvaluatorTrace(traceId);

  const evalResult = evalResults?.[0];
  const entityType = String(trace?.entity_type || "");
  const entityId = String(trace?.entity_id || "");
  const { data: entity } = useEntity(entityType, entityId);

  if (traceLoading || !trace) {
    return (
      <div className="w-[440px] flex-shrink-0 bg-white border-l border-gray-200 p-4 space-y-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const associateName = String(trace.associate_name || "Unknown");
  const colorKey = associateName.toLowerCase().replace(/\s+/g, "_");
  const color = ASSOCIATE_COLORS[colorKey] || ASSOCIATE_COLORS._default;
  const isError = trace.execution_status === "error";
  const isEvaluator = associateName === "Evaluator";
  const messages = (trace.messages || []) as Record<string, unknown>[];
  const evalMessages = evaluatorTrace
    ? ((evaluatorTrace.messages || []) as Record<string, unknown>[])
    : [];

  const entityState = String(entity?.status || entity?.execution_status || "");

  return (
    <div className="w-[440px] flex-shrink-0 bg-white border-l border-gray-200 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span
            className="px-1.5 py-0.5 rounded text-[10px] font-semibold text-white"
            style={{ backgroundColor: color }}
          >
            {associateName.split(" ").map((w) => w[0]).join("")}
          </span>
          <h3 className="text-sm font-semibold flex-1 truncate">{associateName}</h3>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-lg leading-none"
          >
            ×
          </button>
        </div>
        <div className="flex items-center gap-3 mt-1 text-xs text-gray-500 font-mono">
          <span>{formatDuration(trace.duration_ms as number)}</span>
          <span>{formatTokens(trace.total_tokens as number)} tok</span>
          <span>{messages.length} msgs</span>
          <Badge
            variant={isError ? "destructive" : "outline"}
            className={isError ? "text-[10px]" : "text-green-700 border-green-300 bg-green-50 text-[10px]"}
          >
            {isError ? "error" : "success"}
          </Badge>
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="trace" className="flex-1 flex flex-col overflow-hidden">
        <TabsList className="w-full justify-start rounded-none border-b bg-gray-50 px-2 h-9 flex-shrink-0">
          <TabsTrigger value="trace" className="text-xs">Trace</TabsTrigger>
          <TabsTrigger value="eval" className="text-xs">
            Evaluation
            {evalResult && (
              <Badge
                variant="outline"
                className={`ml-1.5 text-[9px] ${evalResult.passed ? "text-green-700 border-green-300" : "text-red-600 border-red-300"}`}
              >
                {evalResult.passed ? "pass" : "fail"}
              </Badge>
            )}
          </TabsTrigger>
          {!isEvaluator && (
            <TabsTrigger value="eval-trace" className="text-xs">Evaluator Trace</TabsTrigger>
          )}
        </TabsList>

        {/* TAB: Trace */}
        <TabsContent value="trace" className="flex-1 overflow-hidden m-0">
          <ScrollArea className="h-full">
            <div className="p-4 space-y-4">
              {/* Entity card */}
              <div>
                <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Entity Processed
                </span>
                <Card className="mt-1.5">
                  <CardContent className="p-3 space-y-1 text-xs">
                    <div className="flex gap-2">
                      <span className="text-gray-400 w-12">Type</span>
                      <span className="font-mono">{entityType}</span>
                    </div>
                    <div className="flex gap-2">
                      <span className="text-gray-400 w-12">ID</span>
                      <span className="font-mono text-indigo-600">{shortId(entityId)}</span>
                    </div>
                    {entityState && (
                      <div className="flex gap-2">
                        <span className="text-gray-400 w-12">Status</span>
                        <span className="font-medium">{String(entityState)}</span>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </div>

              <Separator />

              {/* Trace steps */}
              <div>
                <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  Trace — {messages.length} messages
                </span>
                <div className="mt-2">
                  <TraceSteps messages={messages} />
                </div>
              </div>

              <Separator />

              {/* References */}
              <div>
                <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                  References
                </span>
                <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  <span className="text-gray-400">Trace</span>
                  <span className="font-mono text-indigo-600">{shortId(trace._id)}</span>
                  <span className="text-gray-400">Correlation</span>
                  <span className="font-mono text-indigo-600">{shortId(trace.correlation_id)}</span>
                  <span className="text-gray-400">LangSmith</span>
                  <span className="font-mono text-indigo-600">{shortId(trace.langsmith_run_id)}</span>
                </div>
              </div>
            </div>
          </ScrollArea>
        </TabsContent>

        {/* TAB: Evaluation */}
        <TabsContent value="eval" className="flex-1 overflow-hidden m-0">
          <ScrollArea className="h-full">
            <div className="p-4">
              {evalResult ? (
                <EvalScores result={evalResult} />
              ) : (
                <p className="text-sm text-gray-400 py-8 text-center">
                  No evaluation result for this trace
                </p>
              )}
            </div>
          </ScrollArea>
        </TabsContent>

        {/* TAB: Evaluator Trace */}
        <TabsContent value="eval-trace" className="flex-1 overflow-hidden m-0">
          <ScrollArea className="h-full">
            <div className="p-4 space-y-4">
              {evaluatorTrace ? (
                <>
                  <div className="flex items-center gap-2">
                    <span
                      className="px-1.5 py-0.5 rounded text-[10px] font-semibold text-white"
                      style={{ backgroundColor: ASSOCIATE_COLORS.evaluator || "#B4AEFC" }}
                    >
                      Eval
                    </span>
                    <span className="text-xs text-gray-500 font-mono">
                      {formatTokens(evaluatorTrace.total_tokens as number)} tok ·{" "}
                      {formatDuration(evaluatorTrace.duration_ms as number)} ·{" "}
                      {evalMessages.length} msgs
                    </span>
                  </div>
                  <TraceSteps messages={evalMessages} />
                </>
              ) : (
                <p className="text-sm text-gray-400 py-8 text-center">
                  No evaluator trace found
                </p>
              )}
            </div>
          </ScrollArea>
        </TabsContent>
      </Tabs>
    </div>
  );
}
