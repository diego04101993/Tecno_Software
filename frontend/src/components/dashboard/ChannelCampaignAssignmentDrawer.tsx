import { ArrowRightLeft, RadioTower, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import { formatChannelMode } from "../../lib/labels";
import type { Branch, Campaign, CampaignAssignmentResult, Channel } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";

type AssignmentNotice =
  | {
      tone: "success" | "info";
      message: string;
    }
  | null;

export function ChannelCampaignAssignmentDrawer({
  open,
  token,
  branch,
  channel,
  campaigns,
  currentCampaignId,
  currentCampaignLabel,
  onClose,
  onAssigned,
}: {
  open: boolean;
  token: string | null;
  branch: Branch | null;
  channel: Channel | null;
  campaigns: Campaign[];
  currentCampaignId: string | null;
  currentCampaignLabel: string;
  onClose: () => void;
  onAssigned: (result: CampaignAssignmentResult, campaign: Campaign | null) => Promise<void> | void;
}) {
  const [selectedCampaignId, setSelectedCampaignId] = useState("");
  const [notice, setNotice] = useState<AssignmentNotice>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPublishing, setIsPublishing] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }

    setSelectedCampaignId(currentCampaignId ?? campaigns[0]?.id ?? "");
    setNotice(null);
    setError(null);
  }, [campaigns, currentCampaignId, open, channel?.id]);

  const selectedCampaign = useMemo(
    () => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null,
    [campaigns, selectedCampaignId],
  );

  if (!open || !branch || !channel) {
    return null;
  }

  const targetChannel = channel;

  async function handlePublish() {
    if (!token || !selectedCampaignId) {
      return;
    }

    const nextCampaignName = selectedCampaign?.name ?? "la campaña seleccionada";
    const confirmationMessage =
      currentCampaignId && currentCampaignId !== selectedCampaignId && currentCampaignLabel.trim()
        ? `Esta pantalla ya tiene la campaña "${currentCampaignLabel}". ¿Seguro que quieres reemplazarla por "${nextCampaignName}"?`
        : `¿Seguro que quieres enviar la campaña "${nextCampaignName}" a esta pantalla?`;

    if (!window.confirm(confirmationMessage)) {
      return;
    }

    setIsPublishing(true);
    setError(null);

    try {
      const result = await apiRequest<CampaignAssignmentResult>(`/channels/${targetChannel.id}/campaign-assignment`, {
        method: "PUT",
        token,
        body: {
          campaign_id: selectedCampaignId,
          priority: 1,
        },
      });

      setNotice({
        tone: result.assignment_status === "existing" ? "info" : "success",
        message:
          result.assignment_status === "existing"
            ? `La campaña ${nextCampaignName} ya estaba publicada en esta pantalla.`
            : result.assignment_status === "replaced"
              ? `Listo, se cambió a ${nextCampaignName}.`
            : `Listo, se envió ${nextCampaignName} a esta pantalla.`,
      });
      await onAssigned(result, selectedCampaign);
    } catch (nextError) {
      setNotice(null);
      setError(getApiErrorMessage(nextError, "No se pudo publicar la campaña en esta pantalla."));
    } finally {
      setIsPublishing(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />
      <aside className="h-screen w-[min(95vw,1100px)] max-w-none overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white px-6 py-6 shadow-2xl lg:px-8">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Publicar campaña</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{targetChannel.name}</h3>
            <p className="mt-2 text-sm text-slate-600">Publica o cambia la campaña de esta pantalla sin salir del dashboard del cliente.</p>
          </div>
          <button
            aria-label="Cerrar drawer"
            className="rounded-2xl border border-slate-200 p-3 text-slate-500 transition hover:border-slate-300 hover:text-ink"
            type="button"
            onClick={onClose}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {error ? <div className="mt-5 rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
        {notice ? (
          <div
            className={[
              "mt-5 rounded-[20px] px-4 py-3 text-sm",
              notice.tone === "success" ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900",
            ].join(" ")}
          >
            {notice.message}
          </div>
        ) : null}

        <div className="mt-6 space-y-4">
          <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
            <div className="flex items-start gap-4">
              <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                <ArrowRightLeft className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Sucursal</p>
                <p className="mt-2 truncate font-semibold text-ink" title={branch.name}>
                  {branch.name}
                </p>
                <p className="mt-1 text-sm text-slate-500">{branch.code}</p>
              </div>
            </div>
          </div>

          <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
            <div className="flex items-start gap-4">
              <span className="rounded-2xl bg-white p-3 text-slate-600 shadow-sm">
                <RadioTower className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Pantalla / canal</p>
                <p className="mt-2 truncate font-semibold text-ink" title={channel.name}>
                  {targetChannel.name}
                </p>
                <p className="mt-1 text-sm text-slate-500">
                  {formatChannelMode(targetChannel.mode)} · {targetChannel.resolution_width}x{targetChannel.resolution_height}
                </p>
                <p className="mt-1 text-sm text-slate-500">Player code: {targetChannel.channel_code}</p>
              </div>
            </div>
          </div>

          <div className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
            <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Campaña visible actual</p>
            <p className="mt-2 font-semibold text-ink">{currentCampaignLabel}</p>
          </div>

          <div>
            <label className="mb-2 block text-sm font-semibold text-slate-700">Campaña disponible del cliente</label>
            <select value={selectedCampaignId} onChange={(event) => setSelectedCampaignId(event.target.value)}>
              {campaigns.length === 0 ? <option value="">No hay campañas disponibles</option> : null}
              {campaigns.map((campaign) => (
                <option key={campaign.id} value={campaign.id}>
                  {campaign.name}
                </option>
              ))}
            </select>
          </div>

          <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
            Esta acción reemplaza la campaña activa de esta pantalla sin tocar Operación ni las publicaciones masivas.
          </div>

          <div className="flex flex-wrap gap-3">
            <button
              className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
              disabled={!selectedCampaignId || campaigns.length === 0 || isPublishing}
              type="button"
              onClick={() => {
                void handlePublish();
              }}
            >
              {isPublishing ? "Publicando..." : "Publicar en esta pantalla"}
            </button>
            <button
              className="rounded-[20px] border border-slate-200 px-5 py-4 font-semibold text-slate-700 transition hover:border-slate-300 hover:text-ink"
              type="button"
              onClick={onClose}
            >
              Cerrar
            </button>
          </div>
        </div>
      </aside>
    </div>
  );
}

