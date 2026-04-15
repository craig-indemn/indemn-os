import { useEntities, useQueueDepth } from "../api/hooks";

export function RoleOverview() {
  const { data: roles } = useEntities("Role");
  const { data: queueDepth } = useQueueDepth();

  return (
    <div>
      <h1 className="text-xl font-semibold mb-4">Roles</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {roles?.map((role) => (
          <div
            key={String(role._id)}
            className="bg-white p-4 rounded-lg border"
          >
            <h3 className="font-medium text-gray-900">{String(role.name)}</h3>
            <div className="mt-2 text-sm text-gray-500">
              Queue: {queueDepth?.[String(role.name)] ?? 0} pending
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
