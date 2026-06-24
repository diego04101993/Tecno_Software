import { AlertTriangle, Library, PanelsTopLeft, Presentation, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ChannelOperationalCard } from "../../components/ChannelOperationalCard";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { WorkspaceQuickLinks } from "../../components/WorkspaceQuickLinks";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatChannelMode, formatChannelHeartbeatState } from "../../lib/labels";
import { canAccessGlobalClients, isClientAdminLike } from "../../lib/rbac";
import {
  buildBranchChannelsPath,
  buildBranchLayoutsPath,
  buildBranchTimelinePath,
  buildChannelDetailPath,
  buildClientCampaignsPath,
  buildClientContentsPath,
} from "../../lib/workspace";
import type { Branch, Campaign, Channel, Layout, ScheduleItem } from "../../types/domain";

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

export function BranchOverviewPage() {
  const { token, user } = useAuth();
  const { clientId, branchId } = useParams();
  const [branch, setBranch] = useState<Branch | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !clientId || !branchId) {
      return;
    }

    Promise.all([
      apiRequest<Branch[]>(`/branches?client_id=${clientId}`, { token }),
      apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }),
      apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
      apiRequest<Layout[]>(`/layouts?client_id=${clientId}`, { token }),
      apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}`, { token }),
    ])
      .then(([branchesResponse, channelsResponse, campaignsResponse, layoutsResponse, schedulesResponse]) => {
        const channelIds = new Set(channelsResponse.map((item) => item.id));
        setBranch(branchesResponse.find((item) => item.id === branchId) ?? null);
        setChannels(channelsResponse);
        setCampaigns(campaignsResponse);
        setLayouts(layoutsResponse);
        setSchedules(
          schedulesResponse.filter(
            (item) => item.branch_id === branchId || (item.channel_id ? channelIds.has(item.channel_id) : false),
          ),
        );
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el dashboard de la sucursal");
      });
  }, [branchId, clientId, token]);

  const onlineChannels = useMemo(() => channels.filter((channel) => channel.is_online), [channels]);
  const offlineChannels = useMemo(() => channels.filter((channel) => !channel.is_online), [channels]);
  const activeSchedules = useMemo(() => schedules.filter((item) => isScheduleActiveNow(item)), [schedules]);
  const channelsWithoutPlayback = useMemo(() => channels.filter((channel) => !channel.current_playback), [channels]);
  const channelsWithoutSchedules = useMemo(
    () => channels.filter((channel) => getChannelSchedules(channel.id, branchId ?? "", schedules).length === 0),
    [branchId, channels, schedules],
  );

  const channelSnapshots = useMemo(
    () =>
      channels.map((channel) => {
        const channelSchedules = getChannelSchedules(channel.id, branchId ?? "", schedules);
        const activeChannelSchedules = channelSchedules.filter((item) => isScheduleActiveNow(item));
        const selectedSchedule = [...activeChannelSchedules, ...channelSchedules].sort((left, right) => right.priority - left.priority)[0] ?? null;
        const assignedCampaign = campaigns.find((item) => item.id === selectedSchedule?.campaign_id) ?? null;
        const heartbeat = formatChannelHeartbeatState(channel);

        return {
          channel,
          assignedCampaignLabel: assignedCampaign?.name ?? "Sin campaña visible",
          heartbeat,
          activeTimelineCount: activeChannelSchedules.length,
        };
      }),
    [branchId, campaigns, channels, schedules],
  );

  const sortedSnapshots = useMemo(
    () =>
      [...channelSnapshots].sort((left, right) => {
        if (left.channel.is_online !== right.channel.is_online) {
          return left.channel.is_online ? 1 : -1;
        }
        if (left.heartbeat.stale !== right.heartbeat.stale) {
          return left.heartbeat.stale ? -1 : 1;
        }
        return left.channel.name.localeCompare(right.channel.name);
      }),
    [channelSnapshots],
  );

  const alerts = [
    offlineChannels.length > 0
      ? `${offlineChannels.length} canal(es) sin conexión`
      : null,
    channelsWithoutPlayback.length > 0
      ? `${channelsWithoutPlayback.length} canal(es) sin reproducción reportada`
      : null,
    channelsWithoutSchedules.length > 0
      ? `${channelsWithoutSchedules.length} canal(es) sin timeline asignado`
      : null,
  ].filter(Boolean) as string[];
  const canOpenClientRoutes = canAccessGlobalClients(user?.role) || isClientAdminLike(user?.role);

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 2xl:grid-cols-[1.16fr_0.84fr]">
        <article className="rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <p className="text-xs uppercase tracking-[0.28em] text-accent">Centro de control</p>
              <h2 className="mt-3 truncate font-display text-4xl text-ink" title={branch?.name ?? "Sucursal"}>
                {branch?.name ?? "Sucursal"}
              </h2>
              <p className="mt-3 text-sm text-slate-600">
                {branch?.code ?? "Sin código"} · {branch?.timezone ?? "Sin zona horaria"}
              </p>
              <p className="mt-2 text-sm text-slate-700">{branch?.address ?? "Dirección pendiente"}</p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-[24px] border border-emerald-200 bg-emerald-50 px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-emerald-700">Canales en línea</p>
                <p className="mt-2 font-display text-3xl text-ink">{onlineChannels.length}</p>
              </div>
              <div className="rounded-[24px] border border-rose-200 bg-rose-50 px-4 py-4">
                <p className="text-xs uppercase tracking-wide text-rose-700">Alertas operativas</p>
                <p className="mt-2 font-display text-3xl text-ink">{alerts.length}</p>
              </div>
            </div>
          </div>
        </article>

        <section className="grid gap-3 sm:grid-cols-2">
          <StatCard label="Canales" value={String(channels.length)} hint="Inventario visible" tone="teal" />
          <StatCard label="Offline" value={String(offlineChannels.length)} hint="Pendientes de revisar" tone="orange" />
          <StatCard label="Timeline activo" value={String(activeSchedules.length)} hint="Slots vigentes ahora" />
          <StatCard label="Layouts" value={String(layouts.length)} hint={`${campaigns.length} campañas disponibles`} />
        </section>
      </section>

      <div className="grid gap-5 2xl:grid-cols-[1.18fr_0.82fr]">
        <SectionCard
          title="Monitoreo de canales"
          subtitle="Lectura rápida de estado, playback, campaña visible y heartbeat por canal."
          action={
            clientId && branchId ? (
              <Link
                className="rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                to={buildBranchChannelsPath(clientId, branchId)}
              >
                Ver todos
              </Link>
            ) : null
          }
        >
          <div className="grid gap-4 2xl:grid-cols-2">
            {sortedSnapshots.map((snapshot) => (
              <ChannelOperationalCard
                key={snapshot.channel.id}
                channel={snapshot.channel}
                assignedCampaignLabel={snapshot.assignedCampaignLabel}
                activeTimelineCount={snapshot.activeTimelineCount}
                heartbeatLabel={snapshot.heartbeat.label}
                heartbeatTone={snapshot.heartbeat.tone}
                detailTo={clientId && branchId ? buildChannelDetailPath(clientId, branchId, snapshot.channel.id) : undefined}
              />
            ))}
          </div>
        </SectionCard>

        <div className="space-y-5">
          <SectionCard title="Alertas operativas" subtitle="Incidencias visibles que requieren seguimiento en la sucursal.">
            <div className="space-y-3">
              {alerts.length > 0 ? (
                alerts.map((alert) => (
                  <article key={alert} className="rounded-[24px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-900">
                    <div className="flex items-start gap-3">
                      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                      <div>
                        <p className="font-semibold">{alert}</p>
                        <p className="mt-1">Revisa el canal afectado, su heartbeat, su playback o su programación visible.</p>
                      </div>
                    </div>
                  </article>
                ))
              ) : (
                <article className="rounded-[24px] border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-800">
                  <p className="font-semibold">Sin alertas críticas</p>
                  <p className="mt-1">La sucursal reporta conectividad y reproducción sin bloqueos visibles desde esta vista.</p>
                </article>
              )}
              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                  <p className="text-xs uppercase tracking-wide text-slate-400">Modo dominante</p>
                  <p className="mt-2 font-semibold text-ink">
                    {channels[0] ? formatChannelMode(channels[0].mode) : "Sin canales"}
                  </p>
                </div>
                <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                  <p className="text-xs uppercase tracking-wide text-slate-400">Cobertura</p>
                  <p className="mt-2 font-semibold text-ink">{channelsWithoutSchedules.length} sin timeline</p>
                </div>
              </div>
            </div>
          </SectionCard>

          {clientId && branchId ? (
            <WorkspaceQuickLinks
              title="Accesos rápidos"
              subtitle="Atajos operativos para trabajar la sucursal sin salir del flujo actual."
              links={[
                ...(canOpenClientRoutes
                  ? [
                      {
                        label: "Campañas del cliente",
                        caption: "Revisar campañas visibles y asignaciones actuales.",
                        to: buildClientCampaignsPath(clientId),
                        icon: Presentation,
                      },
                      {
                        label: "Contenido",
                        caption: "Entrar a la biblioteca del cliente para validar assets.",
                        to: buildClientContentsPath(clientId),
                        icon: Library,
                      },
                    ]
                  : [
                      {
                        label: "Canales",
                        caption: "Abrir el inventario operativo de esta sucursal.",
                        to: buildBranchChannelsPath(clientId, branchId),
                        icon: Presentation,
                      },
                    ]),
                {
                  label: "Timeline",
                  caption: "Abrir la programación operativa de esta sucursal.",
                  to: buildBranchTimelinePath(clientId, branchId),
                  icon: Route,
                },
                {
                  label: "Layouts",
                  caption: "Consultar layouts disponibles para esta operación.",
                  to: buildBranchLayoutsPath(clientId, branchId),
                  icon: PanelsTopLeft,
                },
              ]}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
