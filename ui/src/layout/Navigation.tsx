import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "../api/client";
import type { EntityListMeta } from "../api/types";

export function Navigation() {
  const location = useLocation();
  const { data: entities } = useQuery({
    queryKey: ["entity-meta"],
    queryFn: () => apiClient<EntityListMeta[]>("/api/_meta/entities"),
  });

  const navItems = [
    { path: "/queue", label: "Queue" },
    { path: "/activity", label: "Activity" },
    { path: "/roles", label: "Roles" },
    { path: "/observability", label: "Observability" },
  ];

  const KERNEL_ENTITIES = new Set([
    "Organization",
    "Actor",
    "Role",
    "Integration",
    "Attention",
    "Runtime",
    "Session",
  ]);

  const INFRASTRUCTURE_ENTITIES = new Set([
    "ChangeRecord",
    "EntityDefinition",
    "Interaction",
    "Lookup",
    "Message",
    "MessageLog",
    "Rule",
    "RuleGroup",
    "Skill",
    "TestTask",
    "VerifyTest",
  ]);

  const allItems =
    entities
      ?.filter((e) => e.permissions.read)
      .map((e) => ({
        path: `/${e.name.toLowerCase()}s`,
        label: e.name,
        isKernel: KERNEL_ENTITIES.has(e.name),
        isInfra: INFRASTRUCTURE_ENTITIES.has(e.name),
      }))
      .sort((a, b) => a.label.localeCompare(b.label)) ?? [];

  const domainItems = allItems.filter((e) => !e.isKernel && !e.isInfra);
  const kernelItems = allItems.filter((e) => e.isKernel);
  const infraItems = allItems.filter((e) => e.isInfra);

  const isActive = (path: string) => location.pathname === path;

  const [expanded, setExpanded] = useState<Record<string, boolean>>(() => {
    try {
      const stored = localStorage.getItem("nav-expanded");
      return stored ? JSON.parse(stored) : { entities: true, system: false, infra: false };
    } catch {
      return { entities: true, system: false, infra: false };
    }
  });

  const toggleSection = (key: string) => {
    setExpanded((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      localStorage.setItem("nav-expanded", JSON.stringify(next));
      return next;
    });
  };

  return (
    <nav className="w-56 bg-white border-r border-gray-200 flex flex-col">
      <div className="p-4 border-b border-gray-200">
        <h1 className="text-lg font-bold text-gray-900">Indemn OS</h1>
      </div>
      <div className="flex-1 py-4 overflow-y-auto">
        <div className="px-3 mb-4">
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
            Operations
          </p>
          {navItems.map((item) => (
            <Link
              key={item.path}
              to={item.path}
              className={`block px-3 py-1.5 rounded text-sm ${
                isActive(item.path)
                  ? "bg-blue-50 text-blue-700 font-medium"
                  : "text-gray-700 hover:bg-gray-100"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </div>
        {domainItems.length > 0 && (
          <div className="px-3 mb-4">
            <button
              onClick={() => toggleSection("entities")}
              className="flex items-center justify-between w-full text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2 hover:text-gray-600"
            >
              <span>Entities</span>
              <span className="text-[10px]">{expanded.entities ? "\u25BC" : "\u25B6"}</span>
            </button>
            {expanded.entities &&
              domainItems.map((item) => (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`block px-3 py-1.5 rounded text-sm ${
                    isActive(item.path)
                      ? "bg-blue-50 text-blue-700 font-medium"
                      : "text-gray-700 hover:bg-gray-100"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
          </div>
        )}
        {kernelItems.length > 0 && (
          <div className="px-3 mb-4">
            <button
              onClick={() => toggleSection("system")}
              className="flex items-center justify-between w-full text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2 hover:text-gray-600"
            >
              <span>System</span>
              <span className="text-[10px]">{expanded.system ? "\u25BC" : "\u25B6"}</span>
            </button>
            {expanded.system &&
              kernelItems.map((item) => (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`block px-3 py-1.5 rounded text-sm ${
                    isActive(item.path)
                      ? "bg-blue-50 text-blue-700 font-medium"
                      : "text-gray-600 hover:bg-gray-100"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
          </div>
        )}
        {infraItems.length > 0 && (
          <div className="px-3 mb-4">
            <button
              onClick={() => toggleSection("infra")}
              className="flex items-center justify-between w-full text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2 hover:text-gray-600"
            >
              <span>Infrastructure</span>
              <span className="text-[10px]">{expanded.infra ? "\u25BC" : "\u25B6"}</span>
            </button>
            {expanded.infra &&
              infraItems.map((item) => (
                <Link
                  key={item.path}
                  to={item.path}
                  className={`block px-3 py-1.5 rounded text-sm ${
                    isActive(item.path)
                      ? "bg-blue-50 text-blue-700 font-medium"
                      : "text-gray-500 hover:bg-gray-100"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
          </div>
        )}
        <div className="px-3">
          <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
            Admin
          </p>
          <Link
            to="/auth-events"
            className={`block px-3 py-1.5 rounded text-sm ${
              isActive("/auth-events")
                ? "bg-blue-50 text-blue-700 font-medium"
                : "text-gray-700 hover:bg-gray-100"
            }`}
          >
            Auth Events
          </Link>
        </div>
      </div>
    </nav>
  );
}
