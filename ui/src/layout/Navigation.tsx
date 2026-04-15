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
    { path: "/roles", label: "Roles" },
  ];

  const entityItems =
    entities
      ?.filter((e) => e.permissions.read)
      .map((e) => ({
        path: `/${e.name.toLowerCase()}s`,
        label: e.name,
        isKernel: e.is_kernel_entity,
      })) ?? [];

  const isActive = (path: string) => location.pathname === path;

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
        {entityItems.length > 0 && (
          <div className="px-3 mb-4">
            <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
              Entities
            </p>
            {entityItems.map((item) => (
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
                {item.isKernel && (
                  <span className="ml-1 text-xs text-gray-400">K</span>
                )}
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
