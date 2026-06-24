import type {
  Campaign,
  CampaignSequencePreviewLayout,
  CampaignSequencePreviewResolvedItem,
  CampaignSequenceItem,
  Channel,
  ContentItem,
  Layout,
  PlaylistItem,
  PlayerRuntimeContent,
  ScheduleItem,
  Videowall,
  VideowallNode,
} from "../types/domain";
import { API_ORIGIN } from "./api";

export type PreviewZone = {
  key: string;
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
};

export type PreviewPlaylistEntry = PlaylistItem & {
  content: PreviewContentSource | null;
  contentLabel: string;
  durationLabel: string;
};

export type PreviewContentSource = Pick<
  ContentItem,
  "id" | "name" | "type" | "file_path" | "source_url" | "html_content" | "text_content" | "duration_seconds" | "metadata_json"
> & {
  download_url?: string | null;
  mime_type?: string | null;
};

export type PreviewLayoutSource = Pick<
  Layout,
  "id" | "name" | "template" | "canvas_width" | "canvas_height" | "zones" | "is_default"
>;

export type PreviewRenderableEntry = {
  id: string;
  duration_seconds: number;
  zone_key: string;
  content: PreviewContentSource | null;
  layout?: PreviewLayoutSource | null;
  contentLabel: string;
};

export type PreviewSequenceEntry = CampaignSequenceItem & {
  content: PreviewContentSource | null;
  layout: PreviewLayoutSource | null;
  zone_key: string;
  contentLabel: string;
  durationLabel: string;
  itemLabel: string;
  itemCode: string;
  itemColorClass: string;
  sourceLabel: string;
};

export type PreviewCampaignChoice = {
  campaign: Campaign | null;
  source: "active_schedule" | "assignment" | "scheduled_fallback" | "none";
  schedule: ScheduleItem | null;
};

export type PreviewVideowallState = {
  wall: Videowall | null;
  nodes: VideowallNode[];
  segment: VideowallNode | null;
};

export type ChannelPreviewBundle = {
  channel: Channel;
  schedules: ScheduleItem[];
  activeSchedules: ScheduleItem[];
  currentSchedule: ScheduleItem | null;
  campaign: Campaign | null;
  campaignSource: PreviewCampaignChoice["source"];
  layout: Layout | null;
  zones: PreviewZone[];
  playlistEntries: PreviewPlaylistEntry[];
  playlistDurationSeconds: number;
  videowall: PreviewVideowallState | null;
};

