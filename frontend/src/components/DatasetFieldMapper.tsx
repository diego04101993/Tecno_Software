import type { DatasetColumn, LayoutBindingPresetInfo } from "../types/domain";

export type BindingFieldDraft = {
  target_field: string;
  column_key: string | null;
  display_label: string;
  fallback_value: string;
  format_hint: string | null;
  position_index: number;
  is_required: boolean;
  options_json: Record<string, unknown>;
};

export function DatasetFieldMapper({
  preset,
  columns,
  fields,
  onChange,
  disabled = false,
}: {
  preset: LayoutBindingPresetInfo;
  columns: DatasetColumn[];
  fields: BindingFieldDraft[];
  onChange: (fields: BindingFieldDraft[]) => void;
  disabled?: boolean;
}) {
  function updateField(targetField: string, patch: Partial<BindingFieldDraft>) {
    onChange(fields.map((field) => (field.target_field === targetField ? { ...field, ...patch } : field)));
  }

  return (
    <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-white">
      <div className="border-b border-slate-200 bg-slate-50 px-4 py-3">
        <p className="text-sm font-semibold text-ink">Mapping de columnas</p>
        <p className="mt-1 text-sm text-slate-600">Relaciona cada campo del preset con la columna correspondiente del dataset actual.</p>
      </div>

      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-white">
            <tr className="text-left text-slate-500">
              <th className="px-4 py-3 font-medium">Campo</th>
              <th className="px-4 py-3 font-medium">Columna del dataset</th>
              <th className="px-4 py-3 font-medium">Etiqueta</th>
              <th className="px-4 py-3 font-medium">Fallback</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {preset.fields.map((presetField) => {
              const field = fields.find((item) => item.target_field === presetField.key);
              if (!field) {
                return null;
              }

              return (
                <tr key={presetField.key}>
                  <td className="px-4 py-3 align-top">
                    <p className="font-medium text-ink">{presetField.label}</p>
                    <p className="mt-1 text-xs uppercase tracking-[0.2em] text-slate-400">
                      {presetField.required ? "Obligatorio" : "Opcional"}
                    </p>
                  </td>
                  <td className="px-4 py-3 align-top">
                    <select
                      value={field.column_key ?? ""}
                      disabled={disabled}
                      onChange={(event) => updateField(presetField.key, { column_key: event.target.value || null })}
                      className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-accent"
                    >
                      <option value="">Sin mapping</option>
                      {columns.map((column) => (
                        <option key={column.id} value={column.column_key}>
                          {column.display_name} ({column.column_key})
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-4 py-3 align-top">
                    <input
                      type="text"
                      value={field.display_label}
                      disabled={disabled}
                      onChange={(event) => updateField(presetField.key, { display_label: event.target.value })}
                      className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-accent"
                    />
                  </td>
                  <td className="px-4 py-3 align-top">
                    <input
                      type="text"
                      value={field.fallback_value}
                      disabled={disabled}
                      onChange={(event) => updateField(presetField.key, { fallback_value: event.target.value })}
                      className="w-full rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-accent"
                      placeholder="Opcional"
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
