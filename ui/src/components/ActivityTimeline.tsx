import { useMemo, useState } from "react";
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

const LANE_HEIGHT = 28;
const BLOCK_HEIGHT = 7;
const BLOCK_GAP = 2;
const MIN_BLOCK_WIDTH = 3;
const LABEL_WIDTH = 52;

interface TraceItem {
  _id: string;
  associate_name: string;
  start_time: string;
  duration_ms: number;
  execution_status: string;
  entity_type?: string;
  entity_id?: string;
}

interface ActivityTimelineProps {
  traces: Record<string, unknown>[];
  onSelectTrace?: (traceId: string) => void;
  selectedTraceId?: string;
}

function colorForAssociate(name: string): string {
  const key = name.toLowerCase().replace(/\s+/g, "_");
  return ASSOCIATE_COLORS[key] || ASSOCIATE_COLORS._default;
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

interface Block {
  trace: TraceItem;
  x: number;
  width: number;
  subLane: number;
}

function computeLaneBlocks(items: TraceItem[], minTime: number, timeRange: number, svgWidth: number): Block[] {
  const sorted = [...items].sort((a, b) => new Date(a.start_time).getTime() - new Date(b.start_time).getTime());
  const subLaneEnds: number[] = [];
  const blocks: Block[] = [];

  for (const trace of sorted) {
    const start = new Date(trace.start_time).getTime();
    const dur = Math.max(trace.duration_ms || 1000, 1000);
    const end = start + dur;

    const x = timeRange > 0 ? ((start - minTime) / timeRange) * svgWidth : 0;
    const width = Math.max(timeRange > 0 ? (dur / timeRange) * svgWidth : MIN_BLOCK_WIDTH, MIN_BLOCK_WIDTH);

    let assigned = -1;
    for (let i = 0; i < subLaneEnds.length; i++) {
      if (subLaneEnds[i] <= start) {
        assigned = i;
        subLaneEnds[i] = end;
        break;
      }
    }
    if (assigned === -1) {
      assigned = subLaneEnds.length;
      subLaneEnds.push(end);
    }

    blocks.push({ trace, x, width, subLane: assigned });
  }
  return blocks;
}

export function ActivityTimeline({ traces, onSelectTrace, selectedTraceId }: ActivityTimelineProps) {
  const items = useMemo(() => {
    return traces
      .filter((t) => t.start_time && t.associate_name)
      .map((t) => ({
        _id: String(t._id || ""),
        associate_name: String(t.associate_name),
        start_time: String(t.start_time),
        duration_ms: Number(t.duration_ms || 0),
        execution_status: String(t.execution_status || "success"),
        entity_type: t.entity_type ? String(t.entity_type) : undefined,
        entity_id: t.entity_id ? String(t.entity_id) : undefined,
      })) as TraceItem[];
  }, [traces]);

  const lanes = useMemo(() => {
    const nameSet = new Set(items.map((t) => t.associate_name));
    return Array.from(nameSet).sort();
  }, [items]);

  const { minTime, maxTime } = useMemo(() => {
    if (items.length === 0) return { minTime: 0, maxTime: 0 };
    const starts = items.map((t) => new Date(t.start_time).getTime());
    const ends = items.map((t) => new Date(t.start_time).getTime() + Math.max(t.duration_ms || 1000, 1000));
    return { minTime: Math.min(...starts), maxTime: Math.max(...ends) };
  }, [items]);

  const timeRange = maxTime - minTime || 60_000;
  const svgWidth = 800;
  const totalHeight = lanes.length * LANE_HEIGHT + 24;

  const timeLabels = useMemo(() => {
    if (items.length === 0) return [];
    const labels: { x: number; label: string }[] = [];
    const step = timeRange / 4;
    for (let i = 0; i <= 4; i++) {
      const t = minTime + step * i;
      labels.push({ x: (step * i / timeRange) * svgWidth, label: formatTime(new Date(t)) });
    }
    return labels;
  }, [items, minTime, timeRange, svgWidth]);

  if (items.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-sm text-gray-400">
          No traces to display
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mb-2">
          Activity
        </div>
        <div className="overflow-x-auto">
          <svg width={svgWidth + LABEL_WIDTH} height={totalHeight} className="block">
            {lanes.map((laneName, laneIdx) => {
              const laneItems = items.filter((t) => t.associate_name === laneName);
              const blocks = computeLaneBlocks(laneItems, minTime, timeRange, svgWidth);
              const y = laneIdx * LANE_HEIGHT;

              return (
                <g key={laneName}>
                  {/* Lane label */}
                  <text
                    x={LABEL_WIDTH - 4}
                    y={y + LANE_HEIGHT / 2}
                    textAnchor="end"
                    dominantBaseline="central"
                    className="fill-gray-400 text-[9px] font-semibold"
                    style={{ fontFamily: "JetBrains Mono, monospace", fontSize: "9px" }}
                  >
                    {laneName.split(/[\s_]+/).map((w) => w.slice(0, 3)).join("").slice(0, 6)}
                  </text>
                  {/* Lane background */}
                  <rect
                    x={LABEL_WIDTH}
                    y={y + 1}
                    width={svgWidth}
                    height={LANE_HEIGHT - 2}
                    rx={3}
                    fill="#fafafa"
                  />
                  {/* Blocks */}
                  {blocks.map((block) => {
                    const isError = block.trace.execution_status === "error";
                    const isSelected = block.trace._id === selectedTraceId;
                    const color = isError ? ERROR_COLOR : colorForAssociate(laneName);
                    const blockY = y + 2 + block.subLane * (BLOCK_HEIGHT + BLOCK_GAP);
                    const tooltip = `${block.trace.associate_name}\n${block.trace.entity_type || ""} · ${String(block.trace.entity_id || "").slice(0, 8)}\n${(block.trace.duration_ms / 1000).toFixed(1)}s · ${isError ? "error" : "success"}`;

                    return (
                      <rect
                        key={block.trace._id}
                        x={LABEL_WIDTH + block.x}
                        y={blockY}
                        width={block.width}
                        height={BLOCK_HEIGHT}
                        rx={2}
                        fill={color}
                        opacity={isSelected ? 1 : 0.85}
                        stroke={isSelected ? "#111827" : "none"}
                        strokeWidth={isSelected ? 1.5 : 0}
                        className="cursor-pointer hover:opacity-100"
                        onClick={() => onSelectTrace?.(block.trace._id)}
                      >
                        <title>{tooltip}</title>
                      </rect>
                    );
                  })}
                </g>
              );
            })}

            {/* Time axis */}
            {timeLabels.map((label, i) => (
              <text
                key={i}
                x={LABEL_WIDTH + label.x}
                y={totalHeight - 4}
                textAnchor="middle"
                className="fill-gray-400"
                style={{ fontFamily: "JetBrains Mono, monospace", fontSize: "9px" }}
              >
                {label.label}
              </text>
            ))}
          </svg>
        </div>

        {/* Legend */}
        <div className="flex flex-wrap gap-3 mt-2">
          {lanes.map((name) => (
            <div key={name} className="flex items-center gap-1">
              <div
                className="w-2 h-2 rounded-sm"
                style={{ backgroundColor: colorForAssociate(name) }}
              />
              <span className="text-[10px] text-gray-500">{name}</span>
            </div>
          ))}
          {items.some((t) => t.execution_status === "error") && (
            <div className="flex items-center gap-1">
              <div className="w-2 h-2 rounded-sm" style={{ backgroundColor: ERROR_COLOR }} />
              <span className="text-[10px] text-gray-500">Error</span>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
