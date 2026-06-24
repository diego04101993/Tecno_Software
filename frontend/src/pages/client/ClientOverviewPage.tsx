import { ArrowRightLeft, PlusCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { SectionCard } from "../../components/SectionCard";
import { BranchFormDrawer } from "../../components/dashboard/BranchFormDrawer";
import { BranchInfrastructureCard } from "../../components/dashboard/BranchInfrastructureCard";
import type { CampaignPublishSummary } from "../../components/dashboard/CampaignPublishResultSummary";
import type { PublishTargetBranchGroup, PublishableChannelTarget } from "../../components/dashboard/CampaignPublishTargetTree";
import { DashboardExecutiveBar } from "../../components/dashboard/DashboardExecutiveBar";
import { ScheduledCampaignDrawer, type ScheduledDrawerContextCard } from "../../components/dashboard/ScheduledCampaignDrawer";
import { ScreenCreateDrawer } from "../../components/dashboard/ScreenCreateDrawer";
import { ScreenEditDrawer } from "../../components/dashboard/ScreenEditDrawer";
import { ScreenGroupActions, ScreenGroupTile } from "../../components/dashboard/ScreenGroupTile";
import { ScreenTile } from "../../components/dashboard/ScreenTile";
import { SimpleVideowallMatrix } from "../../components/dashboard/SimpleVideowallMatrix";
import { VideowallEditDrawer } from "../../components/dashboard/VideowallEditDrawer";
import { VideowallMonitorDrawer, type VideowallMonitorDrawerTarget } from "../../components/dashboard/VideowallMonitorDrawer";
import { getApiErrorMessage } from "../../components/dashboard/apiError";
import { DeleteConfirmDialog } from "../../components/operations/DeleteConfirmDialog";
import { apiRequest } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { canWriteClientScope } from "../../lib/rbac";
import type { Branch, Campaign, Channel, Client, KioskScreen, ScheduleItem, Videowall, VideowallNode } from "../../types/domain";

type ScreenTileView = {
  id: string;
  title: string;
  status: Channel["status"];
  isOnline: boolean;
  mode: string;
  resolutionLabel: string;
  channelCode: string;
  campaignLabel: string;
  currentCampaignId: string | null;
  hasAssignedCampaign: boolean;
  playbackLabel: string | null;
  hardwareLabel: string | null;
  lastHeartbeatAt: string | null;
  heartbeatAgeSeconds: number | null;
  eyebrow?: string;
  channel: Channel | null;
  node: VideowallNode | null;
  videowall: Videowall | null;
};

type ExpandedGroupView = {
  id: string;
  title: string;
  subtitle: string;
  status: Channel["status"];
  summary: Array<{ label: string; value: string }>;
  channel: Channel;
  outputs: ScreenTileView[];
};

type VideowallGroupView = {
  id: string;
  title: string;
  subtitle: string;
  status: Channel["status"];
  summary: Array<{ label: string; value: string }>;
  columns: number;
  rows: number;
  occupiedPositions: number[];
  configuredNodes: number;
  pendingNodes: number;
  videowall: Videowall;
  nodes: ScreenTileView[];
};

type BranchInfrastructureView = {
  branch: Branch;
  totalScreens: number;
  onlineScreens: number;
  offlineScreens: number;
  visibleCampaigns: number;
  standaloneTiles: ScreenTileView[];
  expandedGroups: ExpandedGroupView[];
  videowallGroups: VideowallGroupView[];
};

type BranchDrawerState =
  | {
      mode: "create";
      branch: null;
    }
  | {
      mode: "edit";
      branch: Branch;
    };

type ScreenEditTarget = {
  branch: Branch;
  channel: Channel;
} | null;

type ChannelPlayerConfigSummary = {
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
  active_campaign: {
    campaign_id: string;
    name: string;
    source: "schedule" | "assignment" | null;
    priority: number | null;
    schedule_id: string | null;
    assignment_id: string | null;
  } | null;
};

type PublishDrawerState =
  | {
      scope: "screen";
      title: string;
      subtitle: string;
      fixedTargets: PublishableChannelTarget[];
      currentCampaignId: string | null;
      currentCampaignLabel: string;
      contextCards: ScheduledDrawerContextCard[];
    }
  | {
      scope: "branch";
      title: string;
      subtitle: string;
      branchId: string;
    }
  | {
      scope: "global";
      title: string;
      subtitle: string;
    }
  | {
      scope: "videowall";
      title: string;
      subtitle: string;
      fixedTargets: PublishableChannelTarget[];
      currentCampaignId: string | null;
      currentCampaignLabel: string;
      contextCards: ScheduledDrawerContextCard[];
      resolutionHint: {
        label: string;
        width: number;
        height: number;
      };
    }
  | null;

type VideowallEditTarget = {
  branch: Branch;
  videowall: Videowall;
} | null;

type VideowallMonitorState = {
  branch: Branch;
  videowall: Videowall;
  nodes: VideowallNode[];
  target: VideowallMonitorDrawerTarget;
} | null;

type DeleteTarget =
  | {
      kind: "branch";
      branch: Branch;
      totalScreens: number;
    }
  | {
      kind: "channel";
      branch: Branch;
      channel: Channel;
    }
  | {
      kind: "videowall";
      branch: Branch;
      videowall: Videowall;
      nodesCount: number;
    }
  | {
      kind: "videowall-node";
      branch: Branch;
      channel: Channel;
      node: VideowallNode;
      videowall: Videowall;
    };

type FeedbackNotice =
  | {
      tone: "success" | "error";
      text: string;
    }
  | null;

function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

function isScheduleActiveNow(schedule: ScheduleItem) {
  if (!schedule.is_active) {
    return false;
  }

  const now = new Date();
  const todayIso = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  if (schedule.starts_on && todayIso < schedule.starts_on) {
    return false;
  }
  if (schedule.ends_on && todayIso > schedule.ends_on) {
    return false;
  }

  const day = now.getDay() === 0 ? 7 : now.getDay();
  if (schedule.days_of_week.length > 0 && !schedule.days_of_week.includes(day)) {
    return false;
  }

  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const startMinutes = schedule.start_time ? minutesFromTime(schedule.start_time) : 0;
  const endMinutes = schedule.end_time ? minutesFromTime(schedule.end_time) : 24 * 60;

  return currentMinutes >= startMinutes && currentMinutes <= endMinutes;
}

function getChannelSchedules(channel: Channel, schedules: ScheduleItem[]) {
  return schedules.filter((item) => item.channel_id === channel.id || (!item.channel_id && item.branch_id === channel.branch_id));
}

function getPhysicalScreenCount(channel: Channel) {
  return channel.mode === "expanded" ? Math.max(2, channel.screen_count) : 1;
}

function getResolutionLabel(channel: Channel) {
  return `${channel.resolution_width}x${channel.resolution_height}`;
}

function getPresenceStatus(isOnline: boolean): Channel["status"] {
  return isOnline ? "online" : "offline";
}

function getGroupStatus(isOnlineFlags: boolean[]) {
  if (isOnlineFlags.length === 0) {
    return "unknown" as const;
  }

  if (isOnlineFlags.every(Boolean)) {
    return "online" as const;
  }
  if (isOnlineFlags.every((isOnline) => !isOnline)) {
    return "offline" as const;
  }
  return "unknown" as const;
}

export function ClientOverviewPage() {
  const { token, user } = useAuth();
  const { clientId } = useParams();
  const [client, setClient] = useState<Client | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [channelPlayerConfigs, setChannelPlayerConfigs] = useState<Record<string, ChannelPlayerConfigSummary>>({});
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [videowalls, setVideowalls] = useState<Videowall[]>([]);
  const [videowallNodes, setVideowallNodes] = useState<VideowallNode[]>([]);
  const [screens, setScreens] = useState<KioskScreen[]>([]);
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<FeedbackNotice>(null);
  const [expandedBranchIds, setExpandedBranchIds] = useState<string[]>([]);
  const [branchDrawerState, setBranchDrawerState] = useState<BranchDrawerState | null>(null);
  const [screenDrawerBranchId, setScreenDrawerBranchId] = useState<string | null>(null);
  const [screenEditTarget, setScreenEditTarget] = useState<ScreenEditTarget>(null);
  const [videowallEditTarget, setVideowallEditTarget] = useState<VideowallEditTarget>(null);
  const [videowallMonitorState, setVideowallMonitorState] = useState<VideowallMonitorState>(null);
  const [selectedVideowallCell, setSelectedVideowallCell] = useState<{ videowallId: string; position: number } | null>(null);
  const [publishDrawerState, setPublishDrawerState] = useState<PublishDrawerState>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const canManageInfrastructure = canWriteClientScope(user?.role);

  async function loadDashboard() {
    if (!token || !clientId) {
      return;
    }

    try {
      const [clientResponse, branchesResponse, channelsResponse, campaignsResponse, videowallsResponse, screensResponse, schedulesResponse] =
        await Promise.all([
          apiRequest<Client>(`/clients/${clientId}`, { token }),
          apiRequest<Branch[]>(`/branches?client_id=${clientId}`, { token }),
          apiRequest<Channel[]>(`/channels?client_id=${clientId}`, { token }),
          apiRequest<Campaign[]>(`/campaigns?client_id=${clientId}`, { token }),
          apiRequest<Videowall[]>(`/videowalls?client_id=${clientId}`, { token }),
          apiRequest<KioskScreen[]>(`/kiosk/screens?client_id=${clientId}`, { token }),
          apiRequest<ScheduleItem[]>(`/schedules?client_id=${clientId}`, { token }),
        ]);

      const nodesPerWall = await Promise.all(
        videowallsResponse.map((videowall) =>
          apiRequest<VideowallNode[]>(`/videowalls/${videowall.id}/nodes`, { token }).catch(() => [] as VideowallNode[]),
        ),
      );
      const playerConfigsResponse = await Promise.all(
        channelsResponse.map((channel) =>
          apiRequest<ChannelPlayerConfigSummary>(`/channels/${channel.id}/player-config`, { token }).catch(() => null),
        ),
      );
      const playerConfigMap = playerConfigsResponse.reduce<Record<string, ChannelPlayerConfigSummary>>((accumulator, config) => {
        if (config) {
          accumulator[config.channel.id] = config;
        }
        return accumulator;
      }, {});

      setClient(clientResponse);
      setBranches(branchesResponse);
      setChannels(channelsResponse);
      setChannelPlayerConfigs(playerConfigMap);
      setCampaigns(campaignsResponse);
      setVideowalls(videowallsResponse);
      setVideowallNodes(nodesPerWall.flat());
      setScreens(screensResponse);
      setSchedules(schedulesResponse);
      setError(null);
    } catch (nextError) {
      setError(getApiErrorMessage(nextError, "No se pudo cargar el dashboard del cliente."));
    }
  }

  useEffect(() => {
    void loadDashboard();
  }, [clientId, token]);

  useEffect(() => {
    if (!token || !clientId) {
      return;
    }

    const intervalId = window.setInterval(() => {
      void loadDashboard();
    }, 30000);

    return () => window.clearInterval(intervalId);
  }, [clientId, token]);

  useEffect(() => {
    setExpandedBranchIds((current) => {
      const validIds = current.filter((branchId) => branches.some((branch) => branch.id === branchId));
      if (validIds.length === current.length && validIds.every((branchId, index) => branchId === current[index])) {
        return current;
      }
      return validIds;
    });
  }, [branches]);

  useEffect(() => {
    setDeleteError(null);
  }, [deleteTarget]);

  const campaignsById = useMemo(() => new Map(campaigns.map((campaign) => [campaign.id, campaign])), [campaigns]);
  const videowallNodeByChannelId = useMemo(() => new Map(videowallNodes.map((node) => [node.channel_id, node])), [videowallNodes]);
  const nodesByVideowallId = useMemo(() => {
    const map = new Map<string, VideowallNode[]>();
    videowallNodes.forEach((node) => {
      const current = map.get(node.videowall_id) ?? [];
      current.push(node);
      map.set(node.videowall_id, current);
    });
    return map;
  }, [videowallNodes]);

  const channelContextById = useMemo(() => {
    const map = new Map<
      string,
      {
        campaignLabel: string;
        currentCampaignId: string | null;
        hasAssignedCampaign: boolean;
      }
    >();

    channels.forEach((channel) => {
      const playerConfig = channelPlayerConfigs[channel.id];
      const activeCampaign = playerConfig?.active_campaign;
      if (activeCampaign) {
        map.set(channel.id, {
          campaignLabel: activeCampaign.name,
          currentCampaignId: activeCampaign.campaign_id,
          hasAssignedCampaign: true,
        });
        return;
      }

      const channelSchedules = getChannelSchedules(channel, schedules);
      const activeSchedules = channelSchedules.filter((item) => isScheduleActiveNow(item));
      const selectedSchedule = activeSchedules.sort((left, right) => right.priority - left.priority)[0] ?? null;
      const campaignLabel = selectedSchedule ? campaignsById.get(selectedSchedule.campaign_id)?.name ?? "Sin campaña visible" : "Sin campaña visible";

      map.set(channel.id, {
        campaignLabel,
        currentCampaignId: selectedSchedule?.campaign_id ?? null,
        hasAssignedCampaign: Boolean(selectedSchedule),
      });
    });

    return map;
  }, [campaignsById, channelPlayerConfigs, channels, schedules]);

  const branchInfrastructure = useMemo<BranchInfrastructureView[]>(() => {
    return branches.map((branch) => {
      const branchChannels = channels.filter((channel) => channel.branch_id === branch.id);
      const branchChannelIds = new Set(branchChannels.map((channel) => channel.id));
      const activeCampaignIds = new Set(
        branchChannels
          .map((channel) => channelContextById.get(channel.id)?.currentCampaignId ?? null)
          .filter((campaignId): campaignId is string => Boolean(campaignId)),
      );
      const branchVideowalls = videowalls.filter((videowall) =>
        (nodesByVideowallId.get(videowall.id) ?? []).some((node) => branchChannelIds.has(node.channel_id)),
      );

      const standaloneChannels = branchChannels.filter((channel) => channel.mode !== "expanded" && !videowallNodeByChannelId.has(channel.id));
      const expandedChannels = branchChannels.filter((channel) => channel.mode === "expanded" && !videowallNodeByChannelId.has(channel.id));

      const standaloneTiles = standaloneChannels.map<ScreenTileView>((channel) => {
        const context = channelContextById.get(channel.id);

        return {
          id: channel.id,
          title: channel.name,
          status: getPresenceStatus(channel.is_online),
          isOnline: channel.is_online,
          mode: channel.mode,
          resolutionLabel: getResolutionLabel(channel),
          channelCode: channel.channel_code,
          campaignLabel: context?.campaignLabel ?? "Sin campaña visible",
          currentCampaignId: context?.currentCampaignId ?? null,
          hasAssignedCampaign: context?.hasAssignedCampaign ?? false,
          playbackLabel: channel.current_playback,
          hardwareLabel: channel.hardware_identifier,
          lastHeartbeatAt: channel.last_heartbeat_at ?? channel.last_ping_at,
          heartbeatAgeSeconds: channel.heartbeat_age_seconds,
          eyebrow: "Pantalla",
          channel,
          node: null,
          videowall: null,
        };
      });

      const expandedGroups = expandedChannels.map<ExpandedGroupView>((channel) => {
        const context = channelContextById.get(channel.id);
        const outputs = Array.from({ length: Math.max(2, channel.screen_count) }, (_, index) => ({
          id: `${channel.id}-output-${index + 1}`,
          title: `Salida ${index + 1}`,
          status: getPresenceStatus(channel.is_online),
          isOnline: channel.is_online,
          mode: channel.mode,
          resolutionLabel: getResolutionLabel(channel),
          channelCode: channel.channel_code,
          campaignLabel: context?.campaignLabel ?? "Sin campaña visible",
          currentCampaignId: context?.currentCampaignId ?? null,
          hasAssignedCampaign: context?.hasAssignedCampaign ?? false,
          playbackLabel: channel.current_playback,
          hardwareLabel: channel.hardware_identifier,
          lastHeartbeatAt: channel.last_heartbeat_at ?? channel.last_ping_at,
          heartbeatAgeSeconds: channel.heartbeat_age_seconds,
          eyebrow: channel.name,
          channel,
          node: null,
          videowall: null,
        }));

        return {
          id: channel.id,
          title: channel.name,
          subtitle: "Grupo expandido desde una misma PC",
          status: getPresenceStatus(channel.is_online),
          summary: [
            { label: "Tipo", value: "Expandido" },
            { label: "Salidas", value: `${Math.max(2, channel.screen_count)}` },
            { label: "Resolución", value: getResolutionLabel(channel) },
            { label: "Código player", value: channel.channel_code },
          ],
          channel,
          outputs,
        };
      });

      const videowallGroups = branchVideowalls.map<VideowallGroupView>((videowall) => {
        const nodes = (nodesByVideowallId.get(videowall.id) ?? [])
          .filter((node) => branchChannelIds.has(node.channel_id))
          .sort((left, right) => left.position_index - right.position_index);
        const nodeChannels = nodes
          .map((node) => branchChannels.find((channel) => channel.id === node.channel_id))
          .filter((channel): channel is Channel => Boolean(channel));
        const nodeChannelById = new Map(nodeChannels.map((channel) => [channel.id, channel]));

        const nodeTiles = nodes.map<ScreenTileView>((node) => {
          const channel = nodeChannelById.get(node.channel_id) ?? null;
          const context = channel ? channelContextById.get(channel.id) : null;

          return {
            id: node.id,
            title: `Monitor ${node.position_index}`,
            status: channel ? getPresenceStatus(channel.is_online) : "unknown",
            isOnline: channel?.is_online ?? false,
            mode: channel?.mode ?? "videowall",
            resolutionLabel: channel ? getResolutionLabel(channel) : `${node.width}x${node.height}`,
            channelCode: channel?.channel_code ?? "Sin código",
            campaignLabel: context?.campaignLabel ?? "Sin campaña visible",
            currentCampaignId: context?.currentCampaignId ?? null,
            hasAssignedCampaign: context?.hasAssignedCampaign ?? false,
            playbackLabel: channel?.current_playback ?? null,
            hardwareLabel: channel?.hardware_identifier ?? null,
            lastHeartbeatAt: channel?.last_heartbeat_at ?? channel?.last_ping_at ?? null,
            heartbeatAgeSeconds: channel?.heartbeat_age_seconds ?? null,
            eyebrow: videowall.name,
            channel,
            node,
            videowall,
          };
        });

        const onlineCount = nodeChannels.filter((channel) => channel.is_online).length;
        const offlineCount = nodeChannels.filter((channel) => !channel.is_online).length;
        const configuredNodes = nodes.length;
        const pendingNodes = Math.max(0, videowall.columns * videowall.rows - configuredNodes);

        return {
          id: videowall.id,
          title: videowall.name,
          subtitle: "Grupo videowall sincronizado",
          status: getGroupStatus(nodeChannels.map((channel) => channel.is_online)),
          summary: [
            { label: "Matriz", value: `${videowall.columns}x${videowall.rows}` },
            { label: "Resolución total", value: `${videowall.total_width}x${videowall.total_height}` },
            { label: "Configurados", value: `${configuredNodes}` },
            { label: "Pendientes", value: `${pendingNodes}` },
            { label: "Online", value: `${onlineCount}/${nodes.length}` },
            { label: "Offline", value: `${offlineCount}` },
          ],
          columns: videowall.columns,
          rows: videowall.rows,
          occupiedPositions: nodes.map((node) => node.position_index),
          configuredNodes,
          pendingNodes,
          videowall,
          nodes: nodeTiles,
        };
      });

      return {
        branch,
        totalScreens: branchChannels.reduce((sum, channel) => sum + getPhysicalScreenCount(channel), 0),
        onlineScreens: branchChannels.reduce((sum, channel) => sum + (channel.is_online ? getPhysicalScreenCount(channel) : 0), 0),
        offlineScreens: branchChannels.reduce((sum, channel) => sum + (!channel.is_online ? getPhysicalScreenCount(channel) : 0), 0),
        visibleCampaigns: activeCampaignIds.size,
        standaloneTiles,
        expandedGroups,
        videowallGroups,
      };
    });
  }, [branches, channelContextById, channels, nodesByVideowallId, schedules, videowallNodeByChannelId, videowalls]);

  const publishTargetGroups = useMemo<PublishTargetBranchGroup[]>(() => {
    return branchInfrastructure.map((branchCard) => {
      const targets: PublishableChannelTarget[] = [];
      const seenChannelIds = new Set<string>();

      function pushTarget(target: Omit<PublishableChannelTarget, "slotIndex" | "visualLabel">) {
        if (seenChannelIds.has(target.channelId)) {
          return;
        }
        seenChannelIds.add(target.channelId);
        targets.push({
          ...target,
          slotIndex: targets.length + 1,
          visualLabel: `Pantalla ${targets.length + 1}`,
        });
      }

      branchCard.standaloneTiles.forEach((tile) => {
        if (!tile.channel) {
          return;
        }
        pushTarget({
          branchId: branchCard.branch.id,
          branchName: branchCard.branch.name,
          branchCode: branchCard.branch.code,
          branchTimezone: branchCard.branch.timezone,
          channelId: tile.channel.id,
          channelName: tile.channel.name,
          channelCode: tile.channel.channel_code,
          mode: tile.channel.mode,
          status: getPresenceStatus(tile.channel.is_online),
          hasCampaign: tile.hasAssignedCampaign,
          currentCampaignId: tile.currentCampaignId,
          currentCampaignLabel: tile.campaignLabel,
        });
      });

      branchCard.expandedGroups.forEach((group) => {
        const representativeTile = group.outputs[0];
        pushTarget({
          branchId: branchCard.branch.id,
          branchName: branchCard.branch.name,
          branchCode: branchCard.branch.code,
          branchTimezone: branchCard.branch.timezone,
          channelId: group.channel.id,
          channelName: group.channel.name,
          channelCode: group.channel.channel_code,
          mode: group.channel.mode,
          status: getPresenceStatus(group.channel.is_online),
          hasCampaign: representativeTile?.hasAssignedCampaign ?? false,
          currentCampaignId: representativeTile?.currentCampaignId ?? null,
          currentCampaignLabel: representativeTile?.campaignLabel ?? "Sin campaña visible",
        });
      });

      branchCard.videowallGroups.forEach((group) => {
        group.nodes.forEach((tile) => {
          if (!tile.channel) {
            return;
          }
          pushTarget({
            branchId: branchCard.branch.id,
            branchName: branchCard.branch.name,
            branchCode: branchCard.branch.code,
            branchTimezone: branchCard.branch.timezone,
            channelId: tile.channel.id,
            channelName: tile.channel.name,
            channelCode: tile.channel.channel_code,
            mode: tile.channel.mode,
            status: getPresenceStatus(tile.channel.is_online),
            hasCampaign: tile.hasAssignedCampaign,
            currentCampaignId: tile.currentCampaignId,
            currentCampaignLabel: tile.campaignLabel,
          });
        });
      });

      return {
        branchId: branchCard.branch.id,
        branchName: branchCard.branch.name,
        branchCode: branchCard.branch.code,
        branchTimezone: branchCard.branch.timezone,
        targets,
      };
    });
  }, [branchInfrastructure]);

  const executiveMetrics = useMemo(() => {
    const totalScreens = channels.reduce((sum, channel) => sum + getPhysicalScreenCount(channel), 0);
    const onlineScreens = channels.reduce((sum, channel) => sum + (channel.is_online ? getPhysicalScreenCount(channel) : 0), 0);
    const offlineScreens = channels.reduce((sum, channel) => sum + (!channel.is_online ? getPhysicalScreenCount(channel) : 0), 0);

    return [
      { label: "Sucursales", value: branches.length, icon: "branches" as const },
      { label: "Pantallas", value: totalScreens, icon: "screens" as const, tone: "neutral" as const },
      { label: "Online", value: onlineScreens, icon: "online" as const, tone: "emerald" as const },
      { label: "Offline", value: offlineScreens, icon: "offline" as const, tone: "rose" as const },
      { label: "Videowalls", value: videowalls.length, icon: "videowalls" as const, tone: "cyan" as const },
      { label: "Kioskos", value: screens.length, icon: "kiosks" as const, tone: "amber" as const },
    ];
  }, [branches.length, channels, screens.length, videowalls.length]);

  const activeCreateBranch = useMemo(
    () => branches.find((branch) => branch.id === screenDrawerBranchId) ?? null,
    [branches, screenDrawerBranchId],
  );

  const activePublishTargetGroups = useMemo(() => {
    if (!publishDrawerState || publishDrawerState.scope === "screen" || publishDrawerState.scope === "videowall") {
      return [];
    }

    if (publishDrawerState.scope === "branch") {
      return publishTargetGroups.filter((branchGroup) => branchGroup.branchId === publishDrawerState.branchId);
    }

    return publishTargetGroups;
  }, [publishDrawerState, publishTargetGroups]);

  function buildScreenContextCards(branch: Branch, channel: Channel, currentLabel: string): ScheduledDrawerContextCard[] {
    return [
      {
        label: "Sucursal",
        title: branch.name,
        helper: branch.code,
      },
      {
        label: "Pantalla / canal",
        title: channel.name,
        helper: `${channel.mode} · ${channel.resolution_width}x${channel.resolution_height} · ${channel.channel_code}`,
      },
      {
        label: "Campana visible actual",
        title: currentLabel,
      },
    ];
  }

  function findPublishTargetByChannelId(channelId: string) {
    return publishTargetGroups.flatMap((branchGroup) => branchGroup.targets).find((target) => target.channelId === channelId) ?? null;
  }

  function openScreenPublishDrawer(branch: Branch, channel: Channel, currentCampaignId: string | null, currentCampaignLabel: string) {
    const target =
      findPublishTargetByChannelId(channel.id) ?? {
        branchId: branch.id,
        branchName: branch.name,
        branchCode: branch.code,
        branchTimezone: branch.timezone,
        channelId: channel.id,
        channelName: channel.name,
        channelCode: channel.channel_code,
        mode: channel.mode,
        status: getPresenceStatus(channel.is_online),
        hasCampaign: Boolean(currentCampaignId),
        currentCampaignId,
        currentCampaignLabel,
        slotIndex: 1,
        visualLabel: "Pantalla 1",
      };

    setPublishDrawerState({
      scope: "screen",
      title: channel.name,
      subtitle: "Publica ahora o programa la campana de esta pantalla sin salir del dashboard.",
      fixedTargets: [target],
      currentCampaignId,
      currentCampaignLabel,
      contextCards: buildScreenContextCards(branch, channel, currentCampaignLabel),
    });
  }

  function openVideowallPublishDrawer(branch: Branch, group: VideowallGroupView) {
    const fixedTargets = group.nodes
      .filter((tile) => tile.channel)
      .map((tile, index) => ({
        branchId: branch.id,
        branchName: branch.name,
        branchCode: branch.code,
        branchTimezone: branch.timezone,
        channelId: (tile.channel as Channel).id,
        channelName: (tile.channel as Channel).name,
        channelCode: (tile.channel as Channel).channel_code,
        mode: (tile.channel as Channel).mode,
        status: (tile.channel as Channel).status,
        hasCampaign: tile.hasAssignedCampaign,
        currentCampaignId: tile.currentCampaignId,
        currentCampaignLabel: tile.campaignLabel,
        slotIndex: tile.node?.position_index ?? index + 1,
        visualLabel: `Monitor ${tile.node?.position_index ?? index + 1}`,
      }));

    const uniqueCampaignIds = Array.from(new Set(fixedTargets.map((target) => target.currentCampaignId).filter(Boolean)));
    const primaryTarget = fixedTargets[0] ?? null;
    const currentCampaignLabel =
      uniqueCampaignIds.length > 1 ? "Campanas mixtas" : primaryTarget?.currentCampaignLabel ?? "Sin campana visible";
    setPublishDrawerState({
      scope: "videowall",
      title: `Publicar campana en ${group.title}`,
      subtitle: "Todos los nodos del videowall recibiran la misma campana y cada uno mantendra su crop individual en runtime.",
      fixedTargets,
      currentCampaignId: uniqueCampaignIds.length === 1 ? uniqueCampaignIds[0] ?? null : null,
      currentCampaignLabel,
      contextCards: [
        {
          label: "Sucursal",
          title: branch.name,
          helper: branch.code,
        },
        {
          label: "Videowall",
          title: group.title,
          helper: `${group.columns}x${group.rows} · ${group.configuredNodes} nodo(s) configurado(s)`,
        },
        {
          label: "Campana visible actual",
          title: currentCampaignLabel,
        },
      ],
      resolutionHint: {
        label: "Resolucion total del videowall",
        width: group.videowall.total_width,
        height: group.videowall.total_height,
      },
    });
  }

  function openVideowallMonitorDrawer(branch: Branch, videowall: Videowall, preferredPosition?: number) {
    const wallNodes = (nodesByVideowallId.get(videowall.id) ?? []).sort((left, right) => left.position_index - right.position_index);
    const totalPositions = videowall.columns * videowall.rows;
    const occupiedPositions = wallNodes.map((node) => node.position_index);
    const nextPosition =
      preferredPosition && !occupiedPositions.includes(preferredPosition)
        ? preferredPosition
        : Array.from({ length: totalPositions }, (_, index) => index + 1).find((position) => !occupiedPositions.includes(position)) ?? null;

    if (!nextPosition) {
      setFeedback({
        tone: "error",
        text: `El videowall ${videowall.name} ya está completo. Elimina un monitor o amplía la matriz antes de agregar otro.`,
      });
      return;
    }

    setSelectedVideowallCell({ videowallId: videowall.id, position: nextPosition });
    setVideowallMonitorState({
      branch,
      videowall,
      nodes: wallNodes,
      target: {
        mode: "create",
        preferredPosition: nextPosition,
      },
    });
  }

  const deleteDialogCopy = useMemo(() => {
    if (!deleteTarget) {
      return null;
    }

    if (deleteTarget.kind === "branch") {
      return {
        title: `Eliminar sucursal ${deleteTarget.branch.name}`,
        description:
          deleteTarget.totalScreens > 0
            ? `Esta sucursal tiene ${deleteTarget.totalScreens} pantalla(s). Si aún existen canales asociados, el sistema bloqueará el borrado y te mostrará la dependencia exacta.`
            : "La sucursal se eliminará del dashboard. Si aparecen dependencias, el sistema lo indicará antes de completar el borrado.",
        confirmLabel: "Eliminar sucursal",
      };
    }

    if (deleteTarget.kind === "videowall-node") {
      return {
        title: `Eliminar pantalla ${deleteTarget.channel.name}`,
        description: `Se eliminar\u00e1 el nodo ${deleteTarget.node.position_index} del videowall ${deleteTarget.videowall.name} junto con su canal asociado. Si la pantalla sigue en l\u00ednea, el sistema la bloquear\u00e1. Si est\u00e1 offline, tambi\u00e9n limpiar\u00e1 sus campa\u00f1as y relaciones antes de borrarla.`,
        confirmLabel: "Eliminar pantalla",
      };
    }

    if (deleteTarget.kind === "videowall") {
      return {
        title: `Eliminar videowall ${deleteTarget.videowall.name}`,
        description: `\u00bfEliminar videowall completo? Esta acci\u00f3n intentar\u00e1 borrar el grupo, ${deleteTarget.nodesCount} monitor(es) y sus canales asociados. Solo se bloquear\u00e1 si alg\u00fan monitor sigue en l\u00ednea; en ese caso, el sistema te dir\u00e1 exactamente cu\u00e1l es.`,
        confirmLabel: "Eliminar videowall",
      };
    }

    return {
      title: `Eliminar pantalla ${deleteTarget.channel.name}`,
      description: "La pantalla se eliminar\u00e1 del dashboard. Solo se bloquear\u00e1 si sigue en l\u00ednea o si pertenece a un videowall. Si est\u00e1 offline, el sistema limpiar\u00e1 sus relaciones y la borrar\u00e1.",
      confirmLabel: "Eliminar pantalla",
    };
  }, [deleteTarget]);

  async function handleBranchSaved(savedBranch: Branch, mode: BranchDrawerState["mode"]) {
    await loadDashboard();
    setFeedback({
      tone: "success",
      text: mode === "create" ? "Sucursal creada correctamente." : "Sucursal actualizada correctamente.",
    });
  }

  async function handleScreenSaved(savedChannel: Channel) {
    await loadDashboard();
    setFeedback({
      tone: "success",
      text: `Pantalla ${savedChannel.name} actualizada correctamente.`,
    });
  }

  async function handleVideowallSaved(savedVideowall: Videowall) {
    await loadDashboard();
    setFeedback({
      tone: "success",
      text: `Videowall ${savedVideowall.name} actualizado correctamente.`,
    });
  }

  async function handleVideowallMonitorSaved(message: string) {
    await loadDashboard();
    setFeedback({
      tone: "success",
      text: message,
    });
  }

  async function handlePublishCompleted(summary: CampaignPublishSummary, campaign: Campaign | null, publishMode: "now" | "scheduled") {
    await loadDashboard();
    setFeedback({
      tone: summary.errors.length > 0 ? "error" : "success",
      text:
        publishMode === "scheduled"
          ? summary.errors.length > 0
            ? `Programacion completada con incidencias: ${summary.published} regla(s) creadas y ${summary.errors.length} error(es).`
            : `Programacion guardada para ${summary.total} pantalla(s) con la campana ${campaign?.name ?? "seleccionada"}.`
          : summary.errors.length > 0
            ? `Publicacion completada con incidencias: ${summary.published} publicadas, ${summary.existing} ya estaban publicadas y ${summary.errors.length} error(es).`
            : `Listo, la campana ${campaign?.name ?? "seleccionada"} se proceso en ${summary.total} pantalla(s).`,
    });
  }

  async function handleDeleteConfirm() {
    if (!token || !deleteTarget) {
      return;
    }

    setIsDeleting(true);
    setDeleteError(null);
    try {
      if (deleteTarget.kind === "branch") {
        await apiRequest<void>(`/branches/${deleteTarget.branch.id}`, {
          method: "DELETE",
          token,
        });
      } else if (deleteTarget.kind === "videowall") {
        await apiRequest<void>(`/videowalls/${deleteTarget.videowall.id}`, {
          method: "DELETE",
          token,
        });
      } else if (deleteTarget.kind === "videowall-node") {
        await apiRequest<void>(`/videowalls/${deleteTarget.videowall.id}/nodes/${deleteTarget.node.id}`, {
          method: "DELETE",
          token,
        });
      } else {
        await apiRequest<void>(`/channels/${deleteTarget.channel.id}`, {
          method: "DELETE",
          token,
        });
      }

      await loadDashboard();
      setFeedback({
        tone: "success",
        text:
          deleteTarget.kind === "branch"
            ? `Sucursal ${deleteTarget.branch.name} eliminada correctamente.`
            : deleteTarget.kind === "videowall"
              ? `Videowall ${deleteTarget.videowall.name} eliminado correctamente.`
            : `Pantalla ${deleteTarget.channel.name} eliminada correctamente.`,
      });
      setSelectedVideowallCell(null);
      setDeleteTarget(null);
    } catch (nextError) {
      const humanMessage = getApiErrorMessage(nextError, "No se pudo completar el borrado.");
      setDeleteError(humanMessage);
      setFeedback({
        tone: "error",
        text: humanMessage,
      });
    } finally {
      setIsDeleting(false);
    }
  }

  return (
    <div className="space-y-5">
      {error ? <div className="rounded-[24px] bg-rose-50 px-5 py-4 text-sm text-rose-700">{error}</div> : null}
      {feedback ? (
        <div
          className={[
            "rounded-[24px] px-5 py-4 text-sm",
            feedback.tone === "success" ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-900",
          ].join(" ")}
        >
          {feedback.text}
        </div>
      ) : null}

      <DashboardExecutiveBar
        title={client?.name ?? "Cliente"}
        subtitle="El dashboard del cliente funciona como centro de infraestructura: sucursales, pantallas, grupos expandidos, videowalls y estado operativo visible sin saltar a otra vista."
        metrics={executiveMetrics}
        action={
          canManageInfrastructure ? (
            <>
              <button
                className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-5 py-3 text-sm font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
                type="button"
                onClick={() =>
                  setPublishDrawerState({
                    scope: "global",
                    title: "Publicar campana global",
                    subtitle: "Selecciona sucursales y pantallas para publicar ahora o programar una campana a escala cliente.",
                  })
                }
              >
                <ArrowRightLeft className="h-4 w-4" />
                Publicar campaña global
              </button>
              <button
                className="inline-flex items-center gap-2 rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-900"
                type="button"
                onClick={() => setBranchDrawerState({ mode: "create", branch: null })}
              >
                <PlusCircle className="h-4 w-4" />
                Crear sucursal
              </button>
            </>
          ) : null
        }
      />

      <SectionCard
        title="Infraestructura por sucursal"
        subtitle="Cada card concentra pantallas, grupos y campañas visibles. El detalle se expande inline para evitar navegar a otra página."
      >
        <div className="space-y-4">
          {branchInfrastructure.length > 0 ? (
            branchInfrastructure.map((branchCard) => {
              const isExpanded = expandedBranchIds.includes(branchCard.branch.id);

              return (
                <BranchInfrastructureCard
                  key={branchCard.branch.id}
                  title={branchCard.branch.name}
                  code={branchCard.branch.code}
                  address={branchCard.branch.address}
                  totalScreens={branchCard.totalScreens}
                  onlineScreens={branchCard.onlineScreens}
                  offlineScreens={branchCard.offlineScreens}
                  visibleCampaigns={branchCard.visibleCampaigns}
                  expanded={isExpanded}
                  canAddScreen={canManageInfrastructure}
                  canPublishCampaign={canManageInfrastructure}
                  canEdit={canManageInfrastructure}
                  canDelete={canManageInfrastructure}
                  onAddScreen={() => setScreenDrawerBranchId(branchCard.branch.id)}
                  onPublishCampaign={() =>
                    setPublishDrawerState({
                      scope: "branch",
                      title: `Publicar campana en ${branchCard.branch.name}`,
                      subtitle: "Selecciona las pantallas de esta sucursal para publicar ahora o programar una campana.",
                      branchId: branchCard.branch.id,
                    })
                  }
                  onEdit={() => setBranchDrawerState({ mode: "edit", branch: branchCard.branch })}
                  onDelete={() =>
                    setDeleteTarget({
                      kind: "branch",
                      branch: branchCard.branch,
                      totalScreens: branchCard.totalScreens,
                    })
                  }
                  onToggle={() =>
                    setExpandedBranchIds((current) =>
                      current.includes(branchCard.branch.id)
                        ? current.filter((branchId) => branchId !== branchCard.branch.id)
                        : [...current, branchCard.branch.id],
                    )
                  }
                >
                  <div className="space-y-4">
                    {branchCard.standaloneTiles.length > 0 ? (
                      <div className="grid gap-4 xl:grid-cols-2">
                        {branchCard.standaloneTiles.map((tile) => (
                          <ScreenTile
                            key={tile.id}
                            title={tile.title}
                            status={tile.status}
                            mode={tile.mode}
                            resolutionLabel={tile.resolutionLabel}
                            channelCode={tile.channelCode}
                            campaignLabel={tile.campaignLabel}
                            hasAssignedCampaign={tile.hasAssignedCampaign}
                            playbackLabel={tile.playbackLabel}
                            hardwareLabel={tile.hardwareLabel}
                            lastHeartbeatAt={tile.lastHeartbeatAt}
                            heartbeatAgeSeconds={tile.heartbeatAgeSeconds}
                            eyebrow={tile.eyebrow}
                            onPublishCampaign={
                              canManageInfrastructure && tile.channel
                                ? () =>
                                    openScreenPublishDrawer(
                                      branchCard.branch,
                                      tile.channel as Channel,
                                      tile.currentCampaignId,
                                      tile.campaignLabel,
                                    )
                                : undefined
                            }
                            onEdit={
                              canManageInfrastructure && tile.channel
                                ? () => setScreenEditTarget({ branch: branchCard.branch, channel: tile.channel as Channel })
                                : undefined
                            }
                            onDelete={
                              canManageInfrastructure && tile.channel
                                ? () =>
                                    setDeleteTarget({
                                      kind: "channel",
                                      branch: branchCard.branch,
                                      channel: tile.channel as Channel,
                                    })
                                : undefined
                            }
                          />
                        ))}
                      </div>
                    ) : null}

                    {branchCard.expandedGroups.map((group) => (
                      <ScreenGroupTile
                        key={group.id}
                        title={group.title}
                        subtitle={group.subtitle}
                        status={group.status}
                        summary={group.summary}
                        actions={
                          canManageInfrastructure ? (
                            <ScreenGroupActions
                              onEdit={() => setScreenEditTarget({ branch: branchCard.branch, channel: group.channel })}
                              onDelete={() =>
                                setDeleteTarget({
                                  kind: "channel",
                                  branch: branchCard.branch,
                                  channel: group.channel,
                                })
                              }
                            />
                          ) : undefined
                        }
                      >
                        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                          {group.outputs.map((tile) => (
                            <ScreenTile
                              key={tile.id}
                              title={tile.title}
                              status={tile.status}
                              mode={tile.mode}
                              resolutionLabel={tile.resolutionLabel}
                              channelCode={tile.channelCode}
                              campaignLabel={tile.campaignLabel}
                              hasAssignedCampaign={tile.hasAssignedCampaign}
                              playbackLabel={tile.playbackLabel}
                              hardwareLabel={tile.hardwareLabel}
                              lastHeartbeatAt={tile.lastHeartbeatAt}
                              heartbeatAgeSeconds={tile.heartbeatAgeSeconds}
                              eyebrow={tile.eyebrow}
                              onPublishCampaign={
                                canManageInfrastructure && tile.channel
                                  ? () =>
                                      openScreenPublishDrawer(
                                        branchCard.branch,
                                        tile.channel as Channel,
                                        tile.currentCampaignId,
                                        tile.campaignLabel,
                                      )
                                  : undefined
                              }
                            />
                          ))}
                        </div>
                      </ScreenGroupTile>
                    ))}

                    {branchCard.videowallGroups.map((group) => (
                      <ScreenGroupTile
                        key={group.id}
                        title={group.title}
                        subtitle={group.subtitle}
                        status={group.status}
                        summary={group.summary}
                        actions={
                          canManageInfrastructure ? (
                            <div className="flex flex-wrap gap-2">
                              <button
                                className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
                                type="button"
                                onClick={() => openVideowallPublishDrawer(branchCard.branch, group)}
                              >
                                <ArrowRightLeft className="h-3.5 w-3.5" />
                                Publicar campana
                              </button>
                              <button
                                className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
                                type="button"
                                onClick={() => openVideowallMonitorDrawer(branchCard.branch, group.videowall)}
                              >
                                <PlusCircle className="h-3.5 w-3.5" />
                                Agregar monitor
                              </button>
                              <button
                                className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
                                type="button"
                                onClick={() => setVideowallEditTarget({ branch: branchCard.branch, videowall: group.videowall })}
                              >
                                Editar videowall
                              </button>
                              <button
                                className="inline-flex items-center gap-2 rounded-full border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700 transition hover:border-rose-300 hover:bg-rose-50"
                                type="button"
                                onClick={() =>
                                  setDeleteTarget({
                                    kind: "videowall",
                                    branch: branchCard.branch,
                                    videowall: group.videowall,
                                    nodesCount: group.nodes.length,
                                  })
                                }
                              >
                                Eliminar videowall
                              </button>
                            </div>
                          ) : undefined
                        }
                      >
                        <div className="grid gap-4 xl:grid-cols-[220px_1fr]">
                          <div className="rounded-[24px] border border-slate-200 bg-white p-4">
                            <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Matriz interactiva</p>
                            <div className="mt-4">
                              <SimpleVideowallMatrix
                                columns={group.columns}
                                rows={group.rows}
                                occupiedPositions={group.occupiedPositions}
                                selectedPosition={selectedVideowallCell?.videowallId === group.id ? selectedVideowallCell.position : undefined}
                                showLabels
                                onSelectPosition={
                                  canManageInfrastructure
                                    ? (position) => {
                                        const isOccupied = group.occupiedPositions.includes(position);
                                        setSelectedVideowallCell({ videowallId: group.id, position });
                                        if (!isOccupied) {
                                          openVideowallMonitorDrawer(branchCard.branch, group.videowall, position);
                                        }
                                      }
                                    : undefined
                                }
                              />
                            </div>
                            <p className="mt-4 text-xs text-slate-500">
                              Haz clic en una celda vacía para agregar un monitor. Las celdas verdes ya están configuradas.
                            </p>
                            <div className="mt-4 flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                              <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-emerald-700">{group.configuredNodes} configurados</span>
                              <span className="rounded-full bg-slate-200 px-3 py-1.5 text-slate-700">{group.pendingNodes} pendientes</span>
                            </div>
                          </div>
                          <div className="grid gap-4 md:grid-cols-2">
                            {group.nodes.map((tile) => (
                              <ScreenTile
                                key={tile.id}
                                title={tile.title}
                                status={tile.status}
                                mode={tile.mode}
                                resolutionLabel={tile.resolutionLabel}
                                channelCode={tile.channelCode}
                                campaignLabel={tile.campaignLabel}
                                hasAssignedCampaign={tile.hasAssignedCampaign}
                                playbackLabel={tile.playbackLabel}
                                hardwareLabel={tile.hardwareLabel}
                                lastHeartbeatAt={tile.lastHeartbeatAt}
                                heartbeatAgeSeconds={tile.heartbeatAgeSeconds}
                                eyebrow={tile.eyebrow}
                                onPublishCampaign={
                                  canManageInfrastructure && tile.channel
                                    ? () =>
                                        openScreenPublishDrawer(
                                          branchCard.branch,
                                          tile.channel as Channel,
                                          tile.currentCampaignId,
                                          tile.campaignLabel,
                                        )
                                    : undefined
                                }
                                onEdit={
                                  canManageInfrastructure && tile.channel && tile.node && tile.videowall
                                    ? () => {
                                        const targetVideowall = tile.videowall as Videowall;
                                        const targetNode = tile.node as VideowallNode;
                                        const targetChannel = tile.channel as Channel;
                                        setSelectedVideowallCell({
                                          videowallId: targetVideowall.id,
                                          position: targetNode.position_index,
                                        });
                                        setVideowallMonitorState({
                                          branch: branchCard.branch,
                                          videowall: targetVideowall,
                                          nodes: (nodesByVideowallId.get(targetVideowall.id) ?? []).sort(
                                            (left, right) => left.position_index - right.position_index,
                                          ),
                                          target: {
                                            mode: "edit",
                                            node: targetNode,
                                            channel: targetChannel,
                                          },
                                        });
                                      }
                                    : undefined
                                }
                                onDelete={
                                  canManageInfrastructure && tile.channel && tile.node && tile.videowall
                                    ? () =>
                                        setDeleteTarget({
                                          kind: "videowall-node",
                                          branch: branchCard.branch,
                                          channel: tile.channel as Channel,
                                          node: tile.node as VideowallNode,
                                          videowall: tile.videowall as Videowall,
                                        })
                                    : undefined
                                }
                              />
                            ))}
                          </div>
                        </div>
                      </ScreenGroupTile>
                    ))}

                    {branchCard.standaloneTiles.length === 0 &&
                    branchCard.expandedGroups.length === 0 &&
                    branchCard.videowallGroups.length === 0 ? (
                      <div className="rounded-[24px] border border-slate-200 bg-slate-50 px-4 py-4 text-sm text-slate-600">
                        Esta sucursal aún no tiene pantallas registradas. Usa <strong>Agregar pantalla</strong> para crear la primera.
                      </div>
                    ) : null}
                  </div>
                </BranchInfrastructureCard>
              );
            })
          ) : (
            <div className="rounded-[28px] border border-slate-200 bg-slate-50 px-5 py-5 text-sm text-slate-600">
              Todavía no hay sucursales registradas para este cliente. Crea la primera sucursal desde el botón superior.
            </div>
          )}
        </div>
      </SectionCard>

      {clientId ? (
        <BranchFormDrawer
          open={Boolean(branchDrawerState)}
          mode={branchDrawerState?.mode ?? "create"}
          token={token}
          clientId={clientId}
          clientName={client?.name ?? "Cliente"}
          branch={branchDrawerState?.branch ?? null}
          onClose={() => setBranchDrawerState(null)}
          onSaved={handleBranchSaved}
        />
      ) : null}

      {clientId ? (
        <ScreenCreateDrawer
          open={Boolean(screenDrawerBranchId)}
          token={token}
          clientId={clientId}
          branch={activeCreateBranch}
          onClose={() => setScreenDrawerBranchId(null)}
          onCreated={async () => {
            await loadDashboard();
            setFeedback({
              tone: "success",
              text: "Pantalla creada correctamente.",
            });
          }}
        />
      ) : null}

      <ScreenEditDrawer
        open={Boolean(screenEditTarget)}
        token={token}
        branch={screenEditTarget?.branch ?? null}
        channel={screenEditTarget?.channel ?? null}
        onClose={() => setScreenEditTarget(null)}
        onSaved={handleScreenSaved}
      />

      <VideowallEditDrawer
        open={Boolean(videowallEditTarget)}
        token={token}
        videowall={videowallEditTarget?.videowall ?? null}
        nodes={videowallEditTarget ? nodesByVideowallId.get(videowallEditTarget.videowall.id) ?? [] : []}
        onClose={() => setVideowallEditTarget(null)}
        onSaved={handleVideowallSaved}
      />

      <VideowallMonitorDrawer
        open={Boolean(videowallMonitorState)}
        token={token}
        branch={videowallMonitorState?.branch ?? null}
        videowall={videowallMonitorState?.videowall ?? null}
        nodes={videowallMonitorState?.nodes ?? []}
        target={videowallMonitorState?.target ?? null}
        onClose={() => setVideowallMonitorState(null)}
        onSaved={handleVideowallMonitorSaved}
      />

      <ScheduledCampaignDrawer
        open={Boolean(publishDrawerState)}
        token={token}
        scope={publishDrawerState?.scope ?? "global"}
        title={publishDrawerState?.title ?? "Publicar campana"}
        subtitle={publishDrawerState?.subtitle ?? "Configura la publicacion o la programacion desde el dashboard."}
        campaigns={campaigns}
        targetGroups={activePublishTargetGroups}
        fixedTargets={
          publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall" ? publishDrawerState.fixedTargets : []
        }
        selectionMode={publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall" ? "fixed" : "free"}
        immediateStrategy={publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall" ? "replace" : "append"}
        currentCampaignId={
          publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall"
            ? publishDrawerState.currentCampaignId
            : null
        }
        currentCampaignLabel={
          publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall"
            ? publishDrawerState.currentCampaignLabel
            : "Sin campana visible"
        }
        contextCards={
          publishDrawerState?.scope === "screen" || publishDrawerState?.scope === "videowall" ? publishDrawerState.contextCards : []
        }
        resolutionHint={publishDrawerState?.scope === "videowall" ? publishDrawerState.resolutionHint : null}
        onClose={() => setPublishDrawerState(null)}
        onCompleted={handlePublishCompleted}
      />

      <DeleteConfirmDialog
        open={Boolean(deleteTarget && deleteDialogCopy)}
        title={deleteDialogCopy?.title ?? "Confirmar eliminación"}
        description={deleteDialogCopy?.description ?? "Confirma la eliminación del elemento seleccionado."}
        errorMessage={deleteError}
        confirmLabel={deleteDialogCopy?.confirmLabel ?? "Eliminar"}
        isDeleting={isDeleting}
        onCancel={() => {
          if (!isDeleting) {
            setDeleteError(null);
            setDeleteTarget(null);
          }
        }}
        onConfirm={() => void handleDeleteConfirm()}
      />
    </div>
  );
}

