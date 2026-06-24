import { formatDynamicValue } from "../lib/layoutBindings";
import type { LayoutBindingPreviewHeader } from "../types/domain";

export function DynamicTablePreview({
  headers,
  rows,
  emptyLabel = "No hay filas disponibles para este preview.",
}: {
  headers: LayoutBindingPreviewHeader[];
  rows: Array<Record<string, unknown>>;
  emptyLabel?: string;
}) {
  if (headers.length === 0) {
    return (
      <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
        Aún no hay columnas resueltas para este binding.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-white">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-slate-50">
            <tr className="text-left text-slate-500">
              {headers.map((header) => (
                <th key={header.key} className="px-4 py-3 font-medium">
                  {header.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.length > 0 ? (
              rows.map((row, rowIndex) => (
                <tr key={`${row._row_index ?? rowIndex}`}>
                  {headers.map((header) => (
                    <td key={header.key} className="px-4 py-3 text-slate-700">
                      {formatDynamicValue(row[header.target_field], header.format_hint)}
                    </td>
                  ))}
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={headers.length} className="px-4 py-6 text-center text-sm text-slate-500">
                  {emptyLabel}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
