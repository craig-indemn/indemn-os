import { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";

export const ASSOCIATE_COLORS: Record<string, string> = {
  email_classifier: "#6C63FF",
  evaluator: "#B4AEFC",
  touchpoint_synthesizer: "#1B8F5A",
  intelligence_extractor: "#C4880A",
  meeting_classifier: "#D64545",
  slack_classifier: "#0891b2",
  company_enricher: "#7c3aed",
  proposal_hydrator: "#db2777",
  email_fetcher: "#64748b",
  meeting_fetcher: "#64748b",
  drive_fetcher: "#64748b",
  slack_fetcher: "#64748b",
  _default: "#9ca3af",
};

const ERROR_COLOR = "#dc2626";

const BUCKET_MS: Record<string, number> = {
  "1h": 5 * 60 * 1000,
  "6h": 15 * 60 * 1000,
  "24h": 60 * 60 * 1000,
  "7d": 4 * 60 * 60 * 1000,
  "30d": 24 * 60 * 60 * 1000,
};

function colorForAssociate(name: string): string {
  const key = name.toLowerCase().replace(/\s+/g, "_");
  return ASSOCIATE_COLORS[key] || ASSOCIATE_COLORS._default;
}

function formatBucketLabel(timestamp: number, timeRange: string): string {
  const d = new Date(timestamp);
  if (timeRange === "30d" || timeRange === "7d") {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

interface BucketData {
  timestamp: number;
  counts: Map<string, number>;
  errors: number;
  total: number;
}

interface ActivityTimelineProps {
  traces: Record<string, unknown>[];
  timeRange: string;
}

export function ActivityTimeline({ traces, timeRange }: ActivityTimelineProps) {
  const bucketMs = BUCKET_MS[timeRange] || BUCKET_MS["24h"];

  const { buckets, associates, maxCount } = useMemo(() => {
    const bucketMap = new Map<number, BucketData>();
    const assocSet = new Set<string>();

    for (const trace of traces) {
      const startTime = trace.start_time as string;
      if (!startTime) continue;
      const t = new Date(startTime).getTime();
      const bucketKey = Math.floor(t / bucketMs) * bucketMs;
      const assocName = String(trace.associate_name || "Unknown");
      const isError = trace.execution_status === "error";

      assocSet.add(assocName);

      let bucket = bucketMap.get(bucketKey);
      if (!bucket) {
        bucket = { timestamp: bucketKey, counts: new Map(), errors: 0, total: 0 };
        bucketMap.set(bucketKey, bucket);
      }
      bucket.counts.set(assocName, (bucket.counts.get(assocName) || 0) + 1);
      bucket.total += 1;
      if (isError) bucket.errors += 1;
    }

    const sorted = Array.from(bucketMap.values()).sort((a, b) => a.timestamp - b.timestamp);
    const max = sorted.reduce((m, b) => Math.max(m, b.total), 0);
    return {
      buckets: sorted,
      associates: Array.from(assocSet).sort(),
      maxCount: max,
    };
  }, [traces, bucketMs]);

  if (traces.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-sm text-gray-400">
          No activity in this time range
        </CardContent>
      </Card>
    );
  }

  const svgWidth = 800;
  const svgHeight = 140;
  const chartTop = 10;
  const chartBottom = svgHeight - 24;
  const chartHeight = chartBottom - chartTop;
  const barGap = 2;
  const barWidth = buckets.length > 0
    ? Math.max(Math.min((svgWidth - barGap * buckets.length) / buckets.length, 40), 4)
    : 20;
  const totalWidth = buckets.length * (barWidth + barGap);
  const displayWidth = Math.max(totalWidth, svgWidth);

  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-2">
          Activity — {timeRange}
        </div>
        <div className="overflow-x-auto">
          <svg width={displayWidth} height={svgHeight} className="block">
            {/* Y-axis guide lines */}
            {maxCount > 0 && [0.25, 0.5, 0.75, 1].map((frac) => {
              const y = chartBottom - frac * chartHeight;
              return (
                <g key={frac}>
                  <line x1={0} y1={y} x2={displayWidth} y2={y} stroke="#f3f4f6" strokeWidth={1} />
                  <text x={2} y={y - 2} fill="#d1d5db" style={{ fontSize: "8px", fontFamily: "JetBrains Mono, monospace" }}>
                    {Math.round(maxCount * frac)}
                  </text>
                </g>
              );
            })}

            {/* Bars */}
            {buckets.map((bucket, i) => {
              const x = i * (barWidth + barGap);
              let yOffset = 0;

              const segments: React.ReactNode[] = [];

              for (const assocName of associates) {
                const count = bucket.counts.get(assocName) || 0;
                if (count === 0) continue;
                const segHeight = maxCount > 0 ? (count / maxCount) * chartHeight : 0;
                const y = chartBottom - yOffset - segHeight;
                const color = colorForAssociate(assocName);

                segments.push(
                  <rect
                    key={assocName}
                    x={x}
                    y={y}
                    width={barWidth}
                    height={segHeight}
                    fill={color}
                    rx={1}
                    opacity={0.85}
                  >
                    <title>{`${assocName}: ${count} runs`}</title>
                  </rect>
                );
                yOffset += segHeight;
              }

              if (bucket.errors > 0) {
                const errHeight = maxCount > 0 ? (bucket.errors / maxCount) * chartHeight : 0;
                segments.push(
                  <rect
                    key="error-overlay"
                    x={x}
                    y={chartBottom - errHeight}
                    width={barWidth}
                    height={errHeight}
                    fill={ERROR_COLOR}
                    rx={1}
                    opacity={0.4}
                  >
                    <title>{`${bucket.errors} errors`}</title>
                  </rect>
                );
              }

              return <g key={bucket.timestamp}>{segments}</g>;
            })}

            {/* X-axis labels */}
            {buckets.map((bucket, i) => {
              const x = i * (barWidth + barGap) + barWidth / 2;
              const showLabel = buckets.length <= 30 || i % Math.ceil(buckets.length / 15) === 0;
              if (!showLabel) return null;
              return (
                <text
                  key={`label-${i}`}
                  x={x}
                  y={svgHeight - 4}
                  textAnchor="middle"
                  fill="#9ca3af"
                  style={{ fontSize: "8px", fontFamily: "JetBrains Mono, monospace" }}
                >
                  {formatBucketLabel(bucket.timestamp, timeRange)}
                </text>
              );
            })}
          </svg>
        </div>

        {/* Legend */}
        <div className="flex flex-wrap gap-3 mt-2">
          {associates.map((name) => (
            <div key={name} className="flex items-center gap-1">
              <div
                className="w-2 h-2 rounded-sm"
                style={{ backgroundColor: colorForAssociate(name) }}
              />
              <span className="text-[10px] text-gray-500">{name}</span>
            </div>
          ))}
          {buckets.some((b) => b.errors > 0) && (
            <div className="flex items-center gap-1">
              <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: ERROR_COLOR, opacity: 0.4 }} />
              <span className="text-[10px] text-gray-500">Errors</span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
