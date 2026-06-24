import { Eye, LayoutPanelLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { LayoutPreviewStage } from "../../components/LayoutPreviewStage";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatLayoutRevisionStatus } from "../../lib/labels";
import { buildBranchLayoutsPath, buildLayoutEditorPath } from "../../lib/workspace";
import type { LayoutPreviewPayload } from "../../types/domain";

export function LayoutPreviewPage() {
  const { token } = useAuth();
  const { clientId, branchId, layoutId } = useParams();
  const [payload, setPayload] = useState<LayoutPreviewPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function loadPreview() {
    if (!token || !layoutId) {
      return;
    }

    setLoading(true);
    try {
      const response = await apiRequest<LayoutPreviewPayload>(`/layouts/${layoutId}/preview`, { token });
      setPayload(response);
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el preview del layout");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadPreview();
  }, [layoutId, token]);

  if (!payload) {
    return (
      <div className="space-y-4">
        {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}
        <div className="rounded-[32px] border border-white/70 bg-card/90 px-6 py-8 text-sm text-slate-600 shadow-card backdrop-blur">
          {loading ? "Cargando preview del layout..." : "No se pudo cargar el preview del layout."}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Layout Preview</p>
            <h1 className="mt-2 font-display text-4xl text-ink">{payload.layout.name}</h1>
            <p className="mt-3 max-w-3xl text-sm text-slate-600">
              Simulación visual del layout publicado o del borrador activo, con capas, widgets, bindings dinámicos y estructura preparada para el player futuro.
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
              to={buildLayoutEditorPath(clientId ?? "", branchId ?? "", payload.layout.id)}
              className="rounded-full border border-accent/30 bg-accentSoft/35 px-5 py-3 text-sm font-semibold text-ink transition hover:border-accent"
            >
              Abrir editor
            </Link>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Resolución" value={`${payload.preview.canvas.width}x${payload.preview.canvas.height}`} hint="Canvas actual del layout" tone="teal" />
        <StatCard label="Regiones" value={String(payload.preview.regions.length)} hint="Áreas visibles del diseño" />
        <StatCard label="Widgets" value={String(payload.preview.widgets.length)} hint="Elementos en preview" tone="orange" />
        <StatCard
          label="Revisión"
          value={`Rev. ${payload.revision.revision_number}`}
          hint={formatLayoutRevisionStatus(payload.revision.status)}
        />
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.08fr_0.92fr]">
        <SectionCard
          title="Escenario de preview"
          subtitle="Render de lectura del layout completo, incluyendo tabla dinámica conectada, overlays PNG y widgets listos para runtime."
        >
          <LayoutPreviewStage state={payload.revision.editor_state_json} widgets={payload.preview.widgets} scale={0.42} showRegions />
        </SectionCard>

        <div className="space-y-5">
          <SectionCard
            title="Resumen de revisión"
            subtitle="Control básico del borrador o publicación activa sin tocar todavía el player real."
          >
            <div className="space-y-4">
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="inline-flex items-center gap-2 text-sm font-semibold text-ink">
                  <LayoutPanelLeft className="h-4 w-4 text-accent" />
                  Estado
                </div>
                <p className="mt-3 text-sm text-slate-600">{formatLayoutRevisionStatus(payload.revision.status)}</p>
              </article>
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="inline-flex items-center gap-2 text-sm font-semibold text-ink">
                  <Eye className="h-4 w-4 text-accent" />
                  Generado
                </div>
                <p className="mt-3 text-sm text-slate-600">{new Date(payload.preview_generated_at).toLocaleString("es-MX")}</p>
              </article>
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <p className="text-sm font-semibold text-ink">Player future-ready</p>
                <p className="mt-3 text-sm text-slate-600">player_ready = {payload.player_ready ? "true" : "false"}</p>
              </article>
            </div>
          </SectionCard>

          <SectionCard
            title="Bindings resueltos"
            subtitle="Resumen de datasets y tablas dinámicas conectadas en este snapshot."
          >
            <div className="space-y-3">
              {payload.preview.bindings.length > 0 ? (
                payload.preview.bindings.map((binding) => (
                  <article key={binding.binding?.id ?? `${binding.preset.key}-${binding.zone?.key ?? "principal"}`} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                    <p className="font-semibold text-ink">{binding.binding?.name ?? binding.preset.label}</p>
                    <p className="mt-1 text-sm text-slate-500">{binding.dataset?.name ?? "Sin dataset"} · {binding.zone?.label ?? "Zona principal"}</p>
                    <p className="mt-3 text-sm text-slate-600">
                      {binding.rows.length} fila(s) · {binding.headers.length} columna(s) · {binding.validation.valid ? "válido" : "requiere revisión"}
                    </p>
                  </article>
                ))
              ) : (
                <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                  Este layout todavía no tiene bindings dinámicos activos.
                </div>
              )}
            </div>
          </SectionCard>
        </div>
      </div>
    </div>
  );
}
