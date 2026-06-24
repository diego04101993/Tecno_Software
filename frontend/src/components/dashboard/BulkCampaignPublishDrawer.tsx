import { ArrowRightLeft, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Campaign, CampaignAssignmentResult } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";
import {
  CampaignPublishQuickActions,
  type CampaignPublishCampaignFilter,
  type CampaignPublishStatusFilter,
} from "./CampaignPublishQuickActions";
import { CampaignPublishResultSummary, type CampaignPublishSummary } from "./CampaignPublishResultSummary";
import { CampaignPublishTargetTree, type PublishTargetBranchGroup } from "./CampaignPublishTargetTree";

type BulkCampaignPublishDrawerProps = {
  open: boolean;
  token: string | null;
  mode: "branch" | "global";
  branchGroups: PublishTargetBranchGroup[];
  campaigns: Campaign[];
  onClose: () => void;
  onPublished: (summary: CampaignPublishSummary, campaign: Campaign | null) => Promise<void> | void;
};

async function runWithConcurrencyLimit<T>(items: T[], limit: number, worker: (item: T) => Promise<void>) {
  const queue = [...items];
  const concurrency = Math.max(1, Math.min(limit, queue.length || 1));

  await Promise.all(
    Array.from({ length: concurrency }, async () => {
      while (queue.length > 0) {
        const item = queue.shift();
        if (!item) {
          return;
        }
        await worker(item);
      }
    }),
  );
}

