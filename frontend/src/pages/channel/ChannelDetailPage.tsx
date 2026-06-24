import { AlertTriangle, Library, PanelsTopLeft, Presentation, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { PreviewPlayer } from "../../components/PreviewPlayer";
import { SectionCard } from "../../components/SectionCard";
import { WorkspaceQuickLinks } from "../../components/WorkspaceQuickLinks";
import { StatusBadge } from "../../components/StatusBadge";
import { TimelineBoard } from "../../components/TimelineBoard";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { formatChannelHeartbeatState, formatChannelMode, formatOrientation } from "../../lib/labels";
import {
  buildChannelPreviewBundle,
  getPreviewCampaignChoice,
  getSchedulesForChannel,
  type ChannelPreviewBundle,
} from "../../lib/preview";
import { canAccessGlobalClients, isClientAdminLike } from "../../lib/rbac";
import {
  buildBranchChannelsPath,
  buildBranchLayoutsPath,
  buildBranchPreviewPath,
  buildBranchTimelinePath,
  buildClientCampaignsPath,
  buildClientContentsPath,
} from "../../lib/workspace";
import type {
  Campaign,
  Channel,
  ContentItem,
  Layout,
  PlaylistItem,
  ScheduleItem,
  Videowall,
  VideowallNode,
} from "../../types/domain";

type PlayerConfig = {
  channel: Channel;
  campaigns: Array<{
    assignment_id: string;
    priority: number;
    campaign_id: string;
    name: string;
    active_from: string | null;
    active_until: string | null;
  }>;
  schedules: string[];
  videowall_segment: {
    videowall_id: string;
    position_index: number;
    row_index: number;
    column_index: number;
    x: number;
    y: number;
    width: number;
    height: number;
  } | null;
};

type VideowallPreviewResponse = {
  videowall: Videowall;
  nodes: VideowallNode[];
};

export function ChannelDetailPage() {
  const { token, user } = useAuth();
  const { clientId, branchId, channelId } = useParams();
  const [channel, setChannel] = useState<Channel | null>(null);
  const [playerConfig, setPlayerConfig] = useState<PlayerConfig | null>(null);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [playlist, setPlaylist] = useState<PlaylistItem[]>([]);
  const [videowallPreview, setVideowallPreview] = useState<VideowallPreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !clientId || !branchId || !channelId) {
      return;
    }

    Promise.all([
      apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }),
      apiRequest<PlayerConfig>(`/channels/${channelId}/player-config`, { token }),
      apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}&channel_id=${channelId}`, { token }),
      apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
      apiRequest<ContentItem[]>(`/contents?client_id=${clientId}`, { token }),
      apiRequest<Layout[]>(`/layouts?client_id=${clientId}`, { token }),
    ])
      .then(([channelsResponse, configResponse, schedulesResponse, campaignsResponse, contentsResponse, layoutsResponse]) => {
        setChannel(channelsResponse.find((item) => item.id === channelId) ?? configResponse.channel ?? null);
        setPlayerConfig(configResponse);
        setSchedules(schedulesResponse);
        setCampaigns(campaignsResponse);
        setContents(contentsResponse);
        setLayouts(layoutsResponse);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el detalle del canal");
      });
  }, [branchId, channelId, clientId, token]);

  const previewCampaign = useMemo(() => {
    if (!channel || !branchId) {
      return null;
    }

    return getPreviewCampaignChoice({
      schedules: getSchedulesForChannel(channel.id, branchId, schedules),
      campaigns,
      assignedCampaignIds: playerConfig?.campaigns.map((item) => item.campaign_id) ?? [],
    });
  }, [branchId, campaigns, channel, playerConfig?.campaigns, schedules]);

  useEffect(() => {
    if (!token || !previewCampaign?.campaign?.id) {
      setPlaylist([]);
      return;
    }

    apiRequest<PlaylistItem[]>(`/campaigns/${previewCampaign.campaign.id}/playlist`, { token })
      .then((response) => setPlaylist(response))
      .catch((nextError) => {
        setPlaylist([]);
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la playlist del canal");
      });
  }, [previewCampaign?.campaign?.id, token]);

  useEffect(() => {
    const videowallId = playerConfig?.videowall_segment?.videowall_id;
    if (!token || !videowallId) {
      setVideowallPreview(null);
      return;
    }

    apiRequest<VideowallPreviewResponse>(`/videowalls/${videowallId}/preview`, { token })
      .then((response) => setVideowallPreview(response))
      .catch((nextError) => {
        setVideowallPreview(null);
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el preview del videowall");
      });
  }, [playerConfig?.videowall_segment?.videowall_id, token]);

  const heartbeat = useMemo(
    () =>
      channel
        ? formatChannelHeartbeatState(channel)
        : {
            label: "Sin heartbeat",
            tone: "rose" as const,
            stale: true,
          },
    [channel],
  );
  const scopedSchedules = useMemo(() => {
    if (!channel || !branchId) {
      return [];
    }

    return getSchedulesForChannel(channel.id, branchId, schedules);
  }, [branchId, channel, schedules]);

  const previewBundle: ChannelPreviewBundle | null = useMemo(() => {
    if (!channel || !branchId) {
      return null;
    }

    const videowallSegment = playerConfig?.videowall_segment
      ? {
          id: `preview-${channel.id}`,
          created_at: "",
          updated_at: null,
          videowall_id: playerConfig.videowall_segment.videowall_id,
          channel_id: channel.id,
          position_index: playerConfig.videowall_segment.position_index,
          row_index: playerConfig.videowall_segment.row_index,
          column_index: playerConfig.videowall_segment.column_index,
          x: playerConfig.videowall_segment.x,
          y: playerConfig.videowall_segment.y,
          width: playerConfig.videowall_segment.width,
          height: playerConfig.videowall_segment.height,
        }
      : null;

    return buildChannelPreviewBundle({
      channel,
      branchId,
      schedules,
      campaigns,
      layouts,
      contents,
      playlist,
      assignedCampaignIds: playerConfig?.campaigns.map((item) => item.campaign_id) ?? [],
      videowall: videowallSegment
        ? {
            wall: videowallPreview?.videowall ?? null,
            nodes: videowallPreview?.nodes ?? [],
            segment: videowallSegment,
          }
        : null,
    });
  }, [
    branchId,
    campaigns,
    channel,
    contents,
    layouts,
    playerConfig?.campaigns,
    playerConfig?.videowall_segment,
    playlist,
    schedules,
    videowallPreview,
  ]);

  const alerts = [
    heartbeat.stale ? heartbeat.label : null,
    channel && !channel.current_playback ? "Sin reproduccion reportada" : null,
    schedules.length === 0 ? "Sin timeline asignado" : null,
  ].filter(Boolean) as string[];
  const canOpenClientRoutes = canAccessGlobalClients(user?.role) || isClientAdminLike(user?.role);

  async function copyChannelCode() {
    if (!channel) {
      return;
    }

    try {
      await navigator.clipboard.writeText(channel.channel_code);
    } catch {
      // Ignore clipboard failures in unsupported contexts.
    }
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-[1.08fr_0.92fr]">
        <article className="rounded-[32px] border border-white/70 bg-card/90 p-5 shadow-card backdrop-blur">
          {channel ? (
            <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-[0.28em] text-accent">Detalle de canal</p>
                <h2 className="mt-3 truncate font-display text-4xl text-ink" title={channel.name}>
                  {channel.name}
                </h2>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold tracking-[0.18em] text-slate-600">
                    {channel.channel_code}
                  </span>
                  <button
                    className="rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 transition hover:border-cyan-300 hover:text-ink"
                    type="button"
                    onClick={copyChannelCode}
                  >
                    Copiar código
                  </button>
                </div>
                <p className="mt-3 text-sm text-slate-600">
                  {formatChannelMode(channel.mode)} - {formatOrientation(channel.orientation)} - {channel.screen_count} salida(s)
                </p>
                <p className="mt-2 text-sm text-slate-700">
                  {channel.resolution_width}x{channel.resolution_height} - Hardware {channel.hardware_identifier ?? "pendiente"}
                </p>
              </div>
              <div className="flex flex-col items-start gap-3">
                <StatusBadge status={channel.is_online ? "online" : "offline"} />
                <Link
                  className="inline-flex rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
                  to={buildBranchChannelsPath(clientId ?? "", branchId ?? "")}
                >
                  Volver a canales
                </Link>
              </div>
            </div>
          ) : null}
        </article>

        <section className="grid gap-3 sm:grid-cols-2">
          <div
            className={`rounded-[28px] border px-5 py-5 shadow-card ${
              heartbeat.tone === "emerald"
                ? "border-emerald-200 bg-emerald-50"
                : "border-rose-200 bg-rose-50"
            }`}
          >
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Heartbeat</p>
            <p className="mt-3 font-display text-3xl text-ink">{heartbeat.label}</p>
          </div>
          <div className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Playback</p>
            <p className="mt-3 text-lg font-semibold text-ink">{channel?.current_playback ?? "Sin reproduccion reportada"}</p>
          </div>
          <div className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Timeline activo</p>
            <p className="mt-3 font-display text-3xl text-ink">{previewBundle?.activeSchedules.length ?? 0}</p>
          </div>
          <div className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Asignaciones</p>
            <p className="mt-3 font-display text-3xl text-ink">{playerConfig?.campaigns.length ?? 0}</p>
          </div>
        </section>
      </section>

      <PreviewPlayer
        bundle={previewBundle}
        title={channel ? `Preview de ${channel.name}` : "Preview del canal"}
        subtitle="Simulación de solo lectura con campaña asignada, playlist, timeline, layout y videowall del canal."
      />

      <div className="grid gap-5 xl:grid-cols-[1.05fr_0.95fr]">
        <SectionCard title="Asignación actual" subtitle="Campañas visibles, segmento de videowall y lectura operativa del canal.">
          <div className="space-y-4">
            {(playerConfig?.campaigns ?? []).length > 0 ? (
              (playerConfig?.campaigns ?? []).map((campaign) => (
                <article key={campaign.assignment_id} className="rounded-[24px] border border-slate-200 bg-white p-4">
                  <p className="font-semibold text-ink">{campaign.name}</p>
                  <p className="mt-1 text-sm text-slate-500">Prioridad {campaign.priority}</p>
                </article>
              ))
            ) : (
              <article className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                No hay campañas asignadas desde el player-config actual.
              </article>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                Schedules ligados: {scopedSchedules.length}
              </div>
              <div className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-700">
                {playerConfig?.videowall_segment
                  ? `Segmento ${playerConfig.videowall_segment.position_index} (${playerConfig.videowall_segment.width}x${playerConfig.videowall_segment.height})`
                  : "Sin segmento de videowall asignado"}
              </div>
            </div>
          </div>
        </SectionCard>

        <div className="space-y-5">
          <SectionCard title="Alertas operativas" subtitle="Incidencias visibles en este canal antes de escalar soporte.">
            <div className="space-y-3">
              {alerts.length > 0 ? (
                alerts.map((alert) => (
                  <article key={alert} className="rounded-[24px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-900">
                    <div className="flex items-start gap-3">
                      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                      <div>
                        <p className="font-semibold">{alert}</p>
                        <p className="mt-1">Valida heartbeat, playback, programación o asignaciones del canal.</p>
                      </div>
                    </div>
                  </article>
                ))
              ) : (
                <article className="rounded-[24px] border border-emerald-200 bg-emerald-50 px-4 py-4 text-sm text-emerald-800">
                  <p className="font-semibold">Canal estable</p>
                  <p className="mt-1">El canal reporta estado, playback y programación sin alertas visibles.</p>
                </article>
              )}
            </div>
          </SectionCard>

          {clientId && branchId ? (
            <WorkspaceQuickLinks
              title="Accesos rapidos"
              subtitle="Atajos para revisar campañas, contenido, layouts, timeline y preview de sucursal."
              links={[
                ...(canOpenClientRoutes
                  ? [
                      {
                        label: "Campañas",
                        caption: "Volver a las campañas visibles del cliente.",
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
                  label: "Timeline",
                  caption: "Regresar al timeline de esta sucursal.",
                  to: buildBranchTimelinePath(clientId, branchId),
                  icon: Route,
                },
                {
                  label: "Preview",
                  caption: "Abrir el preview completo de la sucursal.",
                  to: buildBranchPreviewPath(clientId, branchId),
                  icon: Presentation,
                },
                {
                  label: "Layouts",
                  caption: "Consultar layouts disponibles.",
                  to: buildBranchLayoutsPath(clientId, branchId),
                  icon: PanelsTopLeft,
                },
              ]}
            />
          ) : null}
        </div>
      </div>

      <SectionCard title="Timeline del canal" subtitle="Lectura visual de los slots visibles especificamente para este canal.">
        <TimelineBoard schedules={scopedSchedules} campaigns={campaigns} selectedCampaignId={previewBundle?.campaign?.id ?? null} />
      </SectionCard>
    </div>
  );
}
