import { useParams, useNavigate, Link } from "react-router-dom";
import { useEntityMeta } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { apiClient } from "../api/client";
import { EntityForm } from "../components/EntityForm";

export function EntityCreateView() {
  const { entityType } = useParams<{ entityType: string }>();
  const entityName = useEntityNameFromSlug(entityType) || "";
  const navigate = useNavigate();
  const { data: meta } = useEntityMeta(entityName);

  if (!meta) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        Loading...
      </div>
    );
  }

  // Build empty initial values from meta fields
  const emptyEntity: Record<string, unknown> = {};
  for (const field of meta.fields) {
    if (!field.name.startsWith("_")) {
      emptyEntity[field.name] = "";
    }
  }

  return (
    <div>
      <div className="mb-4">
        <Link
          to={`/${entityType}`}
          className="text-blue-600 hover:underline text-sm"
        >
          &larr; Back to {entityName} list
        </Link>
      </div>
      <h1 className="text-xl font-semibold mb-6">New {entityName}</h1>
      <div className="max-w-2xl">
        <EntityForm
          meta={meta}
          entity={emptyEntity}
          onSave={async (data) => {
            const created = await apiClient<Record<string, unknown>>(
              `/api/${entityType}/`,
              {
                method: "POST",
                body: JSON.stringify(data),
              }
            );
            navigate(`/${entityType}/${created._id}`);
          }}
        />
      </div>
    </div>
  );
}