export function BulkCampaignPublishDrawer({
  open,
  token,
  mode,
  branchGroups,
  campaigns,
  onClose,
  onPublished,
}: BulkCampaignPublishDrawerProps) {
  const [selectedCampaignId, setSelectedCampaignId] = useState("");
  const [selectedChannelIds, setSelectedChannelIds] = useState<Set<string>>(new Set());
  const [statusFilter, setStatusFilter] = useState<CampaignPublishStatusFilter>("all");
  const [campaignFilter, setCampaignFilter] = useState<CampaignPublishCampaignFilter>("all");
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<CampaignPublishSummary | null>(null);
  const [isPublishing, setIsPublishing] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }

    setSelectedCampaignId(campaigns[0]?.id ?? "");
    setSelectedChannelIds(new Set());
    setStatusFilter("all");
    setCampaignFilter("all");
    setError(null);
    setSummary(null);
  }, [mode, open]);

  useEffect(() => {
    if (!open || selectedCampaignId || campaigns.length === 0) {
      return;
    }
    setSelectedCampaignId(campaigns[0].id);
  }, [campaigns, open, selectedCampaignId]);

  const selectedCampaign = useMemo(
    () => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null,
    [campaigns, selectedCampaignId],
  );

  const allTargets = useMemo(() => branchGroups.flatMap((branchGroup) => branchGroup.targets), [branchGroups]);
  const targetsByChannelId = useMemo(() => new Map(allTargets.map((target) => [target.channelId, target])), [allTargets]);
  const availableSlotIndexes = useMemo(
    () => Array.from(new Set(allTargets.map((target) => target.slotIndex))).sort((left, right) => left - right),
    [allTargets],
  );

  const visibleBranchGroups = useMemo(
    () =>
      branchGroups
        .map((branchGroup) => ({
          ...branchGroup,
          targets: branchGroup.targets.filter((target) => {
            if (statusFilter === "online" && target.status !== "online") {
              return false;
            }
            if (statusFilter === "offline" && target.status !== "offline") {
              return false;
            }
            if (campaignFilter === "without" && target.hasCampaign) {
              return false;
            }
            if (campaignFilter === "with" && !target.hasCampaign) {
              return false;
            }
            return true;
          }),
        }))
        .filter((branchGroup) => branchGroup.targets.length > 0),
    [branchGroups, campaignFilter, statusFilter],
  );

  const visibleTargets = useMemo(() => visibleBranchGroups.flatMap((branchGroup) => branchGroup.targets), [visibleBranchGroups]);
  const selectedTargets = useMemo(
    () =>
      Array.from(selectedChannelIds)
        .map((channelId) => targetsByChannelId.get(channelId) ?? null)
        .filter((target): target is NonNullable<typeof target> => Boolean(target)),
    [selectedChannelIds, targetsByChannelId],
  );
  const selectedCount = selectedTargets.length;
  const selectedBranchCount = useMemo(() => new Set(selectedTargets.map((target) => target.branchId)).size, [selectedTargets]);
  const includedPositions = useMemo(
    () => Array.from(new Set(selectedTargets.map((target) => target.slotIndex))).sort((left, right) => left - right),
    [selectedTargets],
  );
  const fullySelectedSlotIndexes = useMemo(
    () =>
      availableSlotIndexes.filter((slotIndex) => {
        const slotTargets = visibleTargets.filter((target) => target.slotIndex === slotIndex);
        return slotTargets.length > 0 && slotTargets.every((target) => selectedChannelIds.has(target.channelId));
      }),
    [availableSlotIndexes, selectedChannelIds, visibleTargets],
  );

  const scopeTitle =
    mode === "branch"
      ? `Publicar campaña en ${branchGroups[0]?.branchName ?? "sucursal"}`
      : "Publicar campaña global";
  const scopeSubtitle =
    mode === "branch"
      ? "Selecciona una o varias pantallas de esta sucursal y publica la campaña elegida sin salir del dashboard."
      : "Selecciona sucursales y pantallas de todo el cliente para publicar la campaña de forma masiva.";

  function replaceSelection(channelIds: string[]) {
    setSelectedChannelIds(new Set(channelIds));
  }

  function toggleChannel(channelId: string) {
    setSelectedChannelIds((current) => {
      const next = new Set(current);
      if (next.has(channelId)) {
        next.delete(channelId);
      } else {
        next.add(channelId);
      }
      return next;
    });
  }

  function toggleBranch(branchId: string) {
    const branchTargets = visibleBranchGroups.find((branchGroup) => branchGroup.branchId === branchId)?.targets ?? [];
    const branchChannelIds = branchTargets.map((target) => target.channelId);
    const allSelected = branchChannelIds.length > 0 && branchChannelIds.every((channelId) => selectedChannelIds.has(channelId));

    setSelectedChannelIds((current) => {
      const next = new Set(current);
      if (allSelected) {
        branchChannelIds.forEach((channelId) => next.delete(channelId));
      } else {
        branchChannelIds.forEach((channelId) => next.add(channelId));
      }
      return next;
    });
  }

  function toggleSlotSelection(slotIndex: number) {
    const slotChannelIds = visibleBranchGroups
      .map((branchGroup) => branchGroup.targets.find((target) => target.slotIndex === slotIndex)?.channelId ?? null)
      .filter((channelId): channelId is string => Boolean(channelId));

    if (slotChannelIds.length === 0) {
      return;
    }

    const slotFullySelected = slotChannelIds.every((channelId) => selectedChannelIds.has(channelId));

    setSelectedChannelIds((current) => {
      const next = new Set(current);
      if (slotFullySelected) {
        slotChannelIds.forEach((channelId) => next.delete(channelId));
      } else {
        slotChannelIds.forEach((channelId) => next.add(channelId));
      }
      return next;
    });
  }

  async function handlePublish() {
    if (!token || !selectedCampaignId || selectedCount === 0) {
      return;
    }

    setIsPublishing(true);
    setError(null);
    setSummary(null);

    const results: CampaignPublishSummary = {
      total: selectedTargets.length,
      published: 0,
      existing: 0,
      errors: [],
    };

    try {
      await runWithConcurrencyLimit(selectedTargets, 5, async (target) => {
        try {
          const result = await apiRequest<CampaignAssignmentResult>(`/campaigns/${selectedCampaignId}/assignments`, {
            method: "POST",
            token,
            body: {
              channel_id: target.channelId,
              priority: 1,
            },
          });

          if (result.assignment_status === "created") {
            results.published += 1;
          } else {
            results.existing += 1;
          }
        } catch (nextError) {
          results.errors.push({
            targetLabel: `${target.branchName} · ${target.visualLabel}`,
            message: getApiErrorMessage(nextError, "No se pudo publicar la campaña en esta pantalla."),
          });
        }
      });

      const finalSummary = { ...results, errors: [...results.errors] };
      setSummary(finalSummary);
      await onPublished(finalSummary, selectedCampaign);
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo completar la publicación masiva."));
    } finally {
      setIsPublishing(false);
    }
  }

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/45 backdrop-blur-sm">
      <button aria-label="Cerrar" className="flex-1 cursor-default" type="button" onClick={onClose} />

      <aside className="flex h-screen w-[min(95vw,1100px)] max-w-none flex-col overflow-x-hidden overflow-y-auto border-l border-white/10 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-6 py-5">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Dashboard V2.3.1</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{scopeTitle}</h3>
            <p className="mt-2 max-w-3xl text-sm text-slate-600">{scopeSubtitle}</p>
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

        <div className="grid gap-5 px-6 py-5 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="flex min-w-0 flex-col gap-4">
            {error ? <div className="rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}

            <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Campana a publicar</p>
                  <p className="mt-1 text-sm text-slate-600">
                    Se usara el endpoint idempotente actual por cada channel_id seleccionado, con un maximo de 5 requests paralelos.
                  </p>
                </div>
                <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                  {selectedCount} seleccionadas
                </span>
              </div>

              <div className="mt-3">
                <select value={selectedCampaignId} onChange={(event) => setSelectedCampaignId(event.target.value)}>
                  {campaigns.length === 0 ? <option value="">No hay campañas disponibles</option> : null}
                  {campaigns.map((campaign) => (
                    <option key={campaign.id} value={campaign.id}>
                      {campaign.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <CampaignPublishQuickActions
              visibleTargetCount={visibleTargets.length}
              selectedCount={selectedCount}
              slotIndexes={availableSlotIndexes}
              selectedSlotIndexes={fullySelectedSlotIndexes}
              statusFilter={statusFilter}
              campaignFilter={campaignFilter}
              onSelectAllVisible={() => replaceSelection(visibleTargets.map((target) => target.channelId))}
              onSelectOnlineVisible={() =>
                replaceSelection(visibleTargets.filter((target) => target.status === "online").map((target) => target.channelId))
              }
              onSelectWithoutCampaignVisible={() =>
                replaceSelection(visibleTargets.filter((target) => !target.hasCampaign).map((target) => target.channelId))
              }
              onClearSelection={() => replaceSelection([])}
              onToggleSlotSelection={toggleSlotSelection}
              onSetStatusFilter={setStatusFilter}
              onSetCampaignFilter={setCampaignFilter}
            />

            <div className="min-w-0">
              <CampaignPublishTargetTree
                branchGroups={visibleBranchGroups}
                selectedChannelIds={selectedChannelIds}
                onToggleBranch={toggleBranch}
                onToggleChannel={toggleChannel}
              />
            </div>
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex items-start gap-3">
                <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                  <ArrowRightLeft className="h-5 w-5" />
                </span>
                <div className="space-y-3">
                  <div>
                    <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Resumen de seleccion</p>
                    <p className="mt-2 text-sm font-semibold text-ink">{selectedBranchCount} sucursal(es) afectada(s)</p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Pantallas seleccionadas</p>
                      <p className="mt-1 text-lg font-semibold text-ink">{selectedCount}</p>
                    </div>
                    <div>
                      <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Posiciones incluidas</p>
                      <p className="mt-1 text-sm font-semibold text-ink">
                        {includedPositions.length > 0 ? includedPositions.map((slotIndex) => `P${slotIndex}`).join(", ") : "Ninguna"}
                      </p>
                    </div>
                  </div>
                  <div className="text-xs text-slate-500">
                    Vista actual: {visibleBranchGroups.length} sucursal(es) y {visibleTargets.length} pantalla(s) visibles.
                  </div>
                </div>
              </div>
            </div>

            <CampaignPublishResultSummary summary={summary} />

            <div className="mt-auto rounded-[24px] border border-slate-200 bg-white px-4 py-4">
              <div className="space-y-2 text-sm text-slate-600">
                <p>
                  <strong className="text-ink">Campana:</strong> {selectedCampaign?.name ?? "Sin seleccion"}
                </p>
                <p>
                  <strong className="text-ink">Pantallas seleccionadas:</strong> {selectedCount}
                </p>
              </div>

              <div className="mt-4 flex flex-wrap gap-3">
                <button
                  className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                  disabled={!selectedCampaignId || selectedCount === 0 || isPublishing}
                  type="button"
                  onClick={() => {
                    void handlePublish();
                  }}
                >
                  {isPublishing ? "Publicando..." : `Publicar en ${selectedCount} pantalla(s)`}
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
          </div>
        </div>
      </aside>
    </div>
  );
}
