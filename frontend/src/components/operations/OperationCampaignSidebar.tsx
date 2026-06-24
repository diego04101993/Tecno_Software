import { Clapperboard, PlusCircle, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { Campaign } from "../../types/domain";

type OperationCampaignSidebarProps = {
  campaigns: Campaign[];
  selectedCampaignId: string;
  canEdit: boolean;
  composerOpen: boolean;
  onComposerOpenChange: (open: boolean) => void;
  onSelectCampaign: (campaignId: string) => void;
  onCreateCampaign: (name: string) => Promise<void>;
  onDeleteCampaign: (campaign: Campaign) => void;
};

export function OperationCampaignSidebar({
  campaigns,
  selectedCampaignId,
  canEdit,
  composerOpen,
  onComposerOpenChange,
  onSelectCampaign,
  onCreateCampaign,
  onDeleteCampaign,
}: OperationCampaignSidebarProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [draftName, setDraftName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const activeCampaign = campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null;

  useEffect(() => {
    if (composerOpen) {
      window.setTimeout(() => inputRef.current?.focus(), 20);
    }
  }, [composerOpen]);

  async function handleCreate() {
    const normalizedName = draftName.trim();
    if (!normalizedName || submitting) {
      return;
    }

    setSubmitting(true);
    try {
      await onCreateCampaign(normalizedName);
      setDraftName("");
      onComposerOpenChange(false);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="grid h-full min-h-0 min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 overflow-hidden">
      <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Campaign Studio</p>
            <h2 className="mt-1 text-xl font-semibold text-ink">Campañas</h2>
            <p className="mt-1 text-sm text-slate-600">Centro principal para crear, seleccionar y limpiar campañas operativas.</p>
          </div>
          <button
            className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!canEdit}
            type="button"
            onClick={() => onComposerOpenChange(!composerOpen)}
          >
            <PlusCircle className="h-4 w-4" />
            Crear
          </button>
        </div>

        <div className="mt-4 grid gap-2 xl:grid-cols-[84px_minmax(0,1fr)]">
          <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-3">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Totales</p>
            <p className="mt-1 text-2xl font-semibold text-ink">{campaigns.length}</p>
          </div>
          <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-3">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Activa</p>
            <p className="mt-1 truncate text-sm font-semibold text-ink" title={activeCampaign?.name ?? "Sin campaña activa"}>
              {activeCampaign?.name ?? "Sin campaña activa"}
            </p>
            <p className="mt-1 text-xs text-slate-500">{activeCampaign ? "Lista para preview y timeline." : "Selecciona una campaña del listado para empezar."}</p>
          </div>
        </div>
      </div>

      {composerOpen ? (
        <div className="rounded-[20px] border border-slate-200 bg-white px-4 py-4 shadow-sm">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Nueva campaña</p>
          <div className="mt-3 flex flex-col gap-3">
            <input
              ref={inputRef}
              disabled={!canEdit || submitting}
              placeholder="Nombre de la campaña"
              value={draftName}
              onChange={(event) => setDraftName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void handleCreate();
                }
              }}
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-full bg-ink px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-white disabled:cursor-not-allowed disabled:opacity-60"
                disabled={!canEdit || submitting || !draftName.trim()}
                type="button"
                onClick={() => {
                  void handleCreate();
                }}
              >
                {submitting ? "Creando..." : "Crear campaña"}
              </button>
              <button
                className="rounded-full border border-slate-200 bg-white px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-600 transition hover:border-slate-300 hover:text-ink"
                disabled={submitting}
                type="button"
                onClick={() => onComposerOpenChange(false)}
              >
                Cancelar
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="min-h-0 overflow-hidden rounded-[22px] border border-slate-200 bg-white">
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Listado de campañas</p>
          <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            Scroll
          </span>
        </div>

        <div className="h-full min-h-0 overflow-y-auto p-2 pr-1">
          {campaigns.length > 0 ? (
            <div className="space-y-2">
              {campaigns.map((campaign) => {
                const isActive = campaign.id === selectedCampaignId;

                return (
                  <article
                    key={campaign.id}
                    className={[
                      "rounded-[18px] border px-3 py-3 transition",
                      isActive ? "border-cyan-300 bg-cyan-50 shadow-sm" : "border-slate-200 bg-slate-50/60 hover:border-slate-300 hover:bg-slate-50",
                    ].join(" ")}
                  >
                    <div className="flex items-start gap-3">
                      <button className="flex min-w-0 flex-1 items-start gap-3 text-left" type="button" onClick={() => onSelectCampaign(campaign.id)}>
                        <span
                          className={[
                            "mt-0.5 rounded-2xl p-2.5",
                            isActive ? "bg-white text-accent shadow-sm" : "bg-white text-slate-600",
                          ].join(" ")}
                        >
                          <Clapperboard className="h-4 w-4" />
                        </span>

                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="truncate text-sm font-semibold text-ink" title={campaign.name}>
                              {campaign.name}
                            </p>
                            <span className="rounded-full bg-white px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                              {campaign.playback_mode === "random" ? "Aleatorio" : "Secuencia"}
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-slate-500">
                            {campaign.is_active ? "Activa" : "Inactiva"} - Loop {campaign.loop_enabled ? "infinito" : "manual"}
                          </p>
                          <p className="mt-1 line-clamp-2 text-xs leading-5 text-slate-500">
                            {campaign.description?.trim() || "Lista para timeline, preview y publicación posterior desde dashboard."}
                          </p>
                        </div>
                      </button>

                      {canEdit ? (
                        <button
                          className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 transition hover:border-rose-300 hover:text-rose-600"
                          title="Eliminar campaña"
                          type="button"
                          onClick={() => onDeleteCampaign(campaign)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="grid min-h-[220px] place-items-center rounded-[18px] border border-dashed border-slate-300 bg-slate-50 px-5 text-center text-sm text-slate-600">
              Aún no hay campañas. Crea la primera para empezar a construir la secuencia.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
