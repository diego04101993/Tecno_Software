import { ArrowRightLeft, CalendarClock, Layers3, RadioTower, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { Campaign, CampaignAssignmentResult } from "../../types/domain";
import { getApiErrorMessage } from "./apiError";
import {
  CampaignPublishQuickActions,
  type CampaignPublishCampaignFilter,
  type CampaignPublishStatusFilter,
} from "./CampaignPublishQuickActions";
import { CampaignPublishResultSummary, type CampaignPublishSummary } from "./CampaignPublishResultSummary";
import { CampaignPublishTargetTree, type PublishTargetBranchGroup, type PublishableChannelTarget } from "./CampaignPublishTargetTree";

type ScheduledDrawerScope = "screen" | "branch" | "global" | "videowall";
type PublishMode = "now" | "scheduled";
type ImmediateStrategy = "append" | "replace";

export type ScheduledDrawerContextCard = {
  label: string;
  title: string;
  helper?: string;
};

const WEEKDAY_OPTIONS = [
  { value: 1, label: "Lun" },
  { value: 2, label: "Mar" },
  { value: 3, label: "Mie" },
  { value: 4, label: "Jue" },
  { value: 5, label: "Vie" },
  { value: 6, label: "Sab" },
  { value: 7, label: "Dom" },
];

function chipClassName(active = false) {
  return [
    "rounded-full border px-3 py-2 text-xs font-semibold uppercase tracking-[0.14em] transition",
    active
      ? "border-slate-900 bg-slate-900 text-white"
      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:text-ink",
  ].join(" ");
}

function toggleWeekday(current: number[], day: number) {
  const next = new Set(current);
  if (next.has(day)) {
    next.delete(day);
  } else {
    next.add(day);
  }
  return Array.from(next).sort((left, right) => left - right);
}

function deriveRecurrence({
  startsOn,
  endsOn,
  weekdays,
}: {
  startsOn: string;
  endsOn: string;
  weekdays: number[];
}) {
  if (weekdays.length === WEEKDAY_OPTIONS.length) {
    return "daily";
  }
  if (weekdays.length > 0) {
    return "weekly";
  }
  if (startsOn && endsOn && startsOn === endsOn) {
    return "once";
  }
  return "daily";
}

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

export function ScheduledCampaignDrawer({
  open,
  token,
  scope,
  title,
  subtitle,
  campaigns,
  targetGroups = [],
  fixedTargets = [],
  selectionMode = "free",
  immediateStrategy = "append",
  currentCampaignId = null,
  currentCampaignLabel = "Sin campana visible",
  contextCards = [],
  resolutionHint = null,
  onClose,
  onCompleted,
}: {
  open: boolean;
  token: string | null;
  scope: ScheduledDrawerScope;
  title: string;
  subtitle: string;
  campaigns: Campaign[];
  targetGroups?: PublishTargetBranchGroup[];
  fixedTargets?: PublishableChannelTarget[];
  selectionMode?: "free" | "fixed";
  immediateStrategy?: ImmediateStrategy;
  currentCampaignId?: string | null;
  currentCampaignLabel?: string;
  contextCards?: ScheduledDrawerContextCard[];
  resolutionHint?: { label: string; width: number; height: number } | null;
  onClose: () => void;
  onCompleted: (summary: CampaignPublishSummary, campaign: Campaign | null, publishMode: PublishMode) => Promise<void> | void;
}) {
  const [selectedCampaignId, setSelectedCampaignId] = useState("");
  const [selectedChannelIds, setSelectedChannelIds] = useState<Set<string>>(new Set());
  const [publishMode, setPublishMode] = useState<PublishMode>("now");
  const [statusFilter, setStatusFilter] = useState<CampaignPublishStatusFilter>("all");
  const [campaignFilter, setCampaignFilter] = useState<CampaignPublishCampaignFilter>("all");
  const [scheduleTitle, setScheduleTitle] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [endsOn, setEndsOn] = useState("");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [weekdays, setWeekdays] = useState<number[]>([]);
  const [priority, setPriority] = useState(100);
  const [isActive, setIsActive] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<CampaignPublishSummary | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const wasOpenRef = useRef(false);

  const allTargets = useMemo(
    () => (selectionMode === "fixed" ? fixedTargets : targetGroups.flatMap((branchGroup) => branchGroup.targets)),
    [fixedTargets, selectionMode, targetGroups],
  );
  const targetsByChannelId = useMemo(() => new Map(allTargets.map((target) => [target.channelId, target])), [allTargets]);
  const availableSlotIndexes = useMemo(
    () => Array.from(new Set(allTargets.map((target) => target.slotIndex))).sort((left, right) => left - right),
    [allTargets],
  );

  useEffect(() => {
    const isOpening = open && !wasOpenRef.current;
    wasOpenRef.current = open;

    if (!isOpening) {
      return;
    }

    const initialCampaignId = currentCampaignId ?? campaigns[0]?.id ?? "";
    setSelectedCampaignId(initialCampaignId);
    setSelectedChannelIds(new Set(selectionMode === "fixed" ? fixedTargets.map((target) => target.channelId) : []));
    setPublishMode("now");
    setStatusFilter("all");
    setCampaignFilter("all");
    setScheduleTitle("");
    setStartsOn("");
    setEndsOn("");
    setStartTime("");
    setEndTime("");
    setWeekdays([]);
    setPriority(100);
    setIsActive(true);
    setError(null);
    setSummary(null);
  }, [campaigns, currentCampaignId, fixedTargets, open, selectionMode]);

  useEffect(() => {
    if (!open || campaigns.length === 0) {
      return;
    }
    const selectedCampaignStillExists = campaigns.some((campaign) => campaign.id === selectedCampaignId);
    if (selectedCampaignStillExists) {
      return;
    }
    setSelectedCampaignId(currentCampaignId ?? campaigns[0].id);
  }, [campaigns, currentCampaignId, open, selectedCampaignId]);

  useEffect(() => {
    if (!open) {
      return;
    }
    if (!scheduleTitle.trim()) {
      const selectedCampaign = campaigns.find((campaign) => campaign.id === selectedCampaignId);
      if (selectedCampaign) {
        setScheduleTitle(`${selectedCampaign.name} programada`);
      }
    }
  }, [campaigns, open, scheduleTitle, selectedCampaignId]);

  const selectedCampaign = useMemo(
    () => campaigns.find((campaign) => campaign.id === selectedCampaignId) ?? null,
    [campaigns, selectedCampaignId],
  );
  const allWeekdaysSelected = weekdays.length === WEEKDAY_OPTIONS.length;

  const visibleBranchGroups = useMemo(() => {
    if (selectionMode === "fixed") {
      return [];
    }

    return targetGroups
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
      .filter((branchGroup) => branchGroup.targets.length > 0);
  }, [campaignFilter, selectionMode, statusFilter, targetGroups]);

  const visibleTargets = useMemo(
    () => (selectionMode === "fixed" ? fixedTargets : visibleBranchGroups.flatMap((branchGroup) => branchGroup.targets)),
    [fixedTargets, selectionMode, visibleBranchGroups],
  );

  const selectedTargets = useMemo(
    () =>
      Array.from(selectedChannelIds)
        .map((channelId) => targetsByChannelId.get(channelId) ?? null)
        .filter((target): target is NonNullable<typeof target> => Boolean(target)),
    [selectedChannelIds, targetsByChannelId],
  );
  const effectiveSelectedTargets = selectionMode === "fixed" ? fixedTargets : selectedTargets;
  const selectedCount = effectiveSelectedTargets.length;
  const selectedBranchCount = useMemo(
    () => new Set(effectiveSelectedTargets.map((target) => target.branchId)).size,
    [effectiveSelectedTargets],
  );
  const includedPositions = useMemo(
    () => Array.from(new Set(effectiveSelectedTargets.map((target) => target.slotIndex))).sort((left, right) => left - right),
    [effectiveSelectedTargets],
  );
  const fullySelectedSlotIndexes = useMemo(
    () =>
      availableSlotIndexes.filter((slotIndex) => {
        const slotTargets = visibleTargets.filter((target) => target.slotIndex === slotIndex);
        return slotTargets.length > 0 && slotTargets.every((target) => selectedChannelIds.has(target.channelId));
      }),
    [availableSlotIndexes, selectedChannelIds, visibleTargets],
  );

  function replaceSelection(channelIds: string[]) {
    if (selectionMode === "fixed") {
      return;
    }
    setSelectedChannelIds(new Set(channelIds));
  }

  function toggleChannel(channelId: string) {
    if (selectionMode === "fixed") {
      return;
    }
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
    if (selectionMode === "fixed") {
      return;
    }
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
    if (selectionMode === "fixed") {
      return;
    }

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

  async function handleImmediatePublish() {
    if (!token || !selectedCampaignId || selectedCount === 0) {
      return;
    }

    const results: CampaignPublishSummary = {
      total: effectiveSelectedTargets.length,
      published: 0,
      existing: 0,
      errors: [],
    };

    await runWithConcurrencyLimit(effectiveSelectedTargets, 5, async (target) => {
      try {
        const result =
          immediateStrategy === "replace"
            ? await apiRequest<CampaignAssignmentResult>(`/channels/${target.channelId}/campaign-assignment`, {
                method: "PUT",
                token,
                body: {
                  campaign_id: selectedCampaignId,
                  priority: 1,
                },
              })
            : await apiRequest<CampaignAssignmentResult>(`/campaigns/${selectedCampaignId}/assignments`, {
                method: "POST",
                token,
                body: {
                  channel_id: target.channelId,
                  priority: 1,
                },
              });

        if (result.assignment_status === "existing") {
          results.existing += 1;
        } else {
          results.published += 1;
        }
      } catch (nextError) {
        results.errors.push({
          targetLabel: `${target.branchName} · ${target.visualLabel}`,
          message: getApiErrorMessage(nextError, "No se pudo publicar la campana en esta pantalla."),
        });
      }
    });

    setSummary(results);
    await onCompleted(results, selectedCampaign, "now");
  }

  async function handleScheduledPublish() {
    if (!token || !selectedCampaignId || selectedCount === 0) {
      return;
    }

    const normalizedWeekdays = weekdays.slice().sort((left, right) => left - right);
    const recurrence = deriveRecurrence({ startsOn, endsOn, weekdays: normalizedWeekdays });
    const results: CampaignPublishSummary = {
      total: effectiveSelectedTargets.length,
      published: 0,
      existing: 0,
      errors: [],
    };

    await runWithConcurrencyLimit(effectiveSelectedTargets, 5, async (target) => {
      try {
        await apiRequest("/schedules/", {
          method: "POST",
          token,
          body: {
            client_id: selectedCampaign?.client_id,
            campaign_id: selectedCampaignId,
            channel_id: target.channelId,
            branch_id: target.branchId,
            title: scheduleTitle.trim() || `${selectedCampaign?.name ?? "Campana"} programada`,
            recurrence,
            days_of_week: normalizedWeekdays,
            starts_on: startsOn || null,
            ends_on: endsOn || null,
            start_time: startTime || null,
            end_time: endTime || null,
            is_active: isActive,
            is_looping: true,
            timezone: target.branchTimezone || "America/Mexico_City",
            priority,
          },
        });
        results.published += 1;
      } catch (nextError) {
        results.errors.push({
          targetLabel: `${target.branchName} · ${target.visualLabel}`,
          message: getApiErrorMessage(nextError, "No se pudo guardar la programacion para esta pantalla."),
        });
      }
    });

    setSummary(results);
    await onCompleted(results, selectedCampaign, "scheduled");
  }

  async function handleSubmit() {
    if (!token) {
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setSummary(null);

    try {
      if (publishMode === "scheduled") {
        await handleScheduledPublish();
      } else {
        await handleImmediatePublish();
      }
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo completar la publicacion."));
    } finally {
      setIsSubmitting(false);
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
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Dashboard Scheduling</p>
            <h3 className="mt-2 font-display text-3xl text-ink">{title}</h3>
            <p className="mt-2 max-w-3xl text-sm text-slate-600">{subtitle}</p>
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
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Campana</p>
                  <p className="mt-1 text-sm text-slate-600">
                    {publishMode === "now"
                      ? "La publicacion inmediata mantiene el flujo actual del dashboard."
                      : "La programacion crea reglas por fecha, horario, dias y prioridad sin tocar Operacion."}
                  </p>
                </div>
                <span className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                  {selectedCount} destino(s)
                </span>
              </div>

              <div className="mt-3">
                <select value={selectedCampaignId} onChange={(event) => setSelectedCampaignId(event.target.value)}>
                  {campaigns.length === 0 ? <option value="">No hay campanas disponibles</option> : null}
                  {campaigns.map((campaign) => (
                    <option key={campaign.id} value={campaign.id}>
                      {campaign.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="mt-4 flex flex-wrap gap-2">
                <button className={chipClassName(publishMode === "now")} type="button" onClick={() => setPublishMode("now")}>
                  Publicar ahora
                </button>
                <button className={chipClassName(publishMode === "scheduled")} type="button" onClick={() => setPublishMode("scheduled")}>
                  Programar campana
                </button>
              </div>
            </div>

            {publishMode === "scheduled" ? (
              <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
                <div className="flex items-start gap-3">
                  <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                    <CalendarClock className="h-5 w-5" />
                  </span>
                  <div className="min-w-0">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Programacion</p>
                    <p className="mt-1 text-sm text-slate-600">
                      La prioridad mas alta gana cuando varias reglas coinciden. Si ninguna regla esta vigente, el runtime cae a la campana inmediata actual.
                    </p>
                  </div>
                </div>

                <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  <div className="xl:col-span-3">
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Titulo de programacion</label>
                    <input value={scheduleTitle} onChange={(event) => setScheduleTitle(event.target.value)} placeholder="Ej. Desayuno 08:00 a 12:00" />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Fecha inicio</label>
                    <input type="date" value={startsOn} onChange={(event) => setStartsOn(event.target.value)} />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Fecha fin</label>
                    <input type="date" value={endsOn} onChange={(event) => setEndsOn(event.target.value)} />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Prioridad</label>
                    <input
                      type="number"
                      min={1}
                      value={priority}
                      onChange={(event) => setPriority(Math.max(1, Number(event.target.value) || 1))}
                    />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Hora inicio</label>
                    <input type="time" value={startTime} onChange={(event) => setStartTime(event.target.value)} />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-semibold text-slate-700">Hora fin</label>
                    <input type="time" value={endTime} onChange={(event) => setEndTime(event.target.value)} />
                  </div>
                  <div className="flex items-center gap-3 rounded-[18px] border border-slate-200 bg-white px-4 py-3">
                    <input checked={isActive} className="h-4 w-4 rounded border-slate-300 text-slate-900 focus:ring-slate-400" type="checkbox" onChange={(event) => setIsActive(event.target.checked)} />
                    <div>
                      <p className="text-sm font-semibold text-ink">Programacion activa</p>
                      <p className="text-xs text-slate-500">Puedes crearla desactivada y activarla despues por API si hace falta.</p>
                    </div>
                  </div>
                </div>

                <div className="mt-4">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-semibold text-slate-700">Dias de la semana</p>
                    <button
                      className={chipClassName(allWeekdaysSelected)}
                      type="button"
                      onClick={() => setWeekdays(allWeekdaysSelected ? [] : WEEKDAY_OPTIONS.map((day) => day.value))}
                    >
                      Todos los dias
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {WEEKDAY_OPTIONS.map((day) => (
                      <button
                        key={day.value}
                        className={chipClassName(weekdays.includes(day.value))}
                        type="button"
                        onClick={() => setWeekdays((current) => toggleWeekday(current, day.value))}
                      >
                        {day.label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            ) : null}

            {selectionMode === "free" ? (
              <>
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
              </>
            ) : (
              <div className="space-y-4">
                {contextCards.length > 0 ? (
                  <div className="grid gap-4 xl:grid-cols-3">
                    {contextCards.map((card) => (
                      <div key={`${card.label}:${card.title}`} className="rounded-[24px] border border-slate-200 bg-slate-50 p-4">
                        <p className="text-xs uppercase tracking-[0.18em] text-slate-500">{card.label}</p>
                        <p className="mt-2 font-semibold text-ink">{card.title}</p>
                        {card.helper ? <p className="mt-1 text-sm text-slate-500">{card.helper}</p> : null}
                      </div>
                    ))}
                  </div>
                ) : null}

                <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                  <div className="flex items-start gap-3">
                    <span className="rounded-2xl bg-slate-100 p-3 text-slate-700">
                      {scope === "videowall" ? <Layers3 className="h-5 w-5" /> : <RadioTower className="h-5 w-5" />}
                    </span>
                    <div className="min-w-0">
                      <p className="text-xs uppercase tracking-[0.2em] text-slate-500">
                        {scope === "videowall" ? "Monitores destino" : "Pantalla destino"}
                      </p>
                      <p className="mt-1 text-sm text-slate-600">
                        {scope === "videowall"
                          ? "Todos los nodos configurados del videowall recibiran la misma campana."
                          : "La publicacion inmediata reemplaza la campana visible actual solo en esta pantalla."}
                      </p>
                    </div>
                  </div>

                  <div className="mt-4 space-y-3">
                    {fixedTargets.map((target) => (
                      <div key={target.channelId} className="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <p className="font-semibold text-ink">{target.visualLabel}</p>
                          <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                            {target.channelCode}
                          </span>
                        </div>
                        <p className="mt-1 text-sm text-slate-700">{target.channelName}</p>
                        <p className="mt-1 text-xs text-slate-500">
                          {target.branchName} · {target.hasCampaign ? target.currentCampaignLabel : "Sin campana visible"}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4">
              <div className="flex items-start gap-3">
                <span className="rounded-2xl bg-white p-3 text-accent shadow-sm">
                  <ArrowRightLeft className="h-5 w-5" />
                </span>
                <div className="space-y-3">
                  <div>
                    <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Resumen</p>
                    <p className="mt-2 text-sm font-semibold text-ink">{selectedBranchCount} sucursal(es) afectada(s)</p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Pantallas</p>
                      <p className="mt-1 text-lg font-semibold text-ink">{selectedCount}</p>
                    </div>
                    <div>
                      <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Posiciones</p>
                      <p className="mt-1 text-sm font-semibold text-ink">
                        {includedPositions.length > 0 ? includedPositions.map((slotIndex) => `P${slotIndex}`).join(", ") : "Ninguna"}
                      </p>
                    </div>
                  </div>
                  <div className="text-xs text-slate-500">
                    Campana visible actual: {currentCampaignLabel}
                  </div>
                </div>
              </div>
            </div>

            {resolutionHint ? (
              <div className="rounded-[24px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-900">
                <p className="text-xs uppercase tracking-[0.18em] text-amber-700">Resolucion sugerida</p>
                <p className="mt-2 font-semibold">
                  {resolutionHint.label}: {resolutionHint.width}x{resolutionHint.height}
                </p>
                <p className="mt-1 text-xs text-amber-800">
                  Idealmente usa una campana o layout pensado para esta resolucion total antes de recortar por nodo.
                </p>
              </div>
            ) : null}

            <CampaignPublishResultSummary summary={summary} />

            <div className="mt-auto rounded-[24px] border border-slate-200 bg-white px-4 py-4">
              <div className="space-y-2 text-sm text-slate-600">
                <p>
                  <strong className="text-ink">Modo:</strong> {publishMode === "now" ? "Publicar ahora" : "Programar campana"}
                </p>
                <p>
                  <strong className="text-ink">Campana:</strong> {selectedCampaign?.name ?? "Sin seleccion"}
                </p>
                <p>
                  <strong className="text-ink">Pantallas:</strong> {selectedCount}
                </p>
              </div>

              <div className="mt-4 flex flex-wrap gap-3">
                <button
                  className="rounded-[20px] bg-ink px-5 py-4 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60"
                  disabled={!selectedCampaignId || selectedCount === 0 || isSubmitting}
                  type="button"
                  onClick={() => {
                    void handleSubmit();
                  }}
                >
                  {isSubmitting
                    ? publishMode === "scheduled"
                      ? "Guardando..."
                      : "Publicando..."
                    : publishMode === "scheduled"
                      ? `Programar en ${selectedCount} pantalla(s)`
                      : `Publicar en ${selectedCount} pantalla(s)`}
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
