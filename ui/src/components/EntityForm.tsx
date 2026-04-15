import { useForm } from "react-hook-form";
import { FormField } from "./FormField";
import type { EntityMeta } from "../api/types";

interface Props {
  meta: EntityMeta;
  entity: Record<string, unknown>;
  onSave: (data: Record<string, unknown>) => Promise<void>;
}

const SYSTEM_FIELDS = new Set([
  "org_id",
  "version",
  "created_at",
  "updated_at",
  "created_by",
]);

export function EntityForm({ meta, entity, onSave }: Props) {
  const {
    control,
    handleSubmit,
    formState: { isDirty, isSubmitting },
  } = useForm({ defaultValues: entity as Record<string, unknown> });

  const editableFields = meta.fields.filter(
    (f) => !f.name.startsWith("_") && !SYSTEM_FIELDS.has(f.name)
  );

  return (
    <form onSubmit={handleSubmit(onSave)} className="space-y-4">
      {editableFields.map((field) => (
        <div key={field.name}>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            {field.description || field.name.replace(/_/g, " ")}
            {field.required && <span className="text-red-500 ml-0.5">*</span>}
          </label>
          <FormField field={field} control={control} />
        </div>
      ))}
      {meta.permissions.write && (
        <button
          type="submit"
          disabled={!isDirty || isSubmitting}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 text-sm"
        >
          {isSubmitting ? "Saving..." : "Save"}
        </button>
      )}
    </form>
  );
}