function normalizeZoneValue(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function normalizeZoneKey(value: string | null | undefined) {
  return (value ?? "main").trim().toLowerCase();
}

function resolveSequenceZoneKey(options: Record<string, unknown>) {
  const zoneKey = options.zone_key;
  return typeof zoneKey === "string" && zoneKey.trim() ? zoneKey : "main";
}

export function isStreamLikeContent(content: PreviewContentSource) {
  const source = `${content.source_url ?? ""} ${content.file_path ?? ""}`.toLowerCase();
  return ["m3u8", "rtmp", "youtube.com/live", "youtu.be/live", "twitch.tv", ".mpd"].some((token) => source.includes(token));
}

export function isPreviewEntryDurationEditable(entry: PreviewSequenceEntry | null) {
  if (!entry) {
    return false;
  }
  if (entry.item_type === "layout") {
    return true;
  }
  if (!entry.content) {
    return false;
  }
  if (entry.content.type === "video") {
    return false;
  }
  if (entry.content.type === "url" && isStreamLikeContent(entry.content)) {
    return false;
  }
  return ["image", "text", "html", "url"].includes(entry.content.type);
}

export function getPreviewEntryDurationModeLabel(entry: PreviewSequenceEntry | null) {
  if (!entry) {
    return "Selecciona un clip para editarlo.";
  }
  if (entry.item_type === "layout") {
    return "Duracion manual del layout.";
  }
  if (!entry.content) {
    return "Sin contenido disponible.";
  }
  if (entry.content.type === "video") {
    return "Duracion real del video.";
  }
  if (entry.content.type === "url" && isStreamLikeContent(entry.content)) {
    return "Duracion por fuente continua.";
  }
  return "Duracion manual del clip.";
}

function describeSequenceEntry(item: CampaignSequenceItem, content: PreviewContentSource | null, layout: PreviewLayoutSource | null) {
  if (item.item_type === "layout") {
    return {
      contentLabel: "Layout visual reutilizable dentro de la campaña.",
      itemLabel: layout?.name ?? "Layout visual",
      itemCode: "LAY",
      itemColorClass: "border-violet-300 bg-violet-50 text-violet-700",
      sourceLabel: layout ? `${layout.canvas_width}x${layout.canvas_height} · ${layout.template}` : "Layout pendiente",
    };
  }

  if (!content) {
    return {
      contentLabel: "Contenido no disponible",
      itemLabel: "Contenido faltante",
      itemCode: "N/A",
      itemColorClass: "border-slate-300 bg-slate-100 text-slate-600",
      sourceLabel: "Sin origen disponible",
    };
  }

  switch (content.type) {
    case "image":
      return {
        contentLabel: "Imagen fija lista para loop.",
        itemLabel: content.name,
        itemCode: "IMG",
        itemColorClass: "border-sky-300 bg-sky-50 text-sky-700",
        sourceLabel: content.file_path ?? "Imagen cargada",
      };
    case "video":
      return {
        contentLabel: "Video listo para reproducción.",
        itemLabel: content.name,
        itemCode: "VID",
        itemColorClass: "border-emerald-300 bg-emerald-50 text-emerald-700",
        sourceLabel: content.file_path ?? "Video cargado",
      };
    case "url":
      return isStreamLikeContent(content)
        ? {
            contentLabel: "Stream o enlace en vivo.",
            itemLabel: content.name,
            itemCode: "STR",
            itemColorClass: "border-rose-300 bg-rose-50 text-rose-700",
            sourceLabel: content.source_url ?? "Stream externo",
          }
        : {
            contentLabel: "URL externa embebida.",
            itemLabel: content.name,
            itemCode: "URL",
            itemColorClass: "border-orange-300 bg-orange-50 text-orange-700",
            sourceLabel: content.source_url ?? "URL externa",
          };
    case "html":
      return {
        contentLabel: "HTML personalizado listo para player.",
        itemLabel: content.name,
        itemCode: "HTML",
        itemColorClass: "border-amber-300 bg-amber-50 text-amber-700",
        sourceLabel: "HTML embebido",
      };
    case "text":
      return {
        contentLabel: "Texto dinámico o mensaje simple.",
        itemLabel: content.name,
        itemCode: "TXT",
        itemColorClass: "border-slate-300 bg-slate-100 text-slate-700",
        sourceLabel: content.text_content?.slice(0, 120) ?? "Texto simple",
      };
    default:
      return {
        contentLabel: describeContent(content),
        itemLabel: content.name,
        itemCode: "MED",
        itemColorClass: "border-slate-300 bg-slate-100 text-slate-700",
        sourceLabel: content.file_path ?? content.source_url ?? "Contenido",
      };
  }
}

export function minutesFromTime(raw: string | null) {
  if (!raw) {
    return 0;
  }

  const [hours, minutes] = raw.split(":").map((value) => Number(value));
  return hours * 60 + minutes;
}

export function isScheduleActiveNow(schedule: ScheduleItem) {
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

export function getSchedulesForChannel(channelId: string, branchId: string, schedules: ScheduleItem[]) {
  return schedules.filter((item) => item.channel_id === channelId || (!item.channel_id && item.branch_id === branchId));
}

export function formatSeconds(totalSeconds: number) {
  const safeSeconds = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = safeSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function describeContent(content: PreviewContentSource | ContentItem | null) {
  if (!content) {
    return "Contenido no disponible";
  }

  switch (content.type) {
    case "image":
      return "Imagen";
    case "video":
      return "Video";
    case "url":
      return content.source_url ?? "URL externa";
    case "html":
      return "HTML embebido";
    case "text":
      return content.text_content?.slice(0, 80) ?? "Texto dinámico";
    default:
      return "Contenido";
  }
}

export function normalizeRuntimeContent(content: PlayerRuntimeContent | null): PreviewContentSource | null {
  if (!content) {
    return null;
  }

  return {
    id: content.id,
    name: content.name,
    type: content.type,
    file_path: content.file_path,
    download_url: content.download_url,
    source_url: content.source_url,
    html_content: content.html_content,
    text_content: content.text_content,
    mime_type: content.mime_type,
    duration_seconds: content.duration_seconds ?? 15,
    metadata_json: content.metadata_json ?? {},
  };
}

function isAbsoluteAssetUrl(path: string) {
  return path.startsWith("http://") || path.startsWith("https://") || path.startsWith("data:") || path.startsWith("blob:");
}

export function resolvePreviewMediaUrl(content: PreviewContentSource | ContentItem | null) {
  if (!content) {
    return null;
  }

  const runtimeCandidate = "download_url" in content ? content.download_url : null;
  const candidate = runtimeCandidate ?? content.source_url ?? content.file_path;
  if (!candidate) {
    return null;
  }
  if (isAbsoluteAssetUrl(candidate)) {
    return candidate;
  }

  return candidate.startsWith("/") ? `${API_ORIGIN}${candidate}` : `${API_ORIGIN}/${candidate}`;
}

export function normalizePreviewLayout(layout: CampaignSequencePreviewLayout | null): PreviewLayoutSource | null {
  if (!layout) {
    return null;
  }

  return {
    id: layout.id,
    name: layout.name,
    template: layout.template,
    canvas_width: layout.canvas_width,
    canvas_height: layout.canvas_height,
    zones: layout.zones,
    is_default: layout.is_default,
  };
}

export function enrichPlaylistEntries(playlist: PlaylistItem[], contents: ContentItem[]) {
  return playlist
    .slice()
    .sort((left, right) => left.sort_order - right.sort_order)
    .map((item) => {
      const content = contents.find((entry) => entry.id === item.content_id) ?? null;

      return {
        ...item,
        content,
        contentLabel: describeContent(content),
        durationLabel: formatSeconds(item.duration_seconds),
      } satisfies PreviewPlaylistEntry;
    });
}

export function enrichSequenceEntries(sequence: CampaignSequenceItem[], contents: ContentItem[], layouts: Layout[]) {
  return sequence
    .slice()
    .sort((left, right) => left.sort_order - right.sort_order)
    .map((item) => {
      const content = item.content_id ? contents.find((entry) => entry.id === item.content_id) ?? null : null;
      const layout = item.layout_id ? layouts.find((entry) => entry.id === item.layout_id) ?? null : null;
      const descriptor = describeSequenceEntry(item, content, layout);

      return {
        ...item,
        content,
        layout,
        zone_key: resolveSequenceZoneKey(item.options_json ?? {}),
        contentLabel: descriptor.contentLabel,
        durationLabel: formatSeconds(item.duration_seconds),
        itemLabel: descriptor.itemLabel,
        itemCode: descriptor.itemCode,
        itemColorClass: descriptor.itemColorClass,
        sourceLabel: descriptor.sourceLabel,
      } satisfies PreviewSequenceEntry;
    });
}

export function decorateResolvedSequenceEntries(sequence: CampaignSequencePreviewResolvedItem[]) {
  return sequence
    .slice()
    .sort((left, right) => left.sort_order - right.sort_order)
    .map((item) => {
      const content = normalizeRuntimeContent(item.content);
      const layout = normalizePreviewLayout(item.layout);
      const descriptor = describeSequenceEntry(item, content, layout);

      return {
        ...item,
        content,
        layout,
        zone_key: item.zone_key || resolveSequenceZoneKey(item.options_json ?? {}),
        contentLabel: descriptor.contentLabel,
        durationLabel: formatSeconds(item.duration_seconds),
        itemLabel: descriptor.itemLabel,
        itemCode: descriptor.itemCode,
        itemColorClass: descriptor.itemColorClass,
        sourceLabel: descriptor.sourceLabel,
      } satisfies PreviewSequenceEntry;
    });
}

export function getPreviewCampaignChoice({
  schedules,
  campaigns,
  assignedCampaignIds,
}: {
  schedules: ScheduleItem[];
  campaigns: Campaign[];
  assignedCampaignIds: string[];
}): PreviewCampaignChoice {
  const prioritizedSchedules = schedules.slice().sort((left, right) => right.priority - left.priority);
  const activeSchedule = prioritizedSchedules.find((item) => isScheduleActiveNow(item)) ?? null;

  if (activeSchedule) {
    return {
      campaign: campaigns.find((item) => item.id === activeSchedule.campaign_id) ?? null,
      source: "active_schedule",
      schedule: activeSchedule,
    };
  }

  const assignedCampaign =
    assignedCampaignIds.map((campaignId) => campaigns.find((item) => item.id === campaignId) ?? null).find(Boolean) ?? null;
  if (assignedCampaign) {
    return {
      campaign: assignedCampaign,
      source: "assignment",
      schedule: prioritizedSchedules[0] ?? null,
    };
  }

  const fallbackSchedule = prioritizedSchedules[0] ?? null;
  if (fallbackSchedule) {
    return {
      campaign: campaigns.find((item) => item.id === fallbackSchedule.campaign_id) ?? null,
      source: "scheduled_fallback",
      schedule: fallbackSchedule,
    };
  }

  return {
    campaign: null,
    source: "none",
    schedule: null,
  };
}

export function getPreviewLayout({
  campaign,
  schedule,
  layouts,
}: {
  campaign: Campaign | null;
  schedule: ScheduleItem | null;
  layouts: Layout[];
}) {
  const layoutId = schedule?.layout_id ?? campaign?.layout_id ?? null;
  return layoutId ? layouts.find((item) => item.id === layoutId) ?? null : null;
}

export function normalizePreviewZones(layout: PreviewLayoutSource | Layout | null, channel: Channel) {
  if (!layout || !Array.isArray(layout.zones) || layout.zones.length === 0) {
    return [
      {
        key: "main",
        label: "Principal",
        x: 0,
        y: 0,
        width: channel.resolution_width,
        height: channel.resolution_height,
      },
    ] satisfies PreviewZone[];
  }

  return layout.zones.map((zone, index) => {
    const rawKey = typeof zone.key === "string" ? zone.key : `zone-${index + 1}`;
    return {
      key: rawKey,
      label: rawKey === "main" ? "Principal" : rawKey,
      x: normalizeZoneValue(zone.x, 0),
      y: normalizeZoneValue(zone.y, 0),
      width: normalizeZoneValue(zone.width, layout.canvas_width),
      height: normalizeZoneValue(zone.height, layout.canvas_height),
    } satisfies PreviewZone;
  });
}

export function getZonePreviewEntry({
  entries,
  currentIndex,
  zoneKey,
}: {
  entries: PreviewRenderableEntry[];
  currentIndex: number;
  zoneKey: string;
}) {
  const normalizedZoneKey = normalizeZoneKey(zoneKey);
  const currentEntry = entries[currentIndex] ?? null;
  if (currentEntry && normalizeZoneKey(currentEntry.zone_key) === normalizedZoneKey) {
    return currentEntry;
  }

  const zoneEntries = entries.filter((entry) => normalizeZoneKey(entry.zone_key) === normalizedZoneKey);
  if (zoneEntries.length > 0) {
    return zoneEntries[0];
  }

  if (normalizedZoneKey === "main") {
    return currentEntry;
  }

  return null;
}

export function getPlaylistTotalDuration(entries: PreviewPlaylistEntry[]) {
  return entries.reduce((sum, entry) => sum + entry.duration_seconds, 0);
}

export function getSequenceTotalDuration(entries: PreviewSequenceEntry[]) {
  return entries.reduce((sum, entry) => sum + entry.duration_seconds, 0);
}

export function buildChannelPreviewBundle({
  channel,
  branchId,
  schedules,
  campaigns,
  layouts,
  contents,
  playlist,
  assignedCampaignIds,
  videowall,
}: {
  channel: Channel;
  branchId: string;
  schedules: ScheduleItem[];
  campaigns: Campaign[];
  layouts: Layout[];
  contents: ContentItem[];
  playlist: PlaylistItem[];
  assignedCampaignIds: string[];
  videowall: PreviewVideowallState | null;
}): ChannelPreviewBundle {
  const scopedSchedules = getSchedulesForChannel(channel.id, branchId, schedules);
  const activeSchedules = scopedSchedules.filter((item) => isScheduleActiveNow(item));
  const campaignChoice = getPreviewCampaignChoice({
    schedules: scopedSchedules,
    campaigns,
    assignedCampaignIds,
  });
  const layout = getPreviewLayout({
    campaign: campaignChoice.campaign,
    schedule: campaignChoice.schedule,
    layouts,
  });
  const zones = normalizePreviewZones(layout, channel);
  const playlistEntries = enrichPlaylistEntries(playlist, contents);

  return {
    channel,
    schedules: scopedSchedules,
    activeSchedules,
    currentSchedule: campaignChoice.schedule,
    campaign: campaignChoice.campaign,
    campaignSource: campaignChoice.source,
    layout,
    zones,
    playlistEntries,
    playlistDurationSeconds: getPlaylistTotalDuration(playlistEntries),
    videowall,
  };
}
