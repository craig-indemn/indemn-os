import { useController, type Control } from "react-hook-form";
import type { FieldMeta } from "../api/types";
import { EntityPicker } from "./EntityPicker";
import { MultiValueInput } from "./MultiValueInput";
import { JsonEditor } from "./JsonEditor";

interface Props {
  field: FieldMeta;
  control: Control<Record<string, unknown>>;
  rules?: Record<string, unknown>;
}

const baseClass =
  "w-full px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400";

/** Render the appropriate form control for a field type. [G-32] */
export function FormField({ field, control, rules }: Props) {
  const { field: formField } = useController({ name: field.name, control, rules });

  // Fields with enum_values render as dropdowns regardless of type
  if (field.enum_values?.length) {
    return (
      <select
        value={String(formField.value ?? "")}
        onChange={formField.onChange}
        onBlur={formField.onBlur}
        name={formField.name}
        className={baseClass}
      >
        <option value="">Select...</option>
        {field.enum_values.map((v) => (
          <option key={v} value={v}>
            {v.replace(/_/g, " ")}
          </option>
        ))}
      </select>
    );
  }

  switch (field.type) {
    case "str":
      return (
        <input
          type="text"
          value={String(formField.value ?? "")}
          onChange={formField.onChange}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
    case "int":
      return (
        <input
          type="number"
          step="1"
          value={formField.value as number ?? ""}
          onChange={(e) => formField.onChange(e.target.valueAsNumber)}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
    case "float":
    case "decimal":
      return (
        <input
          type="number"
          step="0.01"
          value={formField.value as number ?? ""}
          onChange={(e) => formField.onChange(e.target.valueAsNumber)}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
    case "bool":
      return (
        <input
          type="checkbox"
          checked={!!formField.value}
          onChange={formField.onChange}
          onBlur={formField.onBlur}
          name={formField.name}
          className="h-4 w-4"
        />
      );
    case "date":
      return (
        <input
          type="date"
          value={String(formField.value ?? "")}
          onChange={formField.onChange}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
    case "datetime":
      return (
        <input
          type="datetime-local"
          value={String(formField.value ?? "")}
          onChange={formField.onChange}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
    // "enum" type: handled by the enum_values check above the switch.
    // Falls through to default (text input) for type="enum" without enum_values.
    case "enum":
    case "objectid":
      return (
        <EntityPicker
          entityType={field.relationship_target || ""}
          value={String(formField.value ?? "")}
          onChange={formField.onChange}
          name={formField.name}
        />
      );
    case "list":
      return (
        <MultiValueInput
          value={(formField.value as string[]) ?? []}
          onChange={formField.onChange}
          name={formField.name}
        />
      );
    case "dict":
      return (
        <JsonEditor
          value={
            typeof formField.value === "string"
              ? formField.value
              : JSON.stringify(formField.value ?? {}, null, 2)
          }
          onChange={formField.onChange}
          name={formField.name}
        />
      );
    default:
      return (
        <input
          type="text"
          value={String(formField.value ?? "")}
          onChange={formField.onChange}
          onBlur={formField.onBlur}
          name={formField.name}
          className={baseClass}
        />
      );
  }
}
