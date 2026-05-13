import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { TraceMessage, ToolCall } from "@/api/types";

interface TraceStepsProps {
  messages: TraceMessage[];
  maxVisible?: number;
}

function extractCommand(args: Record<string, unknown>): string {
  if (typeof args.command === "string") return args.command;
  return JSON.stringify(args);
}

function parseExitStatus(content: string): { text: string; success: boolean | null } {
  const successMatch = content.match(/\[Command succeeded with exit code (\d+)\]/);
  if (successMatch) {
    const cleaned = content.replace(/\n?\[Command succeeded with exit code \d+\]/, "").trim();
    return { text: cleaned, success: true };
  }
  const failMatch = content.match(/\[Command failed with exit code (\d+)\]/);
  if (failMatch) {
    const cleaned = content.replace(/\n?\[Command failed with exit code \d+\]/, "").trim();
    return { text: cleaned, success: false };
  }
  return { text: content, success: null };
}

function truncate(text: string, max: number): { truncated: string; wasTruncated: boolean } {
  if (text.length <= max) return { truncated: text, wasTruncated: false };
  return { truncated: text.slice(0, max), wasTruncated: true };
}

function HumanStep({ content, index }: { content: string; index: number }) {
  return (
    <Collapsible>
      <div className="border rounded px-3 py-2 bg-gray-50 text-sm">
        <CollapsibleTrigger className="w-full text-left">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
            MSG {index} [human] — {content.length.toLocaleString()} chars
          </span>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ScrollArea className="max-h-48 mt-2">
            <pre className="text-xs text-gray-500 font-mono whitespace-pre-wrap break-all">
              {content}
            </pre>
          </ScrollArea>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

function ToolCallStep({ call, index }: { call: ToolCall; index: number }) {
  const isHarness = call.name === "write_todos";
  const cmd = call.name === "execute" ? extractCommand(call.args) : JSON.stringify(call.args);

  if (isHarness) {
    return (
      <div className="border rounded px-3 py-2 bg-gray-50 opacity-40 text-sm">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-300">
          MSG {index} [write_todos]
        </span>
        <code className="block text-xs font-mono text-gray-400 mt-0.5 break-all">
          {cmd.slice(0, 100)}
        </code>
      </div>
    );
  }

  return (
    <div className="border border-blue-200 rounded px-3 py-2 bg-blue-50 text-sm">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-blue-600">
        MSG {index} [ai → {call.name}]
      </span>
      <code className="block text-xs font-mono text-gray-900 mt-0.5 break-all">{cmd}</code>
    </div>
  );
}

function ToolResultStep({
  content,
  name,
  status,
  index,
}: {
  content: string;
  name: string;
  status?: string;
  index: number;
}) {
  const isHarness = name === "write_todos";
  const isError = status === "error" || content.includes("[Command failed");
  const parsed = parseExitStatus(content);
  const { truncated, wasTruncated } = truncate(parsed.text, 500);
  const [expanded, setExpanded] = useState(isError);

  if (isHarness) {
    return (
      <div className="border rounded px-3 py-2 bg-gray-50 opacity-40 text-sm">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-300">
          MSG {index} [write_todos result]
        </span>
      </div>
    );
  }

  const borderClass = isError ? "border-red-300 bg-red-50" : "border-blue-100 bg-sky-50";
  const labelClass = isError ? "text-red-600" : "text-blue-600";

  return (
    <div className={`border rounded px-3 py-2 text-sm ${borderClass}`}>
      <div className="flex items-center gap-2">
        <span className={`text-[10px] font-semibold uppercase tracking-wide ${labelClass}`}>
          MSG {index} [tool: {name}]
        </span>
        {parsed.success === true && (
          <Badge variant="outline" className="text-[10px] text-green-700 border-green-300 bg-green-50">
            exit 0
          </Badge>
        )}
        {parsed.success === false && (
          <Badge variant="outline" className="text-[10px] text-red-700 border-red-300 bg-red-50">
            failed
          </Badge>
        )}
        {content.includes("[stderr]") && !isError && (
          <Badge variant="outline" className="text-[10px] text-amber-700 border-amber-300 bg-amber-50">
            stderr
          </Badge>
        )}
      </div>
      <pre className="text-xs font-mono text-gray-600 mt-1 whitespace-pre-wrap break-all">
        {expanded ? parsed.text : truncated}
      </pre>
      {wasTruncated && !expanded && (
        <button
          onClick={() => setExpanded(true)}
          className="text-xs text-indigo-600 hover:underline mt-1"
        >
          Show full output ({parsed.text.length.toLocaleString()} chars)
        </button>
      )}
      {wasTruncated && expanded && (
        <button
          onClick={() => setExpanded(false)}
          className="text-xs text-indigo-600 hover:underline mt-1"
        >
          Collapse
        </button>
      )}
    </div>
  );
}

function AiTextStep({ content, index }: { content: string; index: number }) {
  return (
    <div className="border rounded px-3 py-2 bg-gray-50 text-sm">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
        MSG {index} [ai]
      </span>
      <p className="text-sm text-gray-700 mt-0.5">{content}</p>
    </div>
  );
}

export function TraceSteps({ messages, maxVisible = 50 }: TraceStepsProps) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? messages : messages.slice(0, maxVisible);

  const steps: React.ReactNode[] = [];
  let stepIndex = 0;

  for (const msg of visible) {
    const i = stepIndex++;

    if (msg.type === "human") {
      steps.push(<HumanStep key={i} content={String(msg.content || "")} index={i} />);
      continue;
    }

    if (msg.type === "ai") {
      const toolCalls = msg.tool_calls || [];
      const contentText = msg.content_text || (typeof msg.content === "string" ? msg.content : "");

      if (toolCalls.length > 0) {
        for (let j = 0; j < toolCalls.length; j++) {
          steps.push(<ToolCallStep key={`${i}-tc-${j}`} call={toolCalls[j]} index={i} />);
        }
        if (contentText && toolCalls.every((tc) => tc.name !== "write_todos")) {
          steps.push(<AiTextStep key={`${i}-text`} content={contentText} index={i} />);
        }
      } else if (contentText) {
        steps.push(<AiTextStep key={i} content={contentText} index={i} />);
      }
      continue;
    }

    if (msg.type === "tool") {
      steps.push(
        <ToolResultStep
          key={i}
          content={String(msg.content || "")}
          name={String(msg.name || "?")}
          status={msg.status}
          index={i}
        />
      );
      continue;
    }

    steps.push(
      <div key={i} className="border rounded px-3 py-2 bg-gray-100 text-sm opacity-50">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
          MSG {i} [{msg.type}]
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {steps}
      {!showAll && messages.length > maxVisible && (
        <button
          onClick={() => setShowAll(true)}
          className="text-xs text-indigo-600 hover:underline py-2"
        >
          Show all {messages.length} messages ({maxVisible} shown)
        </button>
      )}
    </div>
  );
}
