import { useState, useRef, useEffect } from "react";
import type { FieldMeta } from "../api/types";
import { FieldRenderer } from "./FieldRenderer";
import { EntityPicker } from "./EntityPicker";

interface Props {
  field: FieldMeta;
  value: unknown;
  onSave: (value: unknown) => Promise<void>;
  canEdit: boolean;
}

/**
 * Inline-editable field: renders as formatted read-only text by default.
 * Click to edit — shows the appropriate input with accept (✓) and cancel (✕) buttons.
 */
export function InlineField({ field, value, onSave, canEdit }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<unknown>(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(null);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
    }
  }, [editing]);

  // Reset draft when value changes externally
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  const startEdit = () => {
    if (!canEdit) return;
    setDraft(value);
    setEditing(true);
  };

  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };

  const accept = async () => {
    if (draft === value) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(draft);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") cancel();
    if (e.key === "Enter" && !e.shiftKey && field.type !== "str") {
      e.preventDefault();
      accept();
    }
  };

  // State field — not inline editable (transitions handle this)
  if (field.is_state_field) {
    return (
      <div className="text-sm">
        <FieldRenderer type={field.type} value={value} meta={field} />
      </div>
    );
  }

  // Read mode
  if (!editing) {
    const isEmpty = value === null || value === undefined || value === "";
    const long = isLongText(field, value);

    // Long text: show truncated with expand
    if (!isEmpty && long) {
      return <ExpandableText value={String(value)} onEdit={canEdit ? startEdit : undefined} />;
    }

    return (
      <div
        onClick={canEdit ? startEdit : undefined}
        className={`text-sm rounded px-2 py-1.5 -mx-2 min-h-[32px] flex items-start ${
          canEdit ? "cursor-pointer hover:bg-gray-50 group" : ""
        }`}
      >
        {isEmpty ? (
          <span className="text-gray-300 italic">{canEdit ? "Click to add..." : "—"}</span>
        ) : (
          <FieldRenderer type={field.enum_values?.length ? "enum" : field.type} value={value} meta={field} />
        )}
        {canEdit && !isEmpty && (
          <span className="ml-2 text-gray-300 opacity-0 group-hover:opacity-100 text-xs shrink-0">✎</span>
        )}
      </div>
    );
  }

  // Edit mode
  return (
    <div className="flex items-start gap-2">
      <div className="flex-1">{renderEditInput(field, draft, setDraft, handleKeyDown, inputRef)}</div>
      <div className="flex gap-1 pt-1 shrink-0">
        <button
          onClick={accept}
          disabled={saving}
          className="w-7 h-7 rounded bg-green-50 text-green-600 hover:bg-green-100 flex items-center justify-center text-sm font-bold border border-green-200"
          title="Accept"
        >
          ✓
        </button>
        <button
          onClick={cancel}
          disabled={saving}
          className="w-7 h-7 rounded bg-red-50 text-red-400 hover:bg-red-100 flex items-center justify-center text-sm font-bold border border-red-200"
          title="Cancel"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

function isLongText(field: FieldMeta, value: unknown): boolean {
  if (field.type !== "str" || field.enum_values?.length) return false;
  const s = String(value || "");
  return s.length > 60 || s.includes("\n");
}

function renderEditInput(
  field: FieldMeta,
  draft: unknown,
  setDraft: (v: unknown) => void,
  onKeyDown: (e: React.KeyboardEvent) => void,
  ref: React.RefObject<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null>,
) {
  const baseClass = "w-full px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400 focus:border-blue-400";

  // Enum dropdown
  if (field.enum_values?.length) {
    return (
      <select
        ref={ref as React.RefObject<HTMLSelectElement>}
        value={String(draft ?? "")}
        onChange={(e) => setDraft(e.target.value || null)}
        onKeyDown={onKeyDown}
        className={baseClass}
      >
        <option value="">—</option>
        {field.enum_values.map((v) => (
          <option key={v} value={v}>{v.replace(/_/g, " ")}</option>
        ))}
      </select>
    );
  }

  // Relationship picker
  if (field.type === "objectid" && field.relationship_target) {
    return (
      <EntityPicker
        entityType={field.relationship_target}
        value={String(draft ?? "")}
        onChange={(v) => setDraft(v || null)}
        name={field.name}
      />
    );
  }

  // Long text
  if (field.type === "str" && isLongText(field, draft)) {
    return (
      <textarea
        ref={ref as React.RefObject<HTMLTextAreaElement>}
        value={String(draft ?? "")}
        onChange={(e) => setDraft(e.target.value || null)}
        onKeyDown={onKeyDown}
        rows={4}
        className={baseClass + " resize-y"}
      />
    );
  }

  // Numbers
  if (field.type === "int" || field.type === "float" || field.type === "decimal") {
    return (
      <input
        ref={ref as React.RefObject<HTMLInputElement>}
        type="number"
        step={field.type === "int" ? "1" : "0.01"}
        value={draft as number ?? ""}
        onChange={(e) => setDraft(e.target.value ? e.target.valueAsNumber : null)}
        onKeyDown={onKeyDown}
        className={baseClass}
      />
    );
  }

  // Date
  if (field.type === "date") {
    return (
      <input
        ref={ref as React.RefObject<HTMLInputElement>}
        type="date"
        value={String(draft ?? "")}
        onChange={(e) => setDraft(e.target.value || null)}
        onKeyDown={onKeyDown}
        className={baseClass}
      />
    );
  }

  // Boolean
  if (field.type === "bool") {
    return (
      <label className="flex items-center gap-2 py-1.5">
        <input
          type="checkbox"
          checked={!!draft}
          onChange={(e) => setDraft(e.target.checked)}
          className="h-4 w-4"
        />
        <span className="text-sm">{draft ? "Yes" : "No"}</span>
      </label>
    );
  }

  // Default: text input
  return (
    <input
      ref={ref as React.RefObject<HTMLInputElement>}
      type="text"
      value={String(draft ?? "")}
      onChange={(e) => setDraft(e.target.value || null)}
      onKeyDown={onKeyDown}
      className={baseClass}
    />
  );
}

/** Expandable text block for long string fields. */
function ExpandableText({ value, onEdit }: { value: string; onEdit?: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const lines = value.split("\n");
  const isVeryLong = value.length > 200 || lines.length > 4;

  if (expanded) {
    return (
      <div className="text-sm rounded px-2 py-1.5 -mx-2 bg-gray-50 border relative">
        <pre className="whitespace-pre-wrap leading-relaxed font-sans text-gray-900">{value}</pre>
        <div className="flex gap-2 mt-2">
          <button
            onClick={() => setExpanded(false)}
            className="text-xs text-gray-500 hover:text-gray-700"
          >
            Collapse
          </button>
          {onEdit && (
            <button
              onClick={onEdit}
              className="text-xs text-blue-500 hover:text-blue-700"
            >
              Edit
            </button>
          )}
        </div>
      </div>
    );
  }

  const preview = isVeryLong
    ? lines.slice(0, 3).join("\n").slice(0, 150) + "..."
    : value;

  return (
    <div
      onClick={() => isVeryLong ? setExpanded(true) : onEdit?.()}
      className={`text-sm rounded px-2 py-1.5 -mx-2 min-h-[32px] group ${
        isVeryLong || onEdit ? "cursor-pointer hover:bg-gray-50" : ""
      }`}
    >
      <span className="whitespace-pre-wrap leading-relaxed text-gray-900">{preview}</span>
      {isVeryLong && (
        <span className="text-xs text-blue-500 ml-1">Show more</span>
      )}
      {onEdit && !isVeryLong && (
        <span className="ml-2 text-gray-300 opacity-0 group-hover:opacity-100 text-xs shrink-0">✎</span>
      )}
    </div>
  );
}
