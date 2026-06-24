import { Eye, LoaderCircle, Repeat, Shuffle } from "lucide-react";
import { useMemo } from "react";

import {
  decorateResolvedSequenceEntries,
  formatSeconds,
  normalizePreviewLayout,
  normalizePreviewZones,
  type PreviewSequenceEntry,
} from "../../lib/preview";
import type { Campaign, CampaignSequencePreviewPayload, Channel } from "../../types/domain";
import { LayoutPreviewCanvas } from "../LayoutPreviewCanvas";
import { PreviewContentSurface } from "../PreviewContentSurface";
import { TimelineTransportBar } from "./TimelineTransportBar";

type OperationPreviewSidebarProps = {
  campaign: Campaign | null;
  previewPayload: CampaignSequencePreviewPayload | null;
  entries: PreviewSequenceEntry[];
  currentEntry: PreviewSequenceEntry | null;
  nextEntry: PreviewSequenceEntry | null;
  currentIndex: number;
  currentLoopTimeSeconds: number;
  currentItemDurationSeconds: number;
  totalDurationSeconds: number;
  progressPercent: number;
  isPlaying: boolean;
  isLoading: boolean;
  onTogglePlay: () => void;
  onNext: () => void;
};

export function OperationPreviewSidebar({
  campaign,
  previewPayload,
  entries,
  currentEntry,
  nextEntry,
  currentIndex,
  currentLoopTimeSeconds,
  currentItemDurationSeconds,
  totalDurationSeconds,
  progressPercent,
  isPlaying,
  isLoading,
  onTogglePlay,
  onNext,
}: OperationPreviewSidebarProps) {
  const playbackMode = previewPayload?.playback_mode ?? campaign?.playback_mode ?? "sequential";
  const decoratedEntries = useMemo(() => (previewPayload ? decorateResolvedSequenceEntries(previewPayload.resolved_items) : entries), [entries, previewPayload]);
  const playableEntries = useMemo(() => decoratedEntries.filter((entry) => entry.is_enabled), [decoratedEntries]);
  const campaignLayout = normalizePreviewLayout(previewPayload?.campaign_layout ?? null);
  const activeLayout = currentEntry?.layout ?? campaignLayout;

  const previewChannel = useMemo<Channel>(() => {
    const width = activeLayout?.canvas_width ?? 1920;
    const height = activeLayout?.canvas_height ?? 1080;
    return {
      id: `sequence-preview-${campaign?.id ?? "empty"}`,
      created_at: "",
      updated_at: null,
      client_id: campaign?.client_id ?? "",
      branch_id: "campaign-studio",
      name: campaign?.name ?? "Campaign Studio",
      channel_code: "STUDIO",
      resolution_width: width,
      resolution_height: height,
      orientation: width >= height ? "horizontal" : "vertical",
      mode: "normal",
      screen_count: 1,
      status: "online",
      current_playback: currentEntry?.itemLabel ?? null,
      hardware_identifier: null,
      expanded_outputs: [],
      output_mapping_json: {
        enabled: false,
        profile: "normal",
        mode: "normal",
        mapping_mode: "normal",
        slice_count: 1,
        slice_direction: "vertical_stack",
        output_width: width,
        output_height: height,
        physical_width: width,
        physical_height: height,
        source_canvas_width: width,
        source_canvas_height: height,
        canvas_width: width,
        canvas_height: height,
        slices: [],
        viewport_x: 0,
        viewport_y: 0,
        viewport_width: width,
        viewport_height: height,
        scale_x: 1,
        scale_y: 1,
      },
      last_ping_at: null,
      last_heartbeat_at: null,
      heartbeat_age_seconds: null,
      is_online: true,
      notes: "Sequence preview runtime",
    };
  }, [activeLayout?.canvas_height, activeLayout?.canvas_width, campaign?.client_id, campaign?.id, campaign?.name, currentEntry?.itemLabel]);

  const zones = useMemo(() => normalizePreviewZones(activeLayout, previewChannel), [activeLayout, previewChannel]);
  const isVerticalFrame = previewChannel.resolution_height > previewChannel.resolution_width;
  const frameWidthStyle = useMemo(
    () => ({
      width: isVerticalFrame ? "min(100%, 248px)" : "min(100%, 760px)",
    }),
    [isVerticalFrame],
  );

  if (!campaign) {
    return (
      <div className="flex h-full min-h-0 min-w-0 flex-col px-4 py-4">
        <div className="flex items-center gap-3 border-b border-slate-200 pb-3">
          <span className="rounded-2xl bg-accentSoft p-3 text-accent">
            <Eye className="h-5 w-5" />
          </span>
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Preview de campaña</p>
            <p className="mt-1 text-sm text-slate-600">Selecciona una campaña para cargar la simulación del runtime.</p>
          </div>
        </div>
        <div className="grid min-h-0 flex-1 place-items-center px-6 py-6 text-center text-sm text-slate-500">Campaign Studio mostrará aquí la reproducción real de la campaña seleccionada.</div>
      </div>
    );
  }

  if (isLoading && !previewPayload) {
    return (
      <div className="flex h-full min-h-0 min-w-0 flex-col px-4 py-4">
        <div className="flex items-center gap-3 border-b border-slate-200 pb-3">
          <span className="rounded-2xl bg-accentSoft p-3 text-accent">
            <LoaderCircle className="h-5 w-5 animate-spin" />
          </span>
          <div>
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Runtime de campaña</p>
            <p className="mt-1 text-sm text-slate-600">Resolviendo timeline y preview exacto.</p>
          </div>
        </div>
        <div className="grid min-h-0 flex-1 place-items-center px-6 py-6 text-center text-sm text-slate-500">Cargando sequence-preview...</div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden px-3 py-3">
      <div className="border-b border-slate-200 pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Runtime preview V3</p>
            <p className="mt-1 truncate text-lg font-semibold text-ink" title={campaign.name}>
              {campaign.name}
            </p>
            <p className="mt-1 truncate text-sm text-slate-600">
              {activeLayout?.name ?? "Sin layout asociado"} - {previewChannel.resolution_width}x{previewChannel.resolution_height}
            </p>
          </div>

          <div className="flex flex-wrap gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            <span className="rounded-full bg-slate-100 px-3 py-1.5">
              {playbackMode === "random" ? (
                <>
                  <Shuffle className="mr-1 inline h-3.5 w-3.5" />
                  Aleatorio
                </>
              ) : (
                "Secuencia"
              )}
            </span>
            <span className="rounded-full bg-emerald-50 px-3 py-1.5 text-emerald-700">
              <Repeat className="mr-1 inline h-3.5 w-3.5" />
              Loop infinito
            </span>
          </div>
        </div>

        <div className="mt-3 grid gap-2 text-xs text-slate-500 xl:grid-cols-[minmax(0,1fr)_auto_auto]">
          <div className="min-w-0 rounded-[16px] border border-slate-200 bg-slate-50 px-3 py-2.5">
            <p className="uppercase tracking-[0.16em]">Actual</p>
            <p className="mt-1 truncate font-semibold text-slate-700">{currentEntry?.itemLabel ?? "Sin contenido activo"}</p>
          </div>
          <div className="min-w-0 rounded-[16px] border border-slate-200 bg-slate-50 px-3 py-2.5">
            <p className="uppercase tracking-[0.16em]">Siguiente</p>
            <p className="mt-1 truncate font-semibold text-slate-700">{nextEntry?.itemLabel ?? "Reinicio"}</p>
          </div>
          <div className="min-w-0 rounded-[16px] border border-slate-200 bg-slate-50 px-3 py-2.5">
            <p className="uppercase tracking-[0.16em]">Clip</p>
            <p className="mt-1 truncate font-semibold text-slate-700">
              {playableEntries.length === 0 ? 0 : currentIndex + 1}/{playableEntries.length}
            </p>
          </div>
        </div>
      </div>

      <div className="mt-3 flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-[22px] border border-slate-200 bg-slate-50/80 px-3 py-3">
        {playableEntries.length === 0 ? (
          <div className="grid h-full w-full place-items-center rounded-[24px] border border-dashed border-slate-300 bg-white px-6 text-center text-sm text-slate-500">
            Esta campaña no tiene items habilitados. Agrega clips a la timeline para continuar.
          </div>
        ) : activeLayout ? (
          <div className="w-full shrink-0" style={frameWidthStyle}>
            <LayoutPreviewCanvas channel={previewChannel} compact currentIndex={currentIndex} entries={playableEntries} zones={zones} />
          </div>
        ) : (
          <div className="w-full shrink-0" style={frameWidthStyle}>
            <div className="overflow-hidden rounded-[24px] border border-slate-200 bg-slate-950 p-3 shadow-card">
              <div
                className="relative overflow-hidden rounded-[20px] border border-white/10 bg-slate-900"
                style={{ aspectRatio: `${Math.max(1, previewChannel.resolution_width)} / ${Math.max(1, previewChannel.resolution_height)}` }}
              >
                <div className="absolute inset-0 p-3">
                  <PreviewContentSurface entry={currentEntry} title={currentEntry?.itemLabel ?? campaign.name} />
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="pt-2.5">
        <TimelineTransportBar
          currentIndex={currentIndex}
          currentItemDurationSeconds={currentItemDurationSeconds}
          currentLoopTimeSeconds={currentLoopTimeSeconds}
          isPlaying={isPlaying}
          onNext={onNext}
          onTogglePlay={onTogglePlay}
          playbackMode={playbackMode}
          playlistLength={playableEntries.length}
          progressPercent={progressPercent}
          totalDurationSeconds={totalDurationSeconds}
        />
        <p className="mt-2 truncate text-center text-[11px] uppercase tracking-[0.14em] text-slate-500" title={previewPayload?.runtime_signature ?? "Sin firma"}>
          {playableEntries.length > 0
            ? `Loop ${formatSeconds(totalDurationSeconds)} - firma ${previewPayload?.runtime_signature ?? "sin firma"}`
            : "Agrega bloques al timeline para simular el loop del runtime"}
        </p>
      </div>
    </div>
  );
}
