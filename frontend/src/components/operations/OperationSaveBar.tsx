import { CheckCircle2, Clapperboard, LoaderCircle, PlusCircle } from "lucide-react";

import { OperationPlaybackModeBar } from "./OperationPlaybackModeBar";

type OperationSaveBarProps = {
  activeCampaignName: string | null;
  campaignCount: number;
  canEdit: boolean;
  isSaving: boolean;
  playbackMode: "sequential" | "random";
  onCreateCampaign: () => void;
  onChangePlaybackMode: (mode: "sequential" | "random") => void;
};

export function OperationSaveBar({
  activeCampaignName,
  campaignCount,
  canEdit,
  isSaving,
  playbackMode,
  onCreateCampaign,
  onChangePlaybackMode,
}: OperationSaveBarProps) {
  return (
    <section className="min-w-0 overflow-hidden rounded-[24px] border border-slate-200 bg-white/95 px-5 py-4 shadow-sm backdrop-blur">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            <span className="text-accent">Campaign Studio</span>
            <span className="rounded-full bg-slate-100 px-3 py-1.5">{campaignCount} campana(s)</span>
            <span className="rounded-full bg-slate-100 px-3 py-1.5 text-slate-600">Loop infinito</span>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-display text-2xl text-ink xl:text-[2rem]">Operacion diaria</h1>
            <span className="inline-flex min-w-0 items-center gap-2 rounded-full bg-slate-100 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-600">
              <Clapperboard className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">{activeCampaignName ?? "Sin campana activa"}</span>
            </span>
          </div>

          <p className="text-sm text-slate-600">Los cambios de timeline, biblioteca y modo se aplican al instante sobre la campana activa.</p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span
            className={[
              "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-xs font-semibold uppercase tracking-[0.16em]",
              isSaving ? "border-amber-200 bg-amber-50 text-amber-700" : "border-emerald-200 bg-emerald-50 text-emerald-700",
            ].join(" ")}
          >
            {isSaving ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
            {isSaving ? "Sincronizando..." : "Sincronizado"}
          </span>
          <button
            className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!canEdit}
            type="button"
            onClick={onCreateCampaign}
          >
            <PlusCircle className="h-4 w-4" />
            Crear campana
          </button>
        </div>
      </div>

      <div className="mt-3 border-t border-slate-200 pt-3">
        <OperationPlaybackModeBar mode={playbackMode} canEdit={canEdit} onChange={onChangePlaybackMode} />
      </div>
    </section>
  );
}
