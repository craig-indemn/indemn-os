import { useAuthEvents } from "../api/hooks";

export function AuthEventsView() {
  const { data: events } = useAuthEvents({ limit: 100 });

  return (
    <div>
      <h1 className="text-xl font-semibold mb-4">Auth Events</h1>
      <div className="overflow-x-auto border rounded-lg">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Time
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Event
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Actor
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                Details
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {events?.map((event) => (
              <tr key={event.id}>
                <td className="px-4 py-3 text-sm text-gray-500 whitespace-nowrap">
                  {new Date(event.timestamp).toLocaleString()}
                </td>
                <td className="px-4 py-3 text-sm">
                  <span className="px-2 py-0.5 bg-gray-100 rounded text-xs font-mono">
                    {event.event_type}
                  </span>
                </td>
                <td className="px-4 py-3 text-sm text-gray-700 font-mono">
                  {event.actor_id.slice(-8)}
                </td>
                <td className="px-4 py-3 text-sm text-gray-500 max-w-xs truncate">
                  {JSON.stringify(event.metadata)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
