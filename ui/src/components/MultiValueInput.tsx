import { useState } from "react";

interface Props {
  value?: string[];
  onChange: (value: string[]) => void;
  name: string;
}

/** Chip-based input for list fields. Add/remove values. [G-32] */
export function MultiValueInput({ value = [], onChange, name }: Props) {
  const [input, setInput] = useState("");

  const addValue = () => {
    const trimmed = input.trim();
    if (trimmed && !value.includes(trimmed)) {
      onChange([...value, trimmed]);
      setInput("");
    }
  };

  const removeValue = (idx: number) => {
    onChange(value.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div className="flex flex-wrap gap-1 mb-1">
        {value.map((v, i) => (
          <span
            key={i}
            className="inline-flex items-center px-2 py-0.5 bg-blue-50 text-blue-700 rounded text-sm"
          >
            {v}
            <button
              type="button"
              onClick={() => removeValue(i)}
              className="ml-1 text-blue-400 hover:text-blue-600"
            >
              x
            </button>
          </span>
        ))}
      </div>
      <input type="hidden" name={name} value={JSON.stringify(value)} />
      <div className="flex gap-1">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addValue();
            }
          }}
          placeholder="Type and press Enter"
          className="flex-1 px-3 py-1.5 border rounded text-sm focus:ring-2 focus:ring-blue-400"
        />
        <button
          type="button"
          onClick={addValue}
          className="px-2 py-1.5 border rounded text-sm hover:bg-gray-50"
        >
          Add
        </button>
      </div>
    </div>
  );
}
