import { Database, Link2, Table2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { DynamicTablePreview } from "./DynamicTablePreview";
import { apiRequest } from "../lib/api";
import { formatLayoutBindingPreset } from "../lib/labels";
import type {
  Dataset,
  DatasetColumn,
  LayoutBindingField,
  LayoutBindingPreviewResult,
  LayoutDataBinding,
  LayoutWidget,
  LayoutBindingPreset,
} from "../types/domain";

type DatasetBindingInspectorProps = {
  token: string | null;
  layoutId: string | null;
  selectedWidget: LayoutWidget | null;
  datasets: Dataset[];
  bindings: LayoutDataBinding[];
  canManage: boolean;
  onAttachBinding: (bindingId: string, datasetId: string) => void;
  onBindingSaved: () => Promise<void> | void;
};

type EditableField = {
  target_field: string;
  display_label: string;
  column_key: string;
  fallback_value: string;
  format_hint: string;
  is_required: boolean;
  position_index: number;
};

const presetCatalog: Record<LayoutBindingPreset, Array<{ key: string; label: string; required: boolean; format_hint: string }>> = {
  autobuses: [
    { key: "destino", label: "Destino", required: true, format_hint: "text" },
    { key: "hora", label: "Hora", required: true, format_hint: "time" },
    { key: "precio", label: "Precio", required: true, format_hint: "currency" },
    { key: "puerta", label: "Puerta", required: true, format_hint: "text" },
    { key: "servicio", label: "Servicio", required: false, format_hint: "text" },
  ],
  aeropuerto: [
    { key: "vuelo", label: "Vuelo", required: true, format_hint: "text" },
    { key: "destino", label: "Destino", required: true, format_hint: "text" },
    { key: "hora", label: "Hora", required: true, format_hint: "time" },
    { key: "puerta", label: "Puerta", required: true, format_hint: "text" },
    { key: "estado", label: "Estado", required: false, format_hint: "status" },
  ],
  menu: [
    { key: "producto", label: "Producto", required: true, format_hint: "text" },
    { key: "descripcion", label: "Descripción", required: false, format_hint: "text" },
    { key: "precio", label: "Precio", required: true, format_hint: "currency" },
    { key: "categoria", label: "Categoría", required: false, format_hint: "text" },
  ],
  turnero: [
    { key: "turno", label: "Turno", required: true, format_hint: "text" },
    { key: "servicio", label: "Servicio", required: true, format_hint: "text" },
    { key: "modulo", label: "Módulo", required: false, format_hint: "text" },
    { key: "estado", label: "Estado", required: false, format_hint: "status" },
  ],
};

function buildDefaultFields(preset: LayoutBindingPreset, existing: LayoutBindingField[] = []): EditableField[] {
  const existingByTarget = new Map(existing.map((field) => [field.target_field, field]));
  return presetCatalog[preset].map((definition, index) => {
    const field = existingByTarget.get(definition.key);
    return {
      target_field: definition.key,
      display_label: field?.display_label ?? definition.label,
      column_key: field?.column_key ?? "",
      fallback_value: field?.fallback_value ?? "",
      format_hint: field?.format_hint ?? definition.format_hint,
      is_required: field?.is_required ?? definition.required,
      position_index: field?.position_index ?? index,
    };
  });
}

export function DatasetBindingInspector({
  token,
  layoutId,
  selectedWidget,
  datasets,
  bindings,
  canManage,
  onAttachBinding,
  onBindingSaved,
}: DatasetBindingInspectorProps) {
  const [selectedBindingId, setSelectedBindingId] = useState<string>("");
  const [selectedPreset, setSelectedPreset] = useState<LayoutBindingPreset>("autobuses");
  const [selectedDatasetId, setSelectedDatasetId] = useState<string>("");
  const [columns, setColumns] = useState<DatasetColumn[]>([]);
  const [fields, setFields] = useState<EditableField[]>(buildDefaultFields("autobuses"));
  const [preview, setPreview] = useState<LayoutBindingPreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectedBinding = useMemo(
    () => bindings.find((binding) => binding.id === (selectedBindingId || selectedWidget?.binding_ref.binding_id || "")) ?? null,
    [bindings, selectedBindingId, selectedWidget?.binding_ref.binding_id],
  );

  useEffect(() => {
    if (!selectedWidget || selectedWidget.widget_type !== "dataset_table") {
      return;
    }

    const bindingId = selectedWidget.binding_ref.binding_id ?? "";
    setSelectedBindingId(bindingId);
  }, [selectedWidget?.id, selectedWidget?.widget_type, selectedWidget?.binding_ref.binding_id]);

  useEffect(() => {
    if (selectedBinding) {
      setSelectedPreset(selectedBinding.preset_key);
      setSelectedDatasetId(selectedBinding.dataset_id);
    }
  }, [selectedBinding?.id]);

  useEffect(() => {
    async function loadBindingState() {
      if (!token || !layoutId || !selectedBinding) {
        setColumns([]);
        setFields(buildDefaultFields(selectedPreset));
        return;
      }

      try {
        const [columnsResponse, fieldsResponse] = await Promise.all([
          apiRequest<DatasetColumn[]>(`/datasets/${selectedBinding.dataset_id}/columns`, { token }),
          apiRequest<LayoutBindingField[]>(`/layouts/${layoutId}/bindings/${selectedBinding.id}/fields`, { token }),
        ]);
        setColumns(columnsResponse);
        setFields(buildDefaultFields(selectedBinding.preset_key, fieldsResponse));
        setError(null);
      } catch (nextError) {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el binding seleccionado");
      }
    }

    void loadBindingState();
  }, [layoutId, selectedBinding?.id, selectedBinding?.dataset_id, selectedBinding?.preset_key, token]);

  useEffect(() => {
    if (!selectedBinding && selectedDatasetId) {
      async function loadColumns() {
        if (!token) {
          return;
        }
        try {
          const response = await apiRequest<DatasetColumn[]>(`/datasets/${selectedDatasetId}/columns`, { token });
          setColumns(response);
          setError(null);
        } catch (nextError) {
          setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar las columnas del dataset");
        }
      }
      void loadColumns();
    }
  }, [selectedBinding, selectedDatasetId, token]);

  async function validatePreview() {
    if (!token || !layoutId || !(selectedBinding || selectedDatasetId)) {
      return;
    }

    try {
      const response = await apiRequest<LayoutBindingPreviewResult>(`/layouts/${layoutId}/bindings/validate`, {
        method: "POST",
        token,
        body: {
          dataset_id: selectedBinding?.dataset_id ?? selectedDatasetId,
          preset_key: selectedBinding?.preset_key ?? selectedPreset,
          zone_key: null,
          max_rows: 8,
          options_json: {},
          fields: fields.map((field) => ({
            target_field: field.target_field,
            column_key: field.column_key || null,
            display_label: field.display_label || null,
            fallback_value: field.fallback_value || null,
            format_hint: field.format_hint || null,
            position_index: field.position_index,
            is_required: field.is_required,
            options_json: {},
          })),
        },
      });
      setPreview(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo validar el binding");
    }
  }

  async function saveBinding() {
    if (!token || !layoutId || !selectedWidget) {
      return;
    }

    try {
      let bindingId = selectedBinding?.id ?? "";
      let datasetId = selectedBinding?.dataset_id ?? selectedDatasetId;

      if (!bindingId) {
        const created = await apiRequest<LayoutDataBinding>(`/layouts/${layoutId}/bindings`, {
          method: "POST",
          token,
          body: {
            dataset_id: selectedDatasetId,
            name: selectedWidget.name || "Tabla dinámica",
            preset_key: selectedPreset,
            zone_key: null,
            sort_order: bindings.length + 1,
            max_rows: 8,
            options_json: {
              header_config: {
                show_headers: true,
              },
            },
            is_active: true,
          },
        });
        bindingId = created.id;
        datasetId = created.dataset_id;
      }

      await apiRequest(`/layouts/${layoutId}/bindings/${bindingId}/fields`, {
        method: "POST",
        token,
        body: {
          fields: fields.map((field) => ({
            target_field: field.target_field,
            column_key: field.column_key || null,
            display_label: field.display_label || null,
            fallback_value: field.fallback_value || null,
            format_hint: field.format_hint || null,
            position_index: field.position_index,
            is_required: field.is_required,
            options_json: {},
          })),
        },
      });

      onAttachBinding(bindingId, datasetId);
      await onBindingSaved();
      setSelectedBindingId(bindingId);
      setError(null);
      await validatePreview();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el binding");
    }
  }

  if (!selectedWidget || selectedWidget.widget_type !== "dataset_table") {
    return (
      <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
        <div className="border-b border-slate-200 pb-5">
          <h2 className="font-display text-2xl text-ink">Dataset Binding Inspector</h2>
          <p className="mt-1 text-sm text-slate-600">Selecciona un widget de tabla dinámica para conectar datasets, bindings y columnas al canvas.</p>
        </div>
        <div className="mt-6 rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
          El inspector de datasets se activa solo cuando seleccionas un widget de tabla dinámica.
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Dataset Binding Inspector</h2>
        <p className="mt-1 text-sm text-slate-600">Conecta la tabla dinámica a un dataset reutilizable, edita su mapping de columnas y valida el preview antes de publicarlo.</p>
      </div>

      <div className="mt-6 space-y-4">
        {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Binding existente</label>
            <select value={selectedBindingId} onChange={(event) => setSelectedBindingId(event.target.value)} disabled={!canManage}>
              <option value="">Crear desde este widget</option>
              {bindings.map((binding) => (
                <option key={binding.id} value={binding.id}>
                  {binding.name} · {formatLayoutBindingPreset(binding.preset_key)}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Dataset</label>
            <select
              value={selectedBinding?.dataset_id ?? selectedDatasetId}
              onChange={(event) => setSelectedDatasetId(event.target.value)}
              disabled={!canManage || Boolean(selectedBinding)}
            >
              <option value="">Selecciona un dataset</option>
              {datasets.map((dataset) => (
                <option key={dataset.id} value={dataset.id}>
                  {dataset.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Preset</label>
            <select
              value={selectedBinding?.preset_key ?? selectedPreset}
              onChange={(event) => {
                const nextPreset = event.target.value as LayoutBindingPreset;
                setSelectedPreset(nextPreset);
                setFields(buildDefaultFields(nextPreset));
              }}
              disabled={!canManage || Boolean(selectedBinding)}
            >
              <option value="autobuses">Autobuses</option>
              <option value="aeropuerto">Aeropuerto</option>
              <option value="menu">Menú</option>
              <option value="turnero">Turnero</option>
            </select>
          </div>
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
            {selectedBinding ? (
              <span className="inline-flex items-center gap-2">
                <Link2 className="h-4 w-4 text-accent" />
                Binding conectado al widget
              </span>
            ) : (
              <span className="inline-flex items-center gap-2">
                <Database className="h-4 w-4 text-accent" />
                Crea un binding nuevo y asígnalo al widget
              </span>
            )}
          </div>
        </div>

        <div className="space-y-3">
          {fields.map((field, index) => (
            <article key={field.target_field} className="rounded-[24px] border border-slate-200 bg-white p-4">
              <div className="grid gap-4 md:grid-cols-[1fr_1.1fr_0.8fr]">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Campo</label>
                  <input
                    value={field.display_label}
                    disabled={!canManage}
                    onChange={(event) => {
                      const next = [...fields];
                      next[index] = { ...field, display_label: event.target.value };
                      setFields(next);
                    }}
                  />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Columna</label>
                  <select
                    value={field.column_key}
                    disabled={!canManage}
                    onChange={(event) => {
                      const next = [...fields];
                      next[index] = { ...field, column_key: event.target.value };
                      setFields(next);
                    }}
                  >
                    <option value="">Sin mapear</option>
                    {columns.map((column) => (
                      <option key={column.id} value={column.column_key}>
                        {column.display_name}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Fallback</label>
                  <input
                    value={field.fallback_value}
                    disabled={!canManage}
                    onChange={(event) => {
                      const next = [...fields];
                      next[index] = { ...field, fallback_value: event.target.value };
                      setFields(next);
                    }}
                  />
                </div>
              </div>
            </article>
          ))}
        </div>

        <div className="flex flex-wrap gap-3">
          <button className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700" type="button" onClick={() => void validatePreview()}>
            Validar preview
          </button>
          <button
            className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white disabled:opacity-50"
            type="button"
            disabled={!canManage}
            onClick={() => void saveBinding()}
          >
            Guardar binding
          </button>
        </div>

        {preview ? (
          <div className="space-y-4 rounded-[26px] border border-slate-200 bg-slate-50 p-4">
            <div className="flex items-center gap-3">
              <Table2 className="h-5 w-5 text-accent" />
              <p className="font-semibold text-ink">Preview dinámico</p>
            </div>
            {preview.validation.errors.length > 0 ? (
              <div className="rounded-[18px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{preview.validation.errors.join(" ")}</div>
            ) : null}
            <DynamicTablePreview headers={preview.headers} rows={preview.rows} />
          </div>
        ) : null}
      </div>
    </section>
  );
}
