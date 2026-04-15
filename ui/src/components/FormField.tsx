import type { UseFormRegister } from "react-hook-form";
import type { FieldMeta } from "../api/types";

interface Props {
  field: FieldMeta;
  register: UseFormRegister<Record<string, unknown>>;
}

export function FormField({ field, register }: Props) {
  const baseClass = "w-full px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400";

  switch (field.type) {
    case "str":
      return <input type="text" {...register(field.name)} className={baseClass} />;
    case "int":
      return <input type="number" step="1" {...register(field.name, { valueAsNumber: true })} className={baseClass} />;
    case "float":
    case "decimal":
      return <input type="number" step="0.01" {...register(field.name, { valueAsNumber: true })} className={baseClass} />;
    case "bool":
      return <input type="checkbox" {...register(field.name)} className="h-4 w-4" />;
    case "date":
      return <input type="date" {...register(field.name)} className={baseClass} />;
    case "datetime":
      return <input type="datetime-local" {...register(field.name)} className={baseClass} />;
    case "enum":
      return (
        <select {...register(field.name)} className={baseClass}>
          <option value="">Select...</option>
          {field.enum_values?.map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
      );
    case "objectid":
      return <input type="text" {...register(field.name)} className={`${baseClass} font-mono`} placeholder="ObjectId" />;
    case "dict":
      return <textarea {...register(field.name)} className={`${baseClass} font-mono text-xs`} rows={3} placeholder="{}" />;
    default:
      return <input type="text" {...register(field.name)} className={baseClass} />;
  }
}
