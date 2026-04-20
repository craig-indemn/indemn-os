import { useState } from "react";

interface Props {
  name: string;
  args: Record<string, unknown>;
}

export function CollapsibleToolCall({ name, args }: Props) {
  const [expanded, setExpanded] = useState(false);

  // Build a readable command summary
  const command = args.command ? String(args.command) : `${name} ${JSON.stringify(args)}`;
  const summary = command.length > 80 ? command.slice(0, 80) + "..." : command;

  return (
    <div className="border rounded bg-gray-50 text-xs font-mono">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-3 py-1.5 flex items-center gap-2 text-left text-gray-500 hover:text-gray-700"
      >
        <span className="text-[10px]">{expanded ? "\u25BC" : "\u25B6"}</span>
        <span className="truncate">{summary}</span>
      </button>
      {expanded && (
        <pre className="px-3 py-2 border-t text-gray-600 overflow-x-auto whitespace-pre-wrap">
          {JSON.stringify(args, null, 2)}
        </pre>
      )}
    </div>
  );
}
