import { SectionCard } from "../SectionCard";
import type { DatasetColumn, DatasetRow } from "../../types/domain";

export function DatasetRowsPreview({
  columns,
  rows,
}: {
  columns: DatasetColumn[];
  rows: DatasetRow[];
}) {
  return (
    <SectionCard title="Preview de filas" subtitle="Vista de lectura del snapshot actual para validar horarios, tarifas, destinos o cualquier tabla del cliente.">
      {columns.length > 0 && rows.length > 0 ? (
        <div className="overflow-auto rounded-[24px] border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-500">
              <tr>
                <th className="px-4 py-3 font-semibold">#</th>
                {columns.map((column) => (
                  <th key={column.id} className="px-4 py-3 font-semibold">
                    {column.display_name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-t border-slate-100">
                  <td className="px-4 py-3 text-slate-500">{row.row_index}</td>
                  {columns.map((column) => (
                    <td key={`${row.id}-${column.id}`} className="px-4 py-3 text-slate-700">
                      {formatCellValue(row.row_data_json[column.column_key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
          Todavía no hay filas disponibles para previsualizar.
        </div>
      )}
    </SectionCard>
  );
}

function formatCellValue(value: unknown) {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}
