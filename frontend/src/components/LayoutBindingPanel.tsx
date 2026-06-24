import { Loader2, Plus, RefreshCcw, Save, Trash2, WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { apiRequest } from "../lib/api";
import { buildSuggestedBindingFields, getLayoutBindingPreset, normalizeLayoutZones } from "../lib/layoutBindings";
import { formatLayoutBindingPreset } from "../lib/labels";
import type {
  Dataset,
  DatasetColumn,
  Layout,
  LayoutBindingField,
  LayoutBindingPreviewResult,
  LayoutDataBinding,
  LayoutBindingPreset,
} from "../types/domain";
import { DatasetFieldMapper, type BindingFieldDraft } from "./DatasetFieldMapper";
import { SectionCard } from "./SectionCard";
import { TemplatePresetPicker } from "./TemplatePresetPicker";

type LayoutBindingPanelProps = {
  token: string | null;
  layout: Layout | null;
  datasets: Dataset[];
  bindings: LayoutDataBinding[];
  readOnly: boolean;
  onChanged: (nextBindingId?: string | null) => Promise<void> | void;
  onValidated: (result: LayoutBindingPreviewResult | null) => void;
};

function blankFieldDrafts(presetKey: LayoutBindingPreset) {
  return buildSuggestedBindingFields(presetKey, []);
}

export function LayoutBindingPanel({
  token,
  layout,
  datasets,
  bindings,
  readOnly,
  onChanged,
  onValidated,
}: LayoutBindingPanelProps) {
  const [selectedBindingId, setSelectedBindingId] = useState<string | null>(null);
  const [datasetId, setDatasetId] = useState<string>("");
  const [name, setName] = useState("");
  const [presetKey, setPresetKey] = useState<LayoutBindingPreset>("autobuses");
  const [zoneKey, setZoneKey] = useState<string>("");
  const [maxRows, setMaxRows] = useState(8);
  const [sortOrder, setSortOrder] = useState(1);
  const [isActive, setIsActive] = useState(true);
  const [fieldDrafts, setFieldDrafts] = useState<BindingFieldDraft[]>(blankFieldDrafts("autobuses"));
  const [datasetColumns, setDatasetColumns] = useState<DatasetColumn[]>([]);
  const [loadingColumns, setLoadingColumns] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const zones = layout ? normalizeLayoutZones(layout) : [];
  const selectedBinding = bindings.find((binding) => binding.id === selectedBindingId) ?? null;
  const selectedPreset = getLayoutBindingPreset(presetKey);

  useEffect(() => {
    if (!layout) {
      return;
    }

    if (bindings.length === 0) {
      setSelectedBindingId(null);
      setDatasetId(datasets[0]?.id ?? "");
      setPresetKey("autobuses");
      setName(layout.name ? `${layout.name} dinámico` : "Binding dinámico");
      setZoneKey(zones[0]?.key ?? "");
      setMaxRows(getLayoutBindingPreset("autobuses").default_max_rows);
      setSortOrder(1);
      setIsActive(true);
      setFieldDrafts(blankFieldDrafts("autobuses"));
      onValidated(null);
      return;
    }

    if (!selectedBindingId || !bindings.some((binding) => binding.id === selectedBindingId)) {
      setSelectedBindingId(bindings[0].id);
    }
  }, [bindings, datasets, layout, selectedBindingId, zones, onValidated]);

  useEffect(() => {
    if (!selectedBinding) {
      return;
    }

    setDatasetId(selectedBinding.dataset_id);
    setName(selectedBinding.name);
    setPresetKey(selectedBinding.preset_key);
    setZoneKey(selectedBinding.zone_key ?? zones[0]?.key ?? "");
    setMaxRows(selectedBinding.max_rows);
    setSortOrder(selectedBinding.sort_order);
    setIsActive(selectedBinding.is_active);
    onValidated(null);
  }, [selectedBinding, zones, onValidated]);

  useEffect(() => {
    if (!token || !datasetId) {
      setDatasetColumns([]);
      return;
    }

    setLoadingColumns(true);
    apiRequest<DatasetColumn[]>(`/datasets/${datasetId}/columns`, { token })
      .then((columns) => {
        setDatasetColumns(columns);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar las columnas del dataset");
      })
      .finally(() => {
        setLoadingColumns(false);
      });
  }, [datasetId, token]);

  useEffect(() => {
    if (!token || !selectedBindingId || !layout) {
      if (!selectedBindingId) {
        setFieldDrafts(buildSuggestedBindingFields(presetKey, datasetColumns));
      }
      return;
    }

    apiRequest<LayoutBindingField[]>(`/layouts/${layout.id}/bindings/${selectedBindingId}/fields`, { token })
      .then((fields) => {
        setFieldDrafts(buildSuggestedBindingFields(presetKey, datasetColumns, fields));
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar los campos del binding");
      });
  }, [datasetColumns, layout, presetKey, selectedBindingId, token]);

  useEffect(() => {
    if (selectedBindingId || datasetColumns.length === 0) {
      return;
    }

    setFieldDrafts(buildSuggestedBindingFields(presetKey, datasetColumns));
  }, [datasetColumns, presetKey, selectedBindingId]);

  function resetForNewBinding(nextPreset: LayoutBindingPreset = presetKey) {
    setSelectedBindingId(null);
    setPresetKey(nextPreset);
    setName(layout ? `${layout.name} ${formatLayoutBindingPreset(nextPreset)}` : "Binding dinámico");
    setMaxRows(getLayoutBindingPreset(nextPreset).default_max_rows);
    setSortOrder(bindings.length + 1);
    setZoneKey(zones[0]?.key ?? "");
    setIsActive(true);
    setFieldDrafts(buildSuggestedBindingFields(nextPreset, datasetColumns));
    onValidated(null);
  }

  async function handleValidate() {
    if (!token || !layout || !datasetId) {
      return;
    }

    setValidating(true);
    try {
      const result = await apiRequest<LayoutBindingPreviewResult>(`/layouts/${layout.id}/bindings/validate`, {
        method: "POST",
        token,
        body: {
          dataset_id: datasetId,
          preset_key: presetKey,
          zone_key: zoneKey || null,
          max_rows: maxRows,
          options_json: {},
          fields: fieldDrafts,
        },
      });
      onValidated(result);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo validar el mapping");
    } finally {
      setValidating(false);
    }
  }

  async function handleSave() {
    if (!token || !layout || !datasetId || readOnly) {
      return;
    }

    setSaving(true);
    try {
      const binding = selectedBindingId
        ? await apiRequest<LayoutDataBinding>(`/layouts/${layout.id}/bindings/${selectedBindingId}`, {
            method: "PATCH",
            token,
            body: {
              dataset_id: datasetId,
              name,
              preset_key: presetKey,
              zone_key: zoneKey || null,
              sort_order: sortOrder,
              max_rows: maxRows,
              options_json: {},
              is_active: isActive,
            },
          })
        : await apiRequest<LayoutDataBinding>(`/layouts/${layout.id}/bindings`, {
            method: "POST",
            token,
            body: {
              dataset_id: datasetId,
              name,
              preset_key: presetKey,
              zone_key: zoneKey || null,
              sort_order: sortOrder,
              max_rows: maxRows,
              options_json: {},
              is_active: isActive,
            },
          });

      await apiRequest<LayoutBindingField[]>(`/layouts/${layout.id}/bindings/${binding.id}/fields`, {
        method: "POST",
        token,
        body: {
          fields: fieldDrafts,
        },
      });

      setSelectedBindingId(binding.id);
      await onChanged(binding.id);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el binding");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!token || !layout || !selectedBindingId || readOnly) {
      return;
    }

    setSaving(true);
    try {
      await apiRequest(`/layouts/${layout.id}/bindings/${selectedBindingId}`, {
        method: "DELETE",
        token,
      });
      resetForNewBinding();
      await onChanged(null);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo eliminar el binding");
    } finally {
      setSaving(false);
    }
  }

  if (!layout) {
    return null;
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <SectionCard
        title="Bindings dinámicos"
        subtitle="Asigna datasets a zonas del layout y deja el runtime listo para la siguiente fase del player."
        action={
          <button
            type="button"
            disabled={readOnly}
            onClick={() => resetForNewBinding()}
            className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Plus className="h-4 w-4" />
            Nuevo binding
          </button>
        }
      >
        <div className="space-y-5">
          <div className="flex flex-wrap gap-3">
            {bindings.map((binding) => (
              <button
                key={binding.id}
                type="button"
                onClick={() => setSelectedBindingId(binding.id)}
                className={[
                  "rounded-full border px-4 py-2 text-sm font-semibold transition",
                  binding.id === selectedBindingId
                    ? "border-accent bg-accentSoft text-ink"
                    : "border-slate-200 bg-white text-slate-600 hover:border-accent/40",
                ].join(" ")}
              >
                {binding.name}
              </button>
            ))}
            {bindings.length === 0 ? <span className="text-sm text-slate-500">Todavía no hay bindings en este layout.</span> : null}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <label className="space-y-2 text-sm text-slate-600">
              <span className="font-medium text-ink">Nombre del binding</span>
              <input
                type="text"
                value={name}
                disabled={readOnly}
                onChange={(event) => setName(event.target.value)}
                className="w-full rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 outline-none transition focus:border-accent"
              />
            </label>

            <label className="space-y-2 text-sm text-slate-600">
              <span className="font-medium text-ink">Dataset</span>
              <select
                value={datasetId}
                disabled={readOnly}
                onChange={(event) => setDatasetId(event.target.value)}
                className="w-full rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 outline-none transition focus:border-accent"
              >
                <option value="">Selecciona dataset</option>
                {datasets.map((dataset) => (
                  <option key={dataset.id} value={dataset.id}>
                    {dataset.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="space-y-2 text-sm text-slate-600">
              <span className="font-medium text-ink">Zona del layout</span>
              <select
                value={zoneKey}
                disabled={readOnly}
                onChange={(event) => setZoneKey(event.target.value)}
                className="w-full rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 outline-none transition focus:border-accent"
              >
                {zones.map((zone) => (
                  <option key={zone.key} value={zone.key}>
                    {zone.label}
                  </option>
                ))}
              </select>
            </label>

            <div className="grid gap-4 sm:grid-cols-3">
              <label className="space-y-2 text-sm text-slate-600">
                <span className="font-medium text-ink">Max filas</span>
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={maxRows}
                  disabled={readOnly}
                  onChange={(event) => setMaxRows(Number(event.target.value))}
                  className="w-full rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 outline-none transition focus:border-accent"
                />
              </label>
              <label className="space-y-2 text-sm text-slate-600">
                <span className="font-medium text-ink">Orden</span>
                <input
                  type="number"
                  min={1}
                  value={sortOrder}
                  disabled={readOnly}
                  onChange={(event) => setSortOrder(Number(event.target.value))}
                  className="w-full rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700 outline-none transition focus:border-accent"
                />
              </label>
              <label className="flex items-center gap-3 rounded-[22px] border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                <input type="checkbox" checked={isActive} disabled={readOnly} onChange={(event) => setIsActive(event.target.checked)} />
                Activo
              </label>
            </div>
          </div>

          <TemplatePresetPicker
            selectedPreset={presetKey}
            disabled={readOnly}
            onSelect={(nextPreset) => {
              setPresetKey(nextPreset);
              setMaxRows(getLayoutBindingPreset(nextPreset).default_max_rows);
              setFieldDrafts(buildSuggestedBindingFields(nextPreset, datasetColumns, fieldDrafts));
              onValidated(null);
            }}
          />

          {loadingColumns ? (
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-5 text-sm text-slate-600">
              Cargando columnas del dataset actual...
            </div>
          ) : (
            <DatasetFieldMapper preset={selectedPreset} columns={datasetColumns} fields={fieldDrafts} onChange={setFieldDrafts} disabled={readOnly} />
          )}

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void handleValidate()}
              disabled={validating || !datasetId}
              className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
            >
              {validating ? <Loader2 className="h-4 w-4 animate-spin" /> : <WandSparkles className="h-4 w-4" />}
              Validar preview
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving || readOnly || !datasetId}
              className="inline-flex items-center gap-2 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              Guardar binding
            </button>
            <button
              type="button"
              onClick={() => setFieldDrafts(buildSuggestedBindingFields(presetKey, datasetColumns))}
              disabled={readOnly || datasetColumns.length === 0}
              className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshCcw className="h-4 w-4" />
              Sugerir columnas
            </button>
            {selectedBindingId ? (
              <button
                type="button"
                onClick={() => void handleDelete()}
                disabled={saving || readOnly}
                className="inline-flex items-center gap-2 rounded-full border border-rose-200 bg-rose-50 px-4 py-2 text-sm font-semibold text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Trash2 className="h-4 w-4" />
                Eliminar
              </button>
            ) : null}
          </div>
        </div>
      </SectionCard>
    </div>
  );
}
