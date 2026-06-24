import { MonitorPlay, PanelsTopLeft, Presentation, Route } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { PreviewPlayer } from "../../components/PreviewPlayer";
import { SectionCard } from "../../components/SectionCard";
import { StatCard } from "../../components/StatCard";
import { TimelineBoard } from "../../components/TimelineBoard";
import { WorkspaceQuickLinks } from "../../components/WorkspaceQuickLinks";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import {
  buildBranchChannelsPath,
  buildBranchLayoutsPath,
  buildBranchTimelinePath,
  buildClientCampaignsPath,
} from "../../lib/workspace";
import {
  buildChannelPreviewBundle,
  getPreviewCampaignChoice,
  getSchedulesForChannel,
  isScheduleActiveNow,
  type ChannelPreviewBundle,
} from "../../lib/preview";
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

export function BranchPreviewPage() {
  const { token } = useAuth();
  const { clientId, branchId } = useParams();
  const [channels, setChannels] = useState<Channel[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [layouts, setLayouts] = useState<Layout[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);
  const [playerConfig, setPlayerConfig] = useState<PlayerConfig | null>(null);
  const [playlist, setPlaylist] = useState<PlaylistItem[]>([]);
  const [videowallPreview, setVideowallPreview] = useState<VideowallPreviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !clientId || !branchId) {
      return;
    }

    Promise.all([
      apiRequest<Channel[]>(`/channels?branch_id=${branchId}`, { token }),
      apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
      apiRequest<ContentItem[]>(`/contents?client_id=${clientId}`, { token }),
      apiRequest<Layout[]>(`/layouts?client_id=${clientId}`, { token }),
      apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}`, { token }),
    ])
      .then(([channelsResponse, campaignsResponse, contentsResponse, layoutsResponse, schedulesResponse]) => {
        const channelIds = new Set(channelsResponse.map((item) => item.id));
        const scopedSchedules = schedulesResponse.filter(
          (item) => item.branch_id === branchId || (item.channel_id ? channelIds.has(item.channel_id) : false),
        );

        setChannels(channelsResponse);
        setCampaigns(campaignsResponse);
        setContents(contentsResponse);
        setLayouts(layoutsResponse);
        setSchedules(scopedSchedules);
        setSelectedChannelId((current) => current ?? channelsResponse[0]?.id ?? null);
        setError(null);
      })
      .catch((nextError) => {
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar el preview de la sucursal");
      });
  }, [branchId, clientId, token]);

  useEffect(() => {
    if (!token || !selectedChannelId) {
      setPlayerConfig(null);
      return;
    }

    apiRequest<PlayerConfig>(`/channels/${selectedChannelId}/player-config`, { token })
      .then((response) => {
        setPlayerConfig(response);
        setError(null);
      })
      .catch((nextError) => {
        setPlayerConfig(null);
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la configuracion del canal");
      });
  }, [selectedChannelId, token]);

  const selectedChannel = useMemo(
    () => channels.find((item) => item.id === selectedChannelId) ?? playerConfig?.channel ?? null,
    [channels, playerConfig?.channel, selectedChannelId],
  );

  const previewCampaign = useMemo(() => {
    if (!selectedChannel || !branchId) {
      return null;
    }

    return getPreviewCampaignChoice({
      schedules: getSchedulesForChannel(selectedChannel.id, branchId, schedules),
      campaigns,
      assignedCampaignIds: playerConfig?.campaigns.map((item) => item.campaign_id) ?? [],
    });
  }, [branchId, campaigns, playerConfig?.campaigns, schedules, selectedChannel]);

  useEffect(() => {
    if (!token || !previewCampaign?.campaign?.id) {
      setPlaylist([]);
      return;
    }

    apiRequest<PlaylistItem[]>(`/campaigns/${previewCampaign.campaign.id}/playlist`, { token })
      .then((response) => setPlaylist(response))
      .catch((nextError) => {
        setPlaylist([]);
        setError(nextError instanceof Error ? nextError.message : "No se pudo cargar la playlist del preview");
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

  const previewBundle: ChannelPreviewBundle | null = useMemo(() => {
    if (!selectedChannel || !branchId) {
      return null;
    }

    const videowallSegment = playerConfig?.videowall_segment
      ? {
          id: `preview-${selectedChannel.id}`,
          created_at: "",
          updated_at: null,
          videowall_id: playerConfig.videowall_segment.videowall_id,
          channel_id: selectedChannel.id,
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
      channel: selectedChannel,
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
    contents,
    layouts,
    playerConfig?.campaigns,
    playerConfig?.videowall_segment,
    playlist,
    schedules,
    selectedChannel,
    videowallPreview,
  ]);

  const selectedChannelSchedules = useMemo(() => {
    if (!selectedChannel || !branchId) {
      return [];
    }

    return getSchedulesForChannel(selectedChannel.id, branchId, schedules);
  }, [branchId, schedules, selectedChannel]);

  const activeSchedules = useMemo(
    () => selectedChannelSchedules.filter((item) => isScheduleActiveNow(item)),
    [selectedChannelSchedules],
  );

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}

      <section className="grid gap-4 xl:grid-cols-4">
        <StatCard label="Canales" value={String(channels.length)} hint="Disponibles para simular" tone="teal" />
        <StatCard
          label="Campaña visible"
          value={previewBundle?.campaign?.name ?? "Sin campaña"}
          hint="Resuelta por timeline o asignación"
        />
        <StatCard
          label="Playlist"
          value={String(previewBundle?.playlistEntries.length ?? 0)}
          hint="Elementos listos para reproducirse"
        />
        <StatCard
          label="Videowall"
          value={previewBundle?.videowall?.wall ? `${previewBundle.videowall.wall.columns}x${previewBundle.videowall.wall.rows}` : "No"}
          hint="Preview completo si aplica"
          tone="orange"
        />
      </section>

      <div className="grid gap-5 xl:grid-cols-[0.92fr_1.08fr]">
        <SectionCard title="Canales de preview" subtitle="Selecciona el canal que quieres simular dentro de esta sucursal.">
          <div className="space-y-3">
            {channels.map((channel) => (
              <button
                key={channel.id}
                className={[
                  "w-full rounded-[24px] border px-4 py-4 text-left transition",
                  selectedChannelId === channel.id
                    ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.18)]"
                    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50",
                ].join(" ")}
                type="button"
                onClick={() => setSelectedChannelId(channel.id)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-ink" title={channel.name}>
                      {channel.name}
                    </p>
                    <p className="mt-1 text-sm text-slate-500">
                      {channel.resolution_width}x{channel.resolution_height} - {channel.mode}
                    </p>
                  </div>
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">
                    {channel.status}
                  </span>
                </div>
              </button>
            ))}
          </div>
        </SectionCard>

        <PreviewPlayer
          bundle={previewBundle}
          title={selectedChannel ? `Preview de ${selectedChannel.name}` : "Preview de sucursal"}
          subtitle="Simulación de solo lectura usando timeline, campañas, playlist, layout y videowall existentes."
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.08fr_0.92fr]">
        <SectionCard title="Timeline visual" subtitle="Lectura del timeline actual para el canal seleccionado, sin drag and drop ni cambios en pantalla.">
          <TimelineBoard
            schedules={selectedChannelSchedules}
            campaigns={campaigns}
            selectedCampaignId={previewBundle?.campaign?.id ?? null}
          />
        </SectionCard>

        <div className="space-y-5">
          <SectionCard title="Lectura rapida" subtitle="Contexto operativo del preview actual para usuarios no tecnicos.">
            <div className="space-y-3">
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Slot activo</p>
                <p className="mt-2 font-semibold text-ink">{previewBundle?.currentSchedule?.title ?? "Sin slot activo"}</p>
              </div>
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Layout</p>
                <p className="mt-2 font-semibold text-ink">{previewBundle?.layout?.name ?? "Sin layout"}</p>
              </div>
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Campaña activa</p>
                <p className="mt-2 font-semibold text-ink">{previewBundle?.campaign?.name ?? "Sin campaña"}</p>
              </div>
              <div className="rounded-[24px] border border-slate-200 bg-white px-4 py-4">
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Slots visibles</p>
                <p className="mt-2 font-semibold text-ink">
                  {selectedChannelSchedules.length} total - {activeSchedules.length} activos
                </p>
              </div>
            </div>
          </SectionCard>

          {clientId && branchId ? (
            <WorkspaceQuickLinks
              title="Accesos rapidos"
              subtitle="Atajos para revisar la configuracion que alimenta este preview."
              links={[
                {
                  label: "Canales",
                  caption: "Volver al inventario de canales.",
                  to: buildBranchChannelsPath(clientId, branchId),
                  icon: MonitorPlay,
                },
                {
                  label: "Timeline",
                  caption: "Abrir el timeline operativo de la sucursal.",
                  to: buildBranchTimelinePath(clientId, branchId),
                  icon: Route,
                },
                {
                  label: "Campañas",
                  caption: "Revisar las campañas del cliente.",
                  to: buildClientCampaignsPath(clientId),
                  icon: Presentation,
                },
                {
                  label: "Layouts",
                  caption: "Consultar layouts visibles en la sucursal.",
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
