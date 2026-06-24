import { SectionCard } from "../SectionCard";
import { formatDatasetColumnType } from "../../lib/labels";
import type { DatasetColumn } from "../../types/domain";

export function DatasetSchemaTable({
  columns,
}: {
  columns: DatasetColumn[];
}) {
  return (
    <SectionCard title="Esquema detectado" subtitle="Columnas leídas automáticamente desde la versión activa del dataset.">
      {columns.length > 0 ? (
        <div className="overflow-auto rounded-[24px] border border-slate-200 bg-white">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-50 text-slate-500">
              <tr>
                <th className="px-4 py-3 font-semibold">Columna</th>
                <th className="px-4 py-3 font-semibold">Key</th>
                <th className="px-4 py-3 font-semibold">Tipo</th>
                <th className="px-4 py-3 font-semibold">Ejemplo</th>
              </tr>
            </thead>
            <tbody>
              {columns.map((column) => (
                <tr key={column.id} className="border-t border-slate-100">
                  <td className="px-4 py-3 font-semibold text-ink">{column.display_name}</td>
                  <td className="px-4 py-3 text-slate-600">{column.column_key}</td>
                  <td className="px-4 py-3 text-slate-600">{formatDatasetColumnType(column.data_type)}</td>
                  <td className="px-4 py-3 text-slate-600">{column.sample_value ?? "Sin ejemplo"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
          Aún no hay columnas detectadas para este dataset.
        </div>
      )}
    </SectionCard>
  );
}
