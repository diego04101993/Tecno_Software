import { ArrowRightLeft, RadioTower, Trash2 } from "lucide-react";

import type { Branch, Campaign, Channel } from "../../types/domain";

type OperationCampaignAssignmentPanelProps = {
  campaigns: Campaign[];
  selectedCampaignId: string;
  onSelectCampaign: (campaignId: string) => void;
  selectedBranch: Branch | null;
  selectedChannel: Channel | null;
  canAssignCampaign: boolean;
  canDeleteCampaign: boolean;
  isPublishingCampaign: boolean;
  assignmentNotice: { tone: "success" | "info"; message: string } | null;
  onAssignCampaign: () => Promise<void>;
  onDeleteCampaign: (campaign: Campaign) => void;
};

export function OperationCampaignAssignmentPanel({
  campaigns,
  selectedCampaignId,
  onSelectCampaign,
  selectedBranch,
  selectedChannel,
  canAssignCampaign,
  canDeleteCampaign,
  isPublishingCampaign,
  assignmentNotice,
  onAssignCampaign,
  onDeleteCampaign,
}: OperationCampaignAssignmentPanelProps) {
  const selectedCampaign = campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null;

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center gap-3 border-b border-slate-200 pb-3">
        <span className="rounded-xl bg-accentSoft p-2.5 text-accent">
          <ArrowRightLeft className="h-4 w-4" />
        </span>
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Inspector operativo</p>
          <p className="mt-1 text-sm text-slate-600">Selecciona campaña, valida destino y publica en el canal correcto.</p>
        </div>

        {assignmentNotice ? (
          <div
            className={
              assignmentNotice.tone === "success"
                ? "rounded-[18px] border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-700"
                : "rounded-[18px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-800"
            }
          >
            {assignmentNotice.message}
          </div>
        ) : null}
      </div>

      <div className="mt-4 space-y-4">
        <div>
          <div className="mb-2 flex items-center justify-between gap-3">
            <label className="block text-sm font-semibold text-slate-700">Campaña activa</label>
            {selectedCampaign && canDeleteCampaign ? (
              <button
                className="inline-flex items-center gap-1 rounded-full border border-rose-200 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-rose-600 transition hover:bg-rose-50"
                type="button"
                onClick={() => onDeleteCampaign(selectedCampaign)}
              >
                <Trash2 className="h-3.5 w-3.5" />
                Eliminar
              </button>
            ) : null}
          </div>
          <select value={selectedCampaignId} onChange={(event) => onSelectCampaign(event.target.value)}>
            <option value="">Selecciona una campaña</option>
            {campaigns.map((campaign) => (
              <option key={campaign.id} value={campaign.id}>
                {campaign.name}
              </option>
            ))}
          </select>
        </div>

        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-4">
          <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Sucursal objetivo</p>
          <p className="mt-2 truncate font-semibold text-ink" title={selectedBranch?.name ?? "Sin sucursal seleccionada"}>
            {selectedBranch?.name ?? "Sin sucursal seleccionada"}
          </p>
          <p className="mt-1 text-sm text-slate-500">{selectedBranch?.code ?? "Selecciona una sucursal desde el panel izquierdo."}</p>
        </div>

        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-4">
          <div className="flex items-start gap-3">
            <span className="rounded-xl bg-white p-2.5 text-slate-600 shadow-sm">
              <RadioTower className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Canal objetivo</p>
              <p className="mt-2 truncate font-semibold text-ink" title={selectedChannel?.name ?? "Sin canal seleccionado"}>
                {selectedChannel?.name ?? "Sin canal seleccionado"}
              </p>
              <p className="mt-1 text-sm text-slate-500">
                {selectedChannel
                  ? `${selectedChannel.mode} · ${selectedChannel.resolution_width}x${selectedChannel.resolution_height}`
                  : "Selecciona un canal dentro de la sucursal activa."}
              </p>
            </div>
          </div>
        </div>

        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
          Publica la campaña elegida en el canal activo. La edición fina de la secuencia ya se controla desde el timeline de esta misma pantalla.
        </div>
      </div>

      <div className="mt-auto pt-5">
        {canAssignCampaign ? (
          <button
            className="w-full rounded-[18px] bg-ink px-5 py-4 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!selectedCampaignId || !selectedChannel || isPublishingCampaign}
            type="button"
            onClick={() => {
              void onAssignCampaign();
            }}
          >
            {isPublishingCampaign ? "Publicando..." : "Publicar en canal"}
          </button>
        ) : (
          <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
            Este rol puede revisar el flujo, pero no publicar campañas en canales.
          </div>
        )}
      </div>
    </div>
  );
}
