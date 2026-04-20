import { useState, useMemo } from "react";
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
} from "@tanstack/react-table";

interface Props {
  columns: ColumnDef<Record<string, unknown>>[];
  data: Record<string, unknown>[];
  onRowClick?: (row: Record<string, unknown>) => void;
  enableSelection?: boolean;
  onSelectionChange?: (selectedIds: string[]) => void;
}

export function EntityTable({
  columns,
  data,
  onRowClick,
  enableSelection,
  onSelectionChange,
}: Props) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});

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
    };
    return [selectCol, ...columns];
  }, [columns, enableSelection]);

  const table = useReactTable({
    data,
    columns: allColumns,
    getRowId: (row) => String(row._id || row.id || ""),
    state: { sorting, columnFilters, rowSelection },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onRowSelectionChange: (updater) => {
      const next =
        typeof updater === "function" ? updater(rowSelection) : updater;
      setRowSelection(next);
      onSelectionChange?.(Object.keys(next).filter((k) => next[k]));
    },
    enableRowSelection: true,  // Always enabled — checkbox column controls visibility
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <div className="overflow-x-auto border rounded-lg">
      <table className="min-w-full divide-y divide-gray-200">
        <thead className="bg-gray-50">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th
                  key={header.id}
                  className={`px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider${
                    header.column.getCanSort() ? " cursor-pointer select-none" : ""
                  }`}
                  onClick={header.column.getToggleSortingHandler()}
                >
                  <span className="flex items-center gap-1">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    {{
                      asc: <span aria-label="sorted ascending">{"\u25B2"}</span>,
                      desc: <span aria-label="sorted descending">{"\u25BC"}</span>,
                    }[header.column.getIsSorted() as string] ?? null}
                  </span>
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody className="bg-white divide-y divide-gray-200">
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              onClick={() => onRowClick?.(row.original)}
              className={onRowClick ? "cursor-pointer hover:bg-gray-50" : ""}
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-4 py-3 text-sm text-gray-900 whitespace-nowrap">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
          {table.getRowModel().rows.length === 0 && (
            <tr>
              <td colSpan={allColumns.length} className="px-4 py-8 text-center text-gray-400 text-sm">
                No data
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
