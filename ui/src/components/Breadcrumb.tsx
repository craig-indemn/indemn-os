import { Link } from "react-router-dom";

interface Crumb {
  label: string;
  to?: string;
}

export function Breadcrumb({ crumbs }: { crumbs: Crumb[] }) {
  return (
    <nav className="flex items-center gap-1 text-sm text-gray-500 mb-4">
      {crumbs.map((crumb, i) => (
        <span key={crumb.label + (crumb.to || i)} className="flex items-center gap-1">
          {i > 0 && <span>/</span>}
          {crumb.to ? (
            <Link to={crumb.to} className="text-blue-600 hover:underline">{crumb.label}</Link>
          ) : (
            <span className="text-gray-700 font-medium">{crumb.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
