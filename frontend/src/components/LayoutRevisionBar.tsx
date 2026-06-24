import { History, Save, Send, Sparkles } from "lucide-react";

import { canvasPresets, detectCanvasPreset } from "../lib/layoutEditor";
import { formatLayoutRevisionStatus } from "../lib/labels";
import type { LayoutRevision } from "../types/domain";

type LayoutRevisionBarProps = {
  revisions: LayoutRevision[];
  activeRevision: LayoutRevision | null;
  canManage: boolean;
  hasUnsavedChanges: boolean;
  canvasWidth: number;
  canvasHeight: number;
  onSelectRevision: (revisionId: string) => void;
  onSaveDraft: () => void;
  onCreateDraft: () => void;
  onPublish: () => void;
  onCanvasPresetChange: (width: number, height: number) => void;
};

export function LayoutRevisionBar({
  revisions,
  activeRevision,
  canManage,
  hasUnsavedChanges,
  canvasWidth,
  canvasHeight,
  onSelectRevision,
  onSaveDraft,
  onCreateDraft,
  onPublish,
  onCanvasPresetChange,
}: LayoutRevisionBarProps) {
  const activePreset = detectCanvasPreset(canvasWidth, canvasHeight);

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="flex flex-col gap-4 border-b border-slate-200 pb-5 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h2 className="font-display text-2xl text-ink">Revisiones y publicación</h2>
          <p className="mt-1 text-sm text-slate-600">Administra borradores, resolución del canvas y publicación del layout activo sin romper campañas ni preview.</p>
        </div>
        <div className="flex flex-wrap gap-3">
          <div className="rounded-full bg-slate-100 px-4 py-2 text-sm font-semibold text-slate-700">
            {activeRevision ? formatLayoutRevisionStatus(activeRevision.status) : "Sin revisión"}
          </div>
          {hasUnsavedChanges ? (
            <div className="rounded-full bg-amber-100 px-4 py-2 text-sm font-semibold text-amber-800">Cambios sin guardar</div>
          ) : null}
        </div>
      </div>

      <div className="mt-5 grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-3">
            {canvasPresets.map((preset) => (
              <button
                key={preset.label}
                type="button"
                onClick={() => onCanvasPresetChange(preset.width, preset.height)}
                className={[
                  "rounded-[24px] border px-4 py-4 text-left transition",
                  activePreset === preset.label ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                ].join(" ")}
              >
                <p className="font-semibold text-ink">{preset.label}</p>
                <p className="mt-2 text-sm text-slate-500">
                  {preset.width}x{preset.height}
                </p>
              </button>
            ))}
          </div>

          <div className="flex flex-wrap gap-3">
            <button
              className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white disabled:opacity-50"
              type="button"
              disabled={!canManage}
              onClick={onSaveDraft}
            >
              <span className="inline-flex items-center gap-2">
                <Save className="h-4 w-4" />
                Guardar draft
              </span>
            </button>
            <button
              className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 disabled:opacity-50"
              type="button"
              disabled={!canManage}
              onClick={onCreateDraft}
            >
              <span className="inline-flex items-center gap-2">
                <Sparkles className="h-4 w-4" />
                Nueva revisión
              </span>
            </button>
            <button
              className="rounded-full border border-accent/30 bg-accentSoft/40 px-5 py-3 text-sm font-semibold text-ink disabled:opacity-50"
              type="button"
              disabled={!canManage}
              onClick={onPublish}
            >
              <span className="inline-flex items-center gap-2">
                <Send className="h-4 w-4" />
                Publicar revisión
              </span>
            </button>
          </div>
        </div>

        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <History className="h-5 w-5 text-accent" />
            <p className="text-sm font-semibold text-ink">Historial básico</p>
          </div>
          <div className="space-y-3">
            {revisions.length > 0 ? (
              revisions.map((revision) => (
                <button
                  key={revision.id}
                  type="button"
                  onClick={() => onSelectRevision(revision.id)}
                  className={[
                    "w-full rounded-[24px] border p-4 text-left transition",
                    activeRevision?.id === revision.id ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                  ].join(" ")}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="font-semibold text-ink">{revision.name}</p>
                      <p className="mt-1 text-sm text-slate-500">
                        Rev. {revision.revision_number} · {formatLayoutRevisionStatus(revision.status)}
                      </p>
                    </div>
                    <div className="flex flex-col gap-2 text-right text-xs text-slate-500">
                      {revision.is_current_draft ? <span>Borrador activo</span> : null}
                      {revision.is_current_published ? <span>Publicado</span> : null}
                    </div>
                  </div>
                </button>
              ))
            ) : (
              <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                Aún no hay revisiones registradas.
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
