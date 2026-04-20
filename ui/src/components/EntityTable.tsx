import { useState, useMemo, useEffect } from "react";
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
  type ColumnFiltersState,
  type RowSelectionState,
  type VisibilityState,
} from "@tanstack/react-table";

interface Props {
  columns: ColumnDef<Record<string, unknown>>[];
  data: Record<string, unknown>[];
  onRowClick?: (row: Record<string, unknown>) => void;
  enableSelection?: boolean;
  onSelectionChange?: (selectedIds: string[]) => void;
  storageKey?: string;
}

export function EntityTable({
  columns,
  data,
  onRowClick,
  enableSelection,
  onSelectionChange,
  storageKey,
}: Props) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>(() => {
    if (!storageKey) return {};
    try {
      const stored = localStorage.getItem(`col-vis-${storageKey}`);
      return stored ? JSON.parse(stored) : {};
    } catch { return {}; }
  });
  const [showColumnPicker, setShowColumnPicker] = useState(false);

  useEffect(() => {
    if (storageKey && Object.keys(columnVisibility).length > 0) {
      localStorage.setItem(`col-vis-${storageKey}`, JSON.stringify(columnVisibility));
    }
  }, [columnVisibility, storageKey]);

  // Checkbox column prepended when selection is enabled
  const allColumns = useMemo<ColumnDef<Record<string, unknown>>[]>(() => {
    if (!enableSelection) return columns;
    const selectCol: ColumnDef<Record<string, unknown>> = {
      id: "select",
      header: ({ table: t }) => (
        <input
          type="checkbox"
          checked={t.getIsAllPageRowsSelected()}
          onChange={t.getToggleAllPageRowsSelectedHandler()}
          className="h-4 w-4"
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          checked={row.getIsSelected()}
          onChange={row.getToggleSelectedHandler()}
          onClick={(e) => e.stopPropagation()}
          className="h-4 w-4"
        />
      ),
      enableSorting: false,
      enableHiding: false,
    };
    return [selectCol, ...columns];
  }, [columns, enableSelection]);

  const table = useReactTable({
    data,
    columns: allColumns,
    getRowId: (row) => String(row._id || row.id || ""),
    state: { sorting, columnFilters, rowSelection, columnVisibility },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnVisibilityChange: setColumnVisibility,
    onRowSelectionChange: (updater) => {
      const next =
        typeof updater === "function" ? updater(rowSelection) : updater;
      setRowSelection(next);
      onSelectionChange?.(Object.keys(next).filter((k) => next[k]));
    },
    enableRowSelection: true,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <div>
      {/* Column visibility toggle */}
      <div className="flex justify-end mb-2">
        <div className="relative">
          <button
            onClick={() => setShowColumnPicker((v) => !v)}
            className="px-2 py-1 text-xs border rounded text-gray-500 hover:bg-gray-50"
          >
            Columns ({table.getVisibleLeafColumns().length}/{table.getAllLeafColumns().length})
          </button>
          {showColumnPicker && (
            <div className="absolute right-0 top-8 z-20 bg-white border rounded-lg shadow-lg p-3 w-56 max-h-72 overflow-y-auto">
              <div className="flex justify-between items-center mb-2">
                <span className="text-xs font-medium text-gray-500">Toggle columns</span>
                <button
                  onClick={() => {
                    table.toggleAllColumnsVisible(true);
                  }}
                  className="text-xs text-blue-600 hover:underline"
                >
                  Show all
                </button>
              </div>
              {table
                .getAllLeafColumns()
                .filter((col) => col.getCanHide())
                .map((col) => (
                  <label
                    key={col.id}
                    className="flex items-center gap-2 py-1 text-sm text-gray-700 cursor-pointer hover:bg-gray-50 rounded px-1"
                  >
                    <input
                      type="checkbox"
                      checked={col.getIsVisible()}
                      onChange={col.getToggleVisibilityHandler()}
                      className="h-3.5 w-3.5"
                    />
                    {typeof col.columnDef.header === "string"
                      ? col.columnDef.header
                      : col.id.replace(/_/g, " ")}
                  </label>
                ))}
            </div>
          )}
        </div>
      </div>

      {/* Spreadsheet-style scrollable table */}
      <div className="overflow-x-auto border rounded-lg">
        <table className="divide-y divide-gray-200" style={{ minWidth: "100%" }}>
          <thead className="bg-gray-50">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className={`px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider whitespace-nowrap${
                      header.column.getCanSort()
                        ? " cursor-pointer select-none"
                        : ""
                    }`}
                    onClick={header.column.getToggleSortingHandler()}
                  >
                    <span className="flex items-center gap-1">
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext()
                      )}
                      {{
                        asc: <span aria-label="sorted ascending">{"\u25B2"}</span>,
                        desc: <span aria-label="sorted descending">{"\u25BC"}</span>,
                      }[header.column.getIsSorted() as string] ?? null}
                    </span>
                  </th>
                ))}
              </tr>
            ))}
            {/* Per-column filter row */}
            <tr className="border-t">
              {table.getHeaderGroups()[0]?.headers.map((header) => {
                const meta = header.column.columnDef.meta as
                  | { fieldType?: string; enumValues?: string[] }
                  | undefined;
                const canFilter =
                  header.column.getCanFilter() &&
                  header.id !== "select" &&
                  header.id !== "status";

                return (
                  <th key={header.id + "-filter"} className="px-4 py-1">
                    {canFilter && meta?.enumValues?.length ? (
                      <select
                        value={(header.column.getFilterValue() as string) ?? ""}
                        onChange={(e) =>
                          header.column.setFilterValue(
                            e.target.value || undefined
                          )
                        }
                        className="w-full px-1 py-0.5 text-xs border rounded"
                      >
                        <option value="">All</option>
                        {meta.enumValues.map((v) => (
                          <option key={v} value={v}>
                            {v.replace(/_/g, " ")}
                          </option>
                        ))}
                      </select>
                    ) : canFilter ? (
                      <input
                        type="text"
                        value={
                          (header.column.getFilterValue() as string) ?? ""
                        }
                        onChange={(e) =>
                          header.column.setFilterValue(
                            e.target.value || undefined
                          )
                        }
                        placeholder="Filter..."
                        className="w-full px-1 py-0.5 text-xs border rounded"
                      />
                    ) : null}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                onClick={() => onRowClick?.(row.original)}
                className={onRowClick ? "cursor-pointer hover:bg-gray-50" : ""}
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="px-4 py-3 text-sm text-gray-900 whitespace-nowrap"
                  >
                    {flexRender(
                      cell.column.columnDef.cell,
                      cell.getContext()
                    )}
                  </td>
                ))}
              </tr>
            ))}
            {table.getRowModel().rows.length === 0 && (
              <tr>
                <td
                  colSpan={allColumns.length}
                  className="px-4 py-12 text-center text-sm"
                >
                  <p className="text-gray-400">No results found</p>
                  {columnFilters.length > 0 && (
                    <button
                      onClick={() => setColumnFilters([])}
                      className="mt-2 text-blue-600 hover:underline text-xs"
                    >
                      Clear filters
                    </button>
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
