import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";
import type { ChangeRecord } from "../api/types";

interface Props {
  correlationId: string;
}

export function CascadeViewer({ correlationId }: Props) {
  const { data: changes } = useQuery({
    queryKey: ["cascade", correlationId],
    queryFn: () =>
      apiClient<ChangeRecord[]>(
        `/api/audit/changes?correlation_id=${correlationId}&limit=100`
      ),
    enabled: !!correlationId,
  });

  if (!changes?.length) {
    return <p className="text-sm text-gray-400">No cascade events</p>;
  }

  return (
    <div className="border rounded-lg overflow-x-auto">
      <table className="min-w-full divide-y divide-gray-200 text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">
              Time
            </th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">
              Entity
            </th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">
              Change
            </th>
            <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">
              Actor
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {changes.map((c) => (
            <tr key={c.id}>
              <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                {new Date(c.timestamp).toLocaleTimeString()}
              </td>
              <td className="px-3 py-2">
                {c.entity_type} {c.entity_id.slice(-6)}
              </td>
              <td className="px-3 py-2">
                <span className="px-1.5 py-0.5 bg-gray-100 rounded text-xs">
                  {c.change_type}
                </span>
              </td>
              <td className="px-3 py-2 font-mono text-gray-600">
                {c.actor_id.slice(-8)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
