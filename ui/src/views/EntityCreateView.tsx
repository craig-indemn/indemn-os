import { useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { useEntityMeta } from "../api/hooks";
import { useEntityNameFromSlug } from "../hooks/useEntityMeta";
import { apiClient } from "../api/client";
import { EntityForm } from "../components/EntityForm";
import { useToast } from "../context/ToastContext";

export function EntityCreateView() {
  const { entityType } = useParams<{ entityType: string }>();
  const entityName = useEntityNameFromSlug(entityType) || "";
  const navigate = useNavigate();
  const { toast } = useToast();
  const { data: meta } = useEntityMeta(entityName);

  useEffect(() => { document.title = `New ${entityName} - Indemn OS`; }, [entityName]);

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
    if (!field.name.startsWith("_") && !["org_id", "version", "created_at", "updated_at", "created_by"].includes(field.name)) {
      emptyEntity[field.name] = field.default != null ? field.default : "";
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
          isCreate
          onSave={async (data) => {
            const created = await apiClient<Record<string, unknown>>(
              `/api/${entityType}/`,
              {
                method: "POST",
                body: JSON.stringify(data),
              }
            );
            toast(`${entityName} created`, "success");
            navigate(`/${entityType}/${created._id}`);
          }}
        />
      </div>
    </div>
  );
}
