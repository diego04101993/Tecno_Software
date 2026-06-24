import { Eye, Layers3, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { DatasetBindingInspector } from "../../components/DatasetBindingInspector";
import { LayoutCanvas } from "../../components/LayoutCanvas";
import { LayoutPreviewStage } from "../../components/LayoutPreviewStage";
import { LayoutRevisionBar } from "../../components/LayoutRevisionBar";
import { PropertiesInspector } from "../../components/PropertiesInspector";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { WidgetLibraryPanel } from "../../components/WidgetLibraryPanel";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import {
  clampWithinCanvas,
  createDefaultRegion,
  createDefaultWidget,
  removeRegion,
  removeWidget,
  updateRegion,
  updateWidget,
  type SelectionTarget,
} from "../../lib/layoutEditor";
import { canWriteBranchScope } from "../../lib/rbac";
import { buildBranchLayoutsPath, buildLayoutPreviewPath } from "../../lib/workspace";
import type {
  Dataset,
  LayoutDataBinding,
  LayoutEditorPayload,
  LayoutEditorState,
  LayoutRegion,
  LayoutRevision,
  LayoutWidget,
  LayoutWidgetType,
} from "../../types/domain";

function mergeWidgetBindingPreview(state: LayoutEditorState, payload: LayoutEditorPayload | null) {
  const bindingPreviewMap = new Map(
    (payload?.binding_preview.bindings ?? [])
      .filter((item) => item.binding?.id)
      .map((item) => [item.binding!.id, item]),
  );

  return state.widgets.map((widget) => {
    const bindingId = widget.binding_ref.binding_id;
    if (widget.widget_type !== "dataset_table" || !bindingId || !bindingPreviewMap.has(bindingId)) {
      return widget;
    }
    return {
      ...widget,
      binding_preview: bindingPreviewMap.get(bindingId),
    };
  });
}

export function LayoutEditorPage() {
  const { token, user } = useAuth();
  const { clientId, branchId, layoutId } = useParams();
  const [payload, setPayload] = useState<LayoutEditorPayload | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [bindings, setBindings] = useState<LayoutDataBinding[]>([]);
  const [editorState, setEditorState] = useState<LayoutEditorState | null>(null);
  const [activeRevision, setActiveRevision] = useState<LayoutRevision | null>(null);
  const [selectedTarget, setSelectedTarget] = useState<SelectionTarget>({ kind: "canvas" });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const canManage = canWriteBranchScope(user?.role);

  const selectedWidget = useMemo(
    () => (selectedTarget.kind === "widget" ? editorState?.widgets.find((widget) => widget.id === selectedTarget.id) ?? null : null),
    [editorState?.widgets, selectedTarget],
  );
  const selectedRegion = useMemo(
    () => (selectedTarget.kind === "region" ? editorState?.regions.find((region) => region.id === selectedTarget.id) ?? null : null),
    [editorState?.regions, selectedTarget],
  );
  const hasUnsavedChanges = useMemo(() => {
    if (!editorState || !activeRevision) {
      return false;
    }
    return JSON.stringify(editorState) !== JSON.stringify(activeRevision.editor_state_json);
  }, [activeRevision, editorState]);
  const previewWidgets = useMemo(
    () => (editorState ? mergeWidgetBindingPreview(editorState, payload) : []),
    [editorState, payload],
  );
  const activeBindingCount = useMemo(() => bindings.filter((binding) => binding.is_active).length, [bindings]);

  async function persistCurrentRevision() {
    if (!token || !layoutId || !activeRevision || !editorState) {
      return null;
    }

    const saved = await apiRequest<LayoutRevision>(`/layouts/${layoutId}/revisions/${activeRevision.id}`, {
      method: "PATCH",
      token,
      body: {
        editor_state_json: editorState,
      },
    });
    setActiveRevision(saved);
    setEditorState(saved.editor_state_json);
    return saved;
  }

  async function loadEditorState(preferredRevisionId?: string | null) {
    if (!token || !clientId || !layoutId) {
      return;
    }

    setLoading(true);
    try {
      const [editorPayload, datasetsResponse, bindingsResponse] = await Promise.all([
        apiRequest<LayoutEditorPayload>(`/layouts/${layoutId}/editor-state`, { token }),
        apiRequest<Dataset[]>(`/datasets?client_id=${clientId}`, { token }),
        apiRequest<LayoutDataBinding[]>(`/layouts/${layoutId}/bindings`, { token }),
      ]);

      const nextRevision =
        editorPayload.revisions.find((revision) => revision.id === preferredRevisionId) ?? editorPayload.active_revision;

      setPayload(editorPayload);
      setDatasets(datasetsResponse);
      setBindings(bindingsResponse);
      setActiveRevision(nextRevision);
      setEditorState(nextRevision.editor_state_json);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el editor del layout");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadEditorState();
  }, [clientId, layoutId, token]);

  useEffect(() => {
    if (!editorState) {
      return;
    }

    if (selectedTarget.kind === "widget" && !editorState.widgets.some((widget) => widget.id === selectedTarget.id)) {
      setSelectedTarget({ kind: "canvas" });
    }
    if (selectedTarget.kind === "region" && !editorState.regions.some((region) => region.id === selectedTarget.id)) {
      setSelectedTarget({ kind: "canvas" });
    }
  }, [editorState, selectedTarget]);

  function patchWidget(widgetId: string, patch: Partial<LayoutWidget>) {
    setEditorState((current) => {
      if (!current) {
        return current;
      }
      return updateWidget(current, widgetId, (widget) => {
        const next = clampWithinCanvas(current, {
          x: patch.x ?? widget.x,
          y: patch.y ?? widget.y,
          width: patch.width ?? widget.width,
          height: patch.height ?? widget.height,
        });
        return {
          ...widget,
          ...patch,
          x: next.x,
          y: next.y,
          width: next.width,
          height: next.height,
        };
      });
    });
  }

  function patchRegion(regionId: string, patch: Partial<LayoutRegion>) {
    setEditorState((current) => {
      if (!current) {
        return current;
      }
      return updateRegion(current, regionId, (region) => {
        const next = clampWithinCanvas(current, {
          x: patch.x ?? region.x,
          y: patch.y ?? region.y,
          width: patch.width ?? region.width,
          height: patch.height ?? region.height,
        });
        return {
          ...region,
          ...patch,
          x: next.x,
          y: next.y,
          width: next.width,
          height: next.height,
        };
      });
    });
  }

  function patchWidgetProps(widgetId: string, patch: Record<string, unknown>) {
    setEditorState((current) => {
      if (!current) {
        return current;
      }
      return updateWidget(current, widgetId, (widget) => ({
        ...widget,
        props_json: {
          ...widget.props_json,
          ...patch,
        },
      }));
    });
  }

  function attachBinding(bindingId: string, datasetId: string) {
    setEditorState((current) => {
      if (!current || !selectedWidget) {
        return current;
      }
      return updateWidget(current, selectedWidget.id, (widget) => ({
        ...widget,
        binding_ref: {
          binding_id: bindingId,
          dataset_id: datasetId,
        },
      }));
    });
  }

  function handleDeleteSelected() {
    setEditorState((current) => {
      if (!current) {
        return current;
      }
      if (selectedTarget.kind === "widget") {
        return removeWidget(current, selectedTarget.id);
      }
      if (selectedTarget.kind === "region") {
        return removeRegion(current, selectedTarget.id);
      }
      return current;
    });
    setSelectedTarget({ kind: "canvas" });
  }

  async function saveDraft() {
    if (!activeRevision || !editorState) {
      return;
    }
    setSaving(true);
    try {
      const saved = await persistCurrentRevision();
      if (!saved) {
        return;
      }
      setActiveRevision(saved);
      setEditorState(saved.editor_state_json);
      await loadEditorState(saved.id);
      setInfo("Borrador guardado correctamente.");
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo guardar el borrador");
    } finally {
      setSaving(false);
    }
  }

  async function createRevision() {
    if (!token || !layoutId || !activeRevision) {
      return;
    }
    setSaving(true);
    try {
      const sourceRevisionId =
        hasUnsavedChanges && activeRevision.status !== "published" ? (await persistCurrentRevision())?.id ?? activeRevision.id : activeRevision.id;
      const created = await apiRequest<LayoutRevision>(`/layouts/${layoutId}/revisions`, {
        method: "POST",
        token,
        body: {
          name: `${payload?.layout.name ?? "Layout"} Draft`,
          clone_from_revision_id: sourceRevisionId,
        },
      });
      await loadEditorState(created.id);
      setSelectedTarget({ kind: "canvas" });
      setInfo("Nueva revisión creada a partir del estado actual.");
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo crear la nueva revisión");
    } finally {
      setSaving(false);
    }
  }

  async function publishRevision() {
    if (!token || !layoutId || !activeRevision) {
      return;
    }
    setSaving(true);
    try {
      const targetRevisionId =
        hasUnsavedChanges && activeRevision.status !== "published" ? (await persistCurrentRevision())?.id ?? activeRevision.id : activeRevision.id;
      const published = await apiRequest<LayoutRevision>(`/layouts/${layoutId}/revisions/${targetRevisionId}/publish`, {
        method: "POST",
        token,
      });
      await loadEditorState(published.id);
      setInfo("Revisión publicada y sincronizada con el layout activo.");
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo publicar la revisión");
    } finally {
      setSaving(false);
    }
  }

  if (!editorState || !payload || !activeRevision) {
    return (
      <div className="space-y-4">
        {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}
        <div className="rounded-[32px] border border-white/70 bg-card/90 px-6 py-8 text-sm text-slate-600 shadow-card backdrop-blur">
          {loading ? "Cargando editor visual..." : "No se pudo cargar el editor visual del layout."}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}
      {info ? <div className="rounded-[24px] bg-emerald-50 px-5 py-4 text-sm text-emerald-700">{info}</div> : null}

      <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Layout Editor Visual</p>
            <h1 className="mt-2 font-display text-4xl text-ink">{payload.layout.name}</h1>
            <p className="mt-3 max-w-3xl text-sm text-slate-600">
              Diseña regiones, capas, overlays PNG, tablas dinámicas y widgets compatibles con campañas, preview, datasets, bindings y el runtime futuro del player.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Link
              to={buildBranchLayoutsPath(clientId ?? "", branchId ?? "")}
              className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
            >
              Volver a layouts
            </Link>
            <Link
              to={buildLayoutPreviewPath(clientId ?? "", branchId ?? "", payload.layout.id)}
              className="rounded-full border border-accent/30 bg-accentSoft/35 px-5 py-3 text-sm font-semibold text-ink transition hover:border-accent"
            >
              <span className="inline-flex items-center gap-2">
                <Eye className="h-4 w-4" />
                Abrir preview
              </span>
            </Link>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Regiones" value={String(editorState.regions.length)} hint="Estructura publicada compatible" tone="teal" />
        <StatCard label="Widgets" value={String(editorState.widgets.length)} hint="Capas visuales del layout" />
        <StatCard label="Bindings activos" value={String(activeBindingCount)} hint="Tablas dinámicas conectadas" tone="orange" />
        <StatCard
          label="Resolución"
          value={`${editorState.canvas.width}x${editorState.canvas.height}`}
          hint="Horizontal, vertical o personalizada"
        />
      </section>

      <LayoutRevisionBar
        revisions={payload.revisions}
        activeRevision={activeRevision}
        canManage={canManage && !saving}
        hasUnsavedChanges={hasUnsavedChanges}
        canvasWidth={editorState.canvas.width}
        canvasHeight={editorState.canvas.height}
        onSelectRevision={(revisionId) => {
          const revision = payload.revisions.find((item) => item.id === revisionId);
          if (!revision) {
            return;
          }
          setActiveRevision(revision);
          setEditorState(revision.editor_state_json);
          setSelectedTarget({ kind: "canvas" });
          setInfo(null);
        }}
        onSaveDraft={() => void saveDraft()}
        onCreateDraft={() => void createRevision()}
        onPublish={() => void publishRevision()}
        onCanvasPresetChange={(width, height) => {
          setEditorState((current) =>
            current
              ? {
                  ...current,
                  canvas: {
                    ...current.canvas,
                    width,
                    height,
                  },
                }
              : current,
          );
        }}
      />

      <div className="grid gap-5 xl:grid-cols-[0.84fr_1.16fr]">
        <div className="space-y-5">
          <WidgetLibraryPanel
            supportedWidgets={payload.supported_widget_types}
            futureWidgets={payload.future_widget_types}
            canManage={canManage && !saving}
            onAddRegion={() => {
              setEditorState((current) => (current ? { ...current, regions: [...current.regions, createDefaultRegion(current)] } : current));
            }}
            onAddWidget={(widgetType: LayoutWidgetType) => {
              setEditorState((current) => {
                if (!current) {
                  return current;
                }
                const selectedRegionId = selectedTarget.kind === "region" ? selectedTarget.id : selectedWidget?.region_id ?? null;
                return {
                  ...current,
                  widgets: [...current.widgets, createDefaultWidget(widgetType, current, selectedRegionId)],
                };
              });
            }}
          />

          <PropertiesInspector
            state={editorState}
            selectedWidget={selectedWidget}
            selectedRegion={selectedRegion}
            canManage={canManage && !saving}
            onCanvasChange={(width, height, backgroundColor) =>
              setEditorState((current) =>
                current
                  ? {
                      ...current,
                      canvas: {
                        ...current.canvas,
                        width: Math.max(240, Math.round(width)),
                        height: Math.max(240, Math.round(height)),
                        background_color: backgroundColor,
                      },
                    }
                  : current,
              )
            }
            onWidgetChange={patchWidget}
            onWidgetPropsChange={patchWidgetProps}
            onRegionChange={patchRegion}
            onDeleteSelected={handleDeleteSelected}
          />
        </div>

        <div className="space-y-5">
          <LayoutCanvas
            state={editorState}
            selectedTarget={selectedTarget}
            canManage={canManage && !saving}
            onSelectTarget={setSelectedTarget}
            onWidgetPatch={patchWidget}
            onRegionPatch={patchRegion}
          />

          <SectionCard
            title="Preview inmediato"
            subtitle="Simulación de lectura del layout actual con capas, overlays PNG y bindings dinámicos antes de guardar o publicar."
          >
            <LayoutPreviewStage state={editorState} widgets={previewWidgets} scale={0.38} showRegions />
          </SectionCard>
        </div>
      </div>

      <DatasetBindingInspector
        token={token}
        layoutId={payload.layout.id}
        selectedWidget={selectedWidget}
        datasets={datasets}
        bindings={bindings}
        canManage={canManage && !saving}
        onAttachBinding={attachBinding}
        onBindingSaved={async () => {
          await loadEditorState(activeRevision.id);
        }}
      />

      <SectionCard
        title="Compatibilidad operativa"
        subtitle="Este editor sigue publicando un snapshot compatible con campañas, preview, datasets, bindings y runtime futuro del player."
      >
        <div className="grid gap-4 md:grid-cols-3">
          <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-ink">
              <Layers3 className="h-4 w-4 text-accent" />
              Capas y regiones
            </div>
            <p className="mt-3 text-sm text-slate-600">Soporte para fondo, contenido, overlays, branding y marcos PNG en múltiples regiones.</p>
          </article>
          <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-ink">
              <Save className="h-4 w-4 text-accent" />
              Borrador y publicación
            </div>
            <p className="mt-3 text-sm text-slate-600">Las revisiones separan trabajo en progreso del layout publicado sin romper el contenido activo.</p>
          </article>
          <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
            <div className="inline-flex items-center gap-2 text-sm font-semibold text-ink">
              <Eye className="h-4 w-4 text-accent" />
              Runtime futuro
            </div>
            <p className="mt-3 text-sm text-slate-600">El snapshot queda listo para que el player futuro renderice widgets, datasets y overlays sin rehacer el CMS.</p>
          </article>
        </div>
      </SectionCard>
    </div>
  );
}
