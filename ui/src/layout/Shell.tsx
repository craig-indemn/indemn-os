import { useState, useEffect, useRef } from "react";
import { type ReactNode } from "react";
import { Navigation } from "./Navigation";
import { TopBar } from "./TopBar";
import { StatusBanner } from "./StatusBanner";
import { KeyboardHelp } from "../components/KeyboardHelp";
import { ResizeDivider } from "../components/ResizeDivider";
import { AssistantPanel } from "../assistant/AssistantPanel";
import { useAssistantLayout } from "../assistant/useAssistantLayout";

export function Shell({ children }: { children: ReactNode }) {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const { isOpen, width, setWidth, toggleOpen, openPane } = useAssistantLayout();
  const inputRef = useRef<HTMLInputElement>(null);

  // Global keyboard shortcuts for assistant
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = document.activeElement?.tagName;
      if (e.key === "/" && !e.ctrlKey && !e.metaKey && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
        e.preventDefault();
        openPane();
        setTimeout(() => inputRef.current?.focus(), 50);
      }
      if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        openPane();
        setTimeout(() => inputRef.current?.focus(), 50);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [openPane]);

  return (
    <div className="h-screen bg-gray-50 flex">
      {sidebarOpen && <Navigation />}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <StatusBanner />
        <TopBar
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen((v) => !v)}
          onOpenAssistant={() => { openPane(); setTimeout(() => inputRef.current?.focus(), 50); }}
          assistantOpen={isOpen}
        />
        <div className="flex-1 min-h-0 flex flex-row">
          <main className="flex-1 p-6 overflow-auto min-w-0">{children}</main>
          {isOpen && (
            <>
              <ResizeDivider onResize={(delta) => setWidth(width + delta)} />
              <AssistantPanel
                width={width}
                inputRef={inputRef}
                onClose={toggleOpen}
              />
            </>
          )}
          {!isOpen && (
            <button
              onClick={toggleOpen}
              className="w-10 flex-shrink-0 border-l bg-white hover:bg-gray-50 flex items-center justify-center text-gray-400 hover:text-gray-600"
              title="Open Assistant (/ or Cmd+K)"
            >
              <span className="text-lg">&#x2756;</span>
            </button>
          )}
        </div>
      </div>
      <KeyboardHelp />
    </div>
  );
}
