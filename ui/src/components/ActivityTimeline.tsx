import { useRef, useState, useEffect } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { associateColor } from "@/lib/colors";
import type { ActivitySummaryResponse } from "@/api/types";

const ERROR_COLOR = "#dc2626";

function formatBucketLabel(timestamp: string, timeRange: string): string {
  const d = new Date(timestamp);
  if (timeRange === "30d" || timeRange === "7d") {
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

interface ActivityTimelineProps {
  data: ActivitySummaryResponse | undefined;
  timeRange: string;
}

export function ActivityTimeline({ data, timeRange }: ActivityTimelineProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(600);

  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width);
      }
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  const buckets = data?.buckets ?? [];
  const associates = data?.associates ?? [];

  if (buckets.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-sm text-gray-400">
          No activity in this time range
        </CardContent>
      </Card>
    );
  }

  const svgHeight = 140;
  const chartTop = 10;
  const chartBottom = svgHeight - 24;
  const chartHeight = chartBottom - chartTop;
  const barGap = 2;
  const barWidth = Math.max(
    Math.min((containerWidth - barGap * buckets.length) / buckets.length, 40),
    4
  );
  const maxCount = buckets.reduce((m, b) => Math.max(m, b.total), 0);

  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-2">
          Activity — {timeRange}
        </div>
        <div ref={containerRef}>
          <svg width={containerWidth} height={svgHeight} className="block">
            {/* Y-axis guide lines */}
            {maxCount > 0 && [0.25, 0.5, 0.75, 1].map((frac) => {
              const y = chartBottom - frac * chartHeight;
              return (
                <g key={frac}>
                  <line x1={0} y1={y} x2={containerWidth} y2={y} stroke="#f3f4f6" strokeWidth={1} />
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
                const count = bucket.counts[assocName] || 0;
                if (count === 0) continue;
                const segHeight = maxCount > 0 ? (count / maxCount) * chartHeight : 0;
                const y = chartBottom - yOffset - segHeight;

                segments.push(
                  <rect
                    key={assocName}
                    x={x}
                    y={y}
                    width={barWidth}
                    height={segHeight}
                    fill={associateColor(assocName)}
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
              const labelWidth = 50;
              const maxLabels = Math.max(Math.floor(containerWidth / labelWidth), 2);
              const labelStep = Math.ceil(buckets.length / maxLabels);
              const showLabel = i % labelStep === 0;
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
                style={{ backgroundColor: associateColor(name) }}
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
