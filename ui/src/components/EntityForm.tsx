import { useForm } from "react-hook-form";
import { FormField } from "./FormField";
import type { EntityMeta } from "../api/types";

interface Props {
  meta: EntityMeta;
  entity: Record<string, unknown>;
  onSave: (data: Record<string, unknown>) => Promise<void>;
  isCreate?: boolean;
}

const SYSTEM_FIELDS = new Set([
  "org_id",
  "version",
  "created_at",
  "updated_at",
  "created_by",
]);

export function EntityForm({ meta, entity, onSave, isCreate }: Props) {
  const {
    control,
    handleSubmit,
    formState: { isDirty, isSubmitting, errors },
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
          <FormField
            field={field}
            control={control}
            rules={field.required ? { required: `${field.name.replace(/_/g, " ")} is required` } : undefined}
          />
          {errors[field.name] && (
            <p className="text-red-500 text-xs mt-1">
              {String(errors[field.name]?.message || "Required")}
            </p>
          )}
        </div>
      ))}
      {meta.permissions.write && (
        <button
          type="submit"
          disabled={(!isDirty && !isCreate) || isSubmitting}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 text-sm"
        >
          {isSubmitting ? "Saving..." : "Save"}
        </button>
      )}
    </form>
  );
}
