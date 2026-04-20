import { type ReactNode } from "react";
import { Navigation } from "./Navigation";
import { TopBar } from "./TopBar";
import { StatusBanner } from "./StatusBanner";
import { KeyboardHelp } from "../components/KeyboardHelp";

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-gray-50 flex">
      <Navigation />
      <div className="flex-1 flex flex-col">
        <StatusBanner />
        <TopBar />
        <main className="flex-1 p-6 overflow-auto">{children}</main>
      </div>
      <KeyboardHelp />
    </div>
  );
}
