import { useEffect, useMemo, useState } from "react";

import type { ChannelPreviewBundle } from "../lib/preview";
import { PreviewCanvas } from "./PreviewCanvas";
import { PreviewControls } from "./PreviewControls";
import { PreviewPlaylistPanel } from "./PreviewPlaylistPanel";
import { VideowallPreviewCanvas } from "./VideowallPreviewCanvas";

export function PreviewPlayer({
  bundle,
  title,
  subtitle,
}: {
  bundle: ChannelPreviewBundle | null;
  title: string;
  subtitle: string;
}) {
  const [isPlaying, setIsPlaying] = useState(true);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [elapsedMs, setElapsedMs] = useState(0);

  const playlistLength = bundle?.playlistEntries.length ?? 0;
  const currentEntry = bundle?.playlistEntries[currentIndex] ?? null;
  const currentDurationSeconds = currentEntry?.duration_seconds ?? 0;
  const progressPercent = currentDurationSeconds > 0 ? (elapsedMs / (currentDurationSeconds * 1000)) * 100 : 0;

  useEffect(() => {
    setCurrentIndex(0);
    setElapsedMs(0);
    setIsPlaying(true);
  }, [bundle?.channel.id, bundle?.campaign?.id, playlistLength]);

  useEffect(() => {
    if (!isPlaying || !bundle || playlistLength === 0) {
      return;
    }

    const timer = window.setInterval(() => {
      setElapsedMs((current) => {
        const itemDurationMs = Math.max(1, (bundle.playlistEntries[currentIndex]?.duration_seconds ?? 1) * 1000);
        if (current + 1000 < itemDurationMs) {
          return current + 1000;
        }

        setCurrentIndex((index) => (index + 1) % bundle.playlistEntries.length);
        return 0;
      });
    }, 1000);

    return () => window.clearInterval(timer);
  }, [bundle, currentIndex, isPlaying, playlistLength]);

  const campaignSourceLabel = useMemo(() => {
    if (!bundle) {
      return "Sin contexto";
    }

    switch (bundle.campaignSource) {
      case "active_schedule":
        return "Timeline activo";
      case "assignment":
        return "Asignacion directa";
      case "scheduled_fallback":
        return "Timeline de respaldo";
      default:
        return "Sin campaña";
    }
  }, [bundle]);

  if (!bundle) {
    return (
      <div className="rounded-[32px] border border-slate-200 bg-white p-6 shadow-card">
        <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Preview</p>
        <h2 className="mt-3 font-display text-3xl text-ink">{title}</h2>
        <p className="mt-2 text-sm text-slate-600">{subtitle}</p>
        <div className="mt-6 rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-10 text-center text-sm text-slate-600">
          Selecciona un canal con campaña y timeline para ver la simulación.
        </div>
      </div>
    );
  }

  return (
    <section className="space-y-5">
      <article className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-accent">Preview</p>
            <h2 className="mt-3 font-display text-3xl text-ink">{title}</h2>
            <p className="mt-2 text-sm text-slate-600">{subtitle}</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Campaña</p>
              <p className="mt-2 text-sm font-semibold text-ink">{bundle.campaign?.name ?? "Sin campaña"}</p>
            </div>
            <div className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Origen</p>
              <p className="mt-2 text-sm font-semibold text-ink">{campaignSourceLabel}</p>
            </div>
            <div className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
              <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Timeline activo</p>
              <p className="mt-2 text-sm font-semibold text-ink">{bundle.activeSchedules.length} slots activos</p>
            </div>
          </div>
        </div>
      </article>

      <div className="grid gap-5 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="space-y-5">
          <PreviewCanvas
            channel={bundle.channel}
            layout={bundle.layout}
            zones={bundle.zones}
            entries={bundle.playlistEntries}
            currentIndex={currentIndex}
          />
          <PreviewControls
            isPlaying={isPlaying}
            onTogglePlay={() => setIsPlaying((current) => !current)}
            onNext={() => {
              if (bundle.playlistEntries.length === 0) {
                return;
              }
              setCurrentIndex((index) => (index + 1) % bundle.playlistEntries.length);
              setElapsedMs(0);
            }}
            currentIndex={currentIndex}
            playlistLength={bundle.playlistEntries.length}
            progressPercent={progressPercent}
            elapsedSeconds={Math.floor(elapsedMs / 1000)}
            itemDurationSeconds={currentDurationSeconds}
            totalDurationSeconds={bundle.playlistDurationSeconds}
          />
        </div>

        <PreviewPlaylistPanel
          entries={bundle.playlistEntries}
          currentIndex={currentIndex}
          onSelectIndex={(index) => {
            setCurrentIndex(index);
            setElapsedMs(0);
          }}
          campaignName={bundle.campaign?.name ?? null}
          scheduleTitle={bundle.currentSchedule?.title ?? null}
          layoutName={bundle.layout?.name ?? null}
          channelMode={bundle.channel.mode}
        />
      </div>

      {bundle.videowall ? (
        <VideowallPreviewCanvas
          videowall={bundle.videowall}
          currentEntry={currentEntry}
          channelName={bundle.channel.name}
        />
      ) : null}
    </section>
  );
}
