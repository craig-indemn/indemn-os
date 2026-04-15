import { useState, useEffect } from "react";

interface Props {
  value?: string;
  onChange: (value: string) => void;
  name: string;
}

/** JSON editor with validation for dict fields. [G-32] */
export function JsonEditor({ value = "{}", onChange, name }: Props) {
  const [text, setText] = useState(
    typeof value === "string" ? value : JSON.stringify(value, null, 2)
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    try {
      JSON.parse(text);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON");
    }
  }, [text]);

  return (
    <div>
      <textarea
        name={name}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            JSON.parse(e.target.value);
            onChange(e.target.value);
          } catch {
            // Don't propagate invalid JSON
          }
        }}
        rows={4}
        className={`w-full px-3 py-1.5 border rounded text-sm font-mono focus:ring-2 ${
          error
            ? "border-red-300 focus:ring-red-400"
            : "focus:ring-blue-400"
        }`}
        placeholder="{}"
      />
      {error && (
        <p className="text-xs text-red-500 mt-0.5">{error}</p>
      )}
    </div>
  );
}
