import { useState, useEffect } from "react";

export function KeyboardHelp() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "?" && !e.ctrlKey && !e.metaKey &&
          !(e.target instanceof HTMLInputElement) &&
          !(e.target instanceof HTMLTextAreaElement) &&
          !(e.target instanceof HTMLSelectElement)) {
        setOpen((v) => !v);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setOpen(false)}>
      <div className="bg-white rounded-lg shadow-xl p-6 w-80" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-sm font-semibold mb-3">Keyboard Shortcuts</h2>
        <div className="space-y-2 text-sm">
          <div className="flex justify-between"><span className="text-gray-600">Open assistant</span><kbd className="px-1.5 py-0.5 bg-gray-100 rounded text-xs font-mono">/</kbd></div>
          <div className="flex justify-between"><span className="text-gray-600">Open assistant</span><kbd className="px-1.5 py-0.5 bg-gray-100 rounded text-xs font-mono">Cmd+K</kbd></div>
          <div className="flex justify-between"><span className="text-gray-600">Close panel</span><kbd className="px-1.5 py-0.5 bg-gray-100 rounded text-xs font-mono">Esc</kbd></div>
          <div className="flex justify-between"><span className="text-gray-600">This help</span><kbd className="px-1.5 py-0.5 bg-gray-100 rounded text-xs font-mono">?</kbd></div>
        </div>
      </div>
    </div>
  );
}
