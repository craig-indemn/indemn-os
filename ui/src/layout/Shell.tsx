import { useState } from "react";
import { type ReactNode } from "react";
import { Navigation } from "./Navigation";
import { TopBar } from "./TopBar";
import { StatusBanner } from "./StatusBanner";
import { KeyboardHelp } from "../components/KeyboardHelp";

export function Shell({ children }: { children: ReactNode }) {
  const [sidebarOpen, setSidebarOpen] = useState(true);

  return (
    <div className="min-h-screen bg-gray-50 flex">
      {sidebarOpen && <Navigation />}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <StatusBanner />
        <TopBar
          sidebarOpen={sidebarOpen}
          onToggleSidebar={() => setSidebarOpen((v) => !v)}
        />
        <main className="flex-1 p-6 overflow-auto">{children}</main>
      </div>
      <KeyboardHelp />
    </div>
  );
}
