import { Library, Monitor, PanelsTopLeft, Presentation, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { TimelineBoard } from "../../components/TimelineBoard";
import { WorkspaceQuickLinks } from "../../components/WorkspaceQuickLinks";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { canAccessGlobalClients, isClientAdminLike } from "../../lib/rbac";
import {
  buildBranchLayoutsPath,
  buildBranchOverviewPath,
  buildBranchPreviewPath,
  buildClientCampaignsPath,
  buildClientContentsPath,
} from "../../lib/workspace";
import type { Campaign, Channel, ScheduleItem } from "../../types/domain";

function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

function isScheduleActiveNow(schedule: ScheduleItem) {
  const now = new Date();
  const day = now.getDay() === 0 ? 7 : now.getDay();
  if (schedule.days_of_week.length > 0 && !schedule.days_of_week.includes(day)) {
    return false;
  }

  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const startMinutes = schedule.start_time ? minutesFromTime(schedule.start_time) : 0;
  const endMinutes = schedule.end_time ? minutesFromTime(schedule.end_time) : 24 * 60;

  return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
}

function getChannelSchedules(channelId: string, branchId: string, schedules: ScheduleItem[]) {
  return schedules.filter((item) => item.channel_id === channelId || (!item.channel_id && item.branch_id === branchId));
}

export function BranchTimelinePage() {
  const { token, user } = useAuth();
  const { clientId, branchId } = useParams();
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [selectedCampaignId, setSelectedCampaignId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !clientId || !branchId) {
      return;
    }

    Promise.all([
      apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
      apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }),
      apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}`, { token }),
    ])
      .then(([campaignsResponse, channelsResponse, schedulesResponse]) => {
        const channelIds = new Set(channelsResponse.map((item) => item.id));
        const scopedSchedules = schedulesResponse.filter(
          (item) => item.branch_id === branchId || (item.channel_id ? channelIds.has(item.channel_id) : false),
        );
        setCampaigns(campaignsResponse);
        setChannels(channelsResponse);
        setSchedules(scopedSchedules);
        setSelectedCampaignId((current) => current ?? campaignsResponse[0]?.id ?? null);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el timeline de la sucursal");
      });
  }, [branchId, clientId, token]);

  const visibleSchedules = useMemo(
    () => (selectedCampaignId ? schedules.filter((item) => item.campaign_id === selectedCampaignId) : schedules),
    [schedules, selectedCampaignId],
  );

  const activeSchedules = useMemo(() => visibleSchedules.filter((item) => isScheduleActiveNow(item)), [visibleSchedules]);
  const channelCoverage = useMemo(
    () =>
      channels.map((channel) => {
        const channelSchedules = getChannelSchedules(channel.id, branchId ?? "", visibleSchedules);
        const activeChannelSchedules = channelSchedules.filter((item) => isScheduleActiveNow(item));
        const prioritized = [...activeChannelSchedules, ...channelSchedules].sort((left, right) => right.priority - left.priority)[0] ?? null;
        const assignedCampaign = campaigns.find((item) => item.id === prioritized?.campaign_id) ?? null;

        return {
          channel,
          activeCount: activeChannelSchedules.length,
          totalCount: channelSchedules.length,
          assignedCampaignLabel: assignedCampaign?.name ?? "Sin campaña visible",
        };
      }),
    [branchId, campaigns, channels, visibleSchedules],
  );

  const channelsWithoutTimeline = channelCoverage.filter((item) => item.totalCount === 0).length;
  const canOpenClientRoutes = canAccessGlobalClients(user?.role) || isClientAdminLike(user?.role);

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Campañas visibles" value={String(campaigns.length)} hint="Catálogo del cliente" tone="teal" />
        <StatCard label="Canales cubiertos" value={String(channels.length)} hint={`${channelsWithoutTimeline} sin timeline`} tone="orange" />
        <StatCard label="Slots activos" value={String(activeSchedules.length)} hint="Programacion vigente ahora" />
        <StatCard label="Slots filtrados" value={String(visibleSchedules.length)} hint="Resultado del filtro actual" />
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.08fr_0.92fr]">
        <SectionCard title="Timeline de sucursal" subtitle="Vista operacional de slots activos por canal y campaña dentro de esta sucursal.">
          <div className="mb-6 flex flex-wrap gap-3">
            <button
              type="button"
              onClick={() => setSelectedCampaignId(null)}
              className={`rounded-full border px-4 py-2 text-sm font-semibold transition ${
                selectedCampaignId === null ? "border-accent bg-accent text-white" : "border-slate-200 bg-white text-slate-700"
              }`}
            >
              Todas las campañas
            </button>
            {campaigns.map((campaign) => (
              <button
                type="button"
                key={campaign.id}
                onClick={() => setSelectedCampaignId(campaign.id)}
                className={`rounded-full border px-4 py-2 text-sm font-semibold transition ${
                  selectedCampaignId === campaign.id ? "border-accent bg-accent text-white" : "border-slate-200 bg-white text-slate-700"
                }`}
              >
                {campaign.name}
              </button>
            ))}
          </div>
          <TimelineBoard schedules={visibleSchedules} campaigns={campaigns} selectedCampaignId={selectedCampaignId} />
        </SectionCard>

        <div className="space-y-5">
          <SectionCard title="Cobertura por canal" subtitle="Resumen ejecutivo de slots activos y campaña visible en cada canal.">
            <div className="space-y-3">
              {channelCoverage.map((item) => (
                <article key={item.channel.id} className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <p className="font-semibold text-ink">{item.channel.name}</p>
                      <p className="mt-1 truncate text-sm text-slate-500" title={item.assignedCampaignLabel}>
                        {item.assignedCampaignLabel}
                      </p>
                    </div>
                    <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
                      {item.activeCount} activo(s)
                    </span>
                  </div>
                  <p className="mt-3 text-sm text-slate-700">{item.totalCount} slot(s) visibles para este canal</p>
                </article>
              ))}
            </div>
          </SectionCard>

          {clientId && branchId ? (
            <WorkspaceQuickLinks
              title="Accesos rapidos"
              subtitle="Atajos para moverte entre campaña, contenido, layouts, overview y preview."
              links={[
                ...(canOpenClientRoutes
                  ? [
                      {
                        label: "Campañas",
                        caption: "Volver al catálogo de campañas del cliente.",
                        to: buildClientCampaignsPath(clientId),
                        icon: Presentation,
                      },
                      {
                        label: "Contenido",
                        caption: "Abrir la biblioteca del cliente.",
                        to: buildClientContentsPath(clientId),
                        icon: Library,
                      },
                    ]
                  : []),
                {
                  label: "Preview",
                  caption: "Abrir la simulación visual de la sucursal.",
                  to: buildBranchPreviewPath(clientId, branchId),
                  icon: Monitor,
                },
                {
                  label: "Layouts",
                  caption: "Consultar layouts disponibles en la sucursal.",
                  to: buildBranchLayoutsPath(clientId, branchId),
                  icon: PanelsTopLeft,
                },
                {
                  label: "Overview",
                  caption: "Regresar al centro de control de la sucursal.",
                  to: buildBranchOverviewPath(clientId, branchId),
                  icon: Route,
                },
              ]}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
