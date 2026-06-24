import { Database, LayoutPanelLeft, Link2, MonitorSmartphone } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BoundLayoutPreview } from "../../components/BoundLayoutPreview";
import { DynamicTablePreview } from "../../components/DynamicTablePreview";
import { LayoutBindingPanel } from "../../components/LayoutBindingPanel";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatLayoutBindingPreset, formatLayoutTemplate } from "../../lib/labels";
import { canWriteBranchScope } from "../../lib/rbac";
import { buildLayoutEditorPath, buildLayoutPreviewPath } from "../../lib/workspace";
import type {
  Dataset,
  Layout,
  LayoutBindingPreviewResult,
  LayoutDataBinding,
  LayoutDataPreviewPayload,
  LayoutRuntimeDataPayload,
} from "../../types/domain";

export function BranchLayoutsPage() {
  const { token, user } = useAuth();
  const { clientId, branchId } = useParams();
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [bindings, setBindings] = useState<LayoutDataBinding[]>([]);
  const [previewPayload, setPreviewPayload] = useState<LayoutDataPreviewPayload | null>(null);
  const [runtimePayload, setRuntimePayload] = useState<LayoutRuntimeDataPayload | null>(null);
  const [validationPreview, setValidationPreview] = useState<LayoutBindingPreviewResult | null>(null);
  const [selectedLayoutId, setSelectedLayoutId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const canManage = canWriteBranchScope(user?.role);
  const selectedLayout = layouts.find((layout) => layout.id === selectedLayoutId) ?? null;
  const validPreviewBindings = useMemo(
    () => previewPayload?.bindings.filter((binding) => binding.validation.valid) ?? [],
    [previewPayload],
  );
  const activeBindings = useMemo(() => bindings.filter((binding) => binding.is_active), [bindings]);
  const primaryPreview = validationPreview ?? previewPayload?.bindings[0] ?? null;

  async function loadBaseData() {
    if (!token || !clientId) {
      return;
    }

    setLoading(true);
    try {
      const [layoutsResponse, datasetsResponse] = await Promise.all([
        apiRequest<Layout[]>(`/layouts?client_id=${clientId}`, { token }),
        apiRequest<Dataset[]>(`/datasets?client_id=${clientId}`, { token }),
      ]);
      setLayouts(layoutsResponse);
      setDatasets(datasetsResponse);
      setSelectedLayoutId((current) => current ?? layoutsResponse[0]?.id ?? null);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudieron cargar layouts y datasets");
    } finally {
      setLoading(false);
    }
  }

  async function loadLayoutBindingState(layoutId: string) {
    if (!token) {
      return;
    }

    try {
      const [bindingsResponse, previewResponse, runtimeResponse] = await Promise.all([
        apiRequest<LayoutDataBinding[]>(`/layouts/${layoutId}/bindings`, { token }),
        apiRequest<LayoutDataPreviewPayload>(`/layouts/${layoutId}/data-preview`, { token }),
        apiRequest<LayoutRuntimeDataPayload>(`/layouts/${layoutId}/runtime-data`, { token }),
      ]);
      setBindings(bindingsResponse);
      setPreviewPayload(previewResponse);
      setRuntimePayload(runtimeResponse);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el estado dinámico del layout");
    }
  }

  useEffect(() => {
    void loadBaseData();
  }, [clientId, token]);

  useEffect(() => {
    if (!selectedLayoutId) {
      setBindings([]);
      setPreviewPayload(null);
      setRuntimePayload(null);
      return;
    }

    setValidationPreview(null);
    void loadLayoutBindingState(selectedLayoutId);
  }, [selectedLayoutId, token]);

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Layouts" value={String(layouts.length)} hint="Catálogo disponible en esta sucursal" tone="teal" />
        <StatCard label="Datasets" value={String(datasets.length)} hint="Fuentes listas para alimentar contenido" />
        <StatCard label="Bindings activos" value={String(activeBindings.length)} hint="Conexiones vivas entre dataset y layout" tone="orange" />
        <StatCard label="Preview válidos" value={String(validPreviewBindings.length)} hint="Bindings resueltos sin errores" tone="teal" />
      </section>

      <div className="grid gap-5 xl:grid-cols-[0.9fr_1.1fr]">
        <SectionCard
          title="Layouts visibles"
          subtitle="Selecciona el layout que quieres alimentar con datos dinámicos, abrir en el editor visual o revisar en preview."
        >
          <div className="space-y-4">
            {loading ? (
              <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">Cargando layouts y datasets...</div>
            ) : null}

            {layouts.length > 0 ? (
              layouts.map((layout) => {
                const isActive = layout.id === selectedLayoutId;
                const layoutPreviewCount = previewPayload?.layout.id === layout.id ? previewPayload.bindings.length : undefined;

                return (
                  <article
                    key={layout.id}
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelectedLayoutId(layout.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setSelectedLayoutId(layout.id);
                      }
                    }}
                    className={[
                      "w-full rounded-[28px] border p-5 text-left transition",
                      isActive ? "border-accent bg-accentSoft/40 shadow-card" : "border-slate-200 bg-white hover:border-accent/40",
                    ].join(" ")}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p className="font-display text-2xl text-ink">{layout.name}</p>
                        <p className="mt-2 text-sm text-slate-500">
                          {formatLayoutTemplate(layout.template)} · {layout.canvas_width}x{layout.canvas_height}
                        </p>
                      </div>
                      {layout.is_default ? (
                        <span className="rounded-full bg-white px-4 py-2 text-xs font-semibold uppercase tracking-wide text-accent">
                          Predeterminado
                        </span>
                      ) : null}
                    </div>

                    <div className="mt-5 grid gap-3 md:grid-cols-3">
                      <div className="rounded-2xl bg-white/80 px-4 py-3 text-sm text-slate-700">{layout.zones.length || 1} zona(s)</div>
                      <div className="rounded-2xl bg-white/80 px-4 py-3 text-sm text-slate-700">{layoutPreviewCount ?? 0} binding(s)</div>
                      <div className="rounded-2xl bg-white/80 px-4 py-3 text-sm text-slate-700">{layout.client_id === clientId ? "Cliente actual" : "Compartido"}</div>
                    </div>

                    <div className="mt-4 flex flex-wrap gap-3">
                      <Link
                        to={buildLayoutEditorPath(clientId ?? "", branchId ?? "", layout.id)}
                        className="rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                        onClick={(event) => event.stopPropagation()}
                      >
                        Abrir editor
                      </Link>
                      <Link
                        to={buildLayoutPreviewPath(clientId ?? "", branchId ?? "", layout.id)}
                        className="rounded-full border border-accent/30 bg-accentSoft/35 px-4 py-2 text-sm font-semibold text-ink transition hover:border-accent"
                        onClick={(event) => event.stopPropagation()}
                      >
                        Abrir preview
                      </Link>
                    </div>
                  </article>
                );
              })
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                Todavía no existen layouts visibles para esta sucursal.
              </div>
            )}
          </div>
        </SectionCard>

        <SectionCard
          title="Resumen operativo"
          subtitle="Vista rápida del layout seleccionado, el dataset disponible y el runtime dinámico preparado para el player futuro."
        >
          {selectedLayout ? (
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-4">
                  <div className="flex items-center gap-3">
                    <span className="rounded-2xl bg-white p-3 text-accent">
                      <LayoutPanelLeft className="h-5 w-5" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-ink">{selectedLayout.name}</p>
                      <p className="text-sm text-slate-500">{formatLayoutTemplate(selectedLayout.template)}</p>
                    </div>
                  </div>
                  <p className="mt-4 text-sm text-slate-700">
                    Canvas: {selectedLayout.canvas_width}x{selectedLayout.canvas_height} · {selectedLayout.zones.length || 1} zona(s)
                  </p>
                </article>

                <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-4">
                  <div className="flex items-center gap-3">
                    <span className="rounded-2xl bg-white p-3 text-accent">
                      <Database className="h-5 w-5" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-ink">{datasets.length} dataset(s) disponibles</p>
                      <p className="text-sm text-slate-500">Listos para binding en este cliente</p>
                    </div>
                  </div>
                  <p className="mt-4 text-sm text-slate-700">
                    Runtime preparado: {runtimePayload?.bindings.length ?? 0} binding(s) · player_ready = {runtimePayload?.player_ready ? "true" : "false"}
                  </p>
                </article>
              </div>

              <div className="grid gap-4 md:grid-cols-3">
                {activeBindings.slice(0, 3).map((binding) => (
                  <article key={binding.id} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                    <p className="text-sm font-semibold text-ink">{binding.name}</p>
                    <p className="mt-1 text-sm text-slate-500">{formatLayoutBindingPreset(binding.preset_key)}</p>
                    <p className="mt-3 text-sm text-slate-700">Zona: {binding.zone_key ?? "Principal"}</p>
                  </article>
                ))}
                {activeBindings.length === 0 ? (
                  <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600 md:col-span-3">
                    Todavía no hay bindings activos para este layout.
                  </div>
                ) : null}
              </div>
            </div>
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Selecciona un layout para empezar a mapear datasets.
            </div>
          )}
        </SectionCard>
      </div>

      <LayoutBindingPanel
        token={token}
        layout={selectedLayout}
        datasets={datasets}
        bindings={bindings}
        readOnly={!canManage}
        onValidated={setValidationPreview}
        onChanged={async () => {
          if (selectedLayoutId) {
            await loadLayoutBindingState(selectedLayoutId);
          }
        }}
      />

      <div className="grid gap-5 xl:grid-cols-[1.02fr_0.98fr]">
        <SectionCard
          title="Preview dinámico"
          subtitle="Lectura previa del dataset ya resuelto. Sirve para revisar columnas, filas y orden final antes de enviar al player."
        >
          {primaryPreview ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-3">
                <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
                  {primaryPreview.preset.label}
                </span>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
                  {primaryPreview.dataset?.name ?? "Sin dataset"}
                </span>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold uppercase tracking-[0.18em] text-slate-600">
                  Zona {primaryPreview.zone?.label ?? "Principal"}
                </span>
              </div>

              {primaryPreview.validation.errors.length > 0 ? (
                <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">
                  {primaryPreview.validation.errors.join(" ")}
                </div>
              ) : null}
              {primaryPreview.validation.warnings.length > 0 ? (
                <div className="rounded-[24px] bg-amber-50 px-5 py-4 text-sm text-amber-800">
                  {primaryPreview.validation.warnings.join(" ")}
                </div>
              ) : null}

              <DynamicTablePreview headers={primaryPreview.headers} rows={primaryPreview.rows} />
            </div>
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Valida o guarda un binding para ver aquí la tabla dinámica resuelta.
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Runtime listo para player"
          subtitle="Snapshot del layout con bindings activos. Esta misma estructura es la que quedará lista para el player en la siguiente fase."
        >
          {runtimePayload ? (
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                  <div className="flex items-center gap-3">
                    <span className="rounded-2xl bg-white p-3 text-accent">
                      <Link2 className="h-5 w-5" />
                    </span>
                    <div>
                      <p className="font-semibold text-ink">{runtimePayload.bindings.length} binding(s)</p>
                      <p className="text-slate-500">Incluidos en runtime-data</p>
                    </div>
                  </div>
                </article>
                <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                  <div className="flex items-center gap-3">
                    <span className="rounded-2xl bg-white p-3 text-accent">
                      <MonitorSmartphone className="h-5 w-5" />
                    </span>
                    <div>
                      <p className="font-semibold text-ink">player_ready = {runtimePayload.player_ready ? "true" : "false"}</p>
                      <p className="text-slate-500">Preparado sin tocar el player real</p>
                    </div>
                  </div>
                </article>
              </div>

              <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-4 text-sm text-slate-700">
                <p className="font-semibold text-ink">Generado</p>
                <p className="mt-2">{new Date(runtimePayload.generated_at).toLocaleString("es-MX")}</p>
              </div>
            </div>
          ) : (
            <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
              Selecciona un layout para cargar su runtime-data.
            </div>
          )}
        </SectionCard>
      </div>

      {selectedLayout && previewPayload ? (
        <BoundLayoutPreview layout={selectedLayout} zones={previewPayload.zones} bindings={previewPayload.bindings} />
      ) : null}
    </div>
  );
}
