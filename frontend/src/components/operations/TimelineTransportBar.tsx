import { Pause, Play, Shuffle, SkipForward } from "lucide-react";

import { formatSeconds } from "../../lib/preview";

type TimelineTransportBarProps = {
  isPlaying: boolean;
  playbackMode: "sequential" | "random";
  currentIndex: number;
  playlistLength: number;
  currentLoopTimeSeconds: number;
  totalDurationSeconds: number;
  currentItemDurationSeconds: number;
  progressPercent: number;
  onTogglePlay: () => void;
  onNext: () => void;
};

export function TimelineTransportBar({
  isPlaying,
  playbackMode,
  currentIndex,
  playlistLength,
  currentLoopTimeSeconds,
  totalDurationSeconds,
  currentItemDurationSeconds,
  progressPercent,
  onTogglePlay,
  onNext,
}: TimelineTransportBarProps) {
  const safeProgress = Math.max(0, Math.min(100, progressPercent));

  return (
    <div className="rounded-[18px] border border-slate-200 bg-white/95 px-3 py-3 shadow-sm">
      <div className="flex flex-wrap items-center gap-2.5">
        <button
          className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2.5 text-sm font-semibold text-white"
          type="button"
          onClick={onTogglePlay}
          disabled={playlistLength === 0}
        >
          {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {isPlaying ? "Pausar" : "Reproducir"}
        </button>

        <button
          className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:border-accent/40 hover:text-ink"
          type="button"
          onClick={onNext}
          disabled={playlistLength === 0}
        >
          <SkipForward className="h-4 w-4" />
          Siguiente
        </button>

        <div className="rounded-full bg-slate-100 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-600">
          {playbackMode === "random" ? (
            <span className="inline-flex items-center gap-1.5">
              <Shuffle className="h-3.5 w-3.5" />
              Aleatorio
            </span>
          ) : (
            "Secuencia"
          )}
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-3 text-sm text-slate-600">
          <span>
            Clip {playlistLength === 0 ? 0 : currentIndex + 1} / {playlistLength}
          </span>
          <span className="font-semibold text-ink">
            {formatSeconds(currentLoopTimeSeconds)} / {formatSeconds(totalDurationSeconds)}
          </span>
        </div>
      </div>

      <div className="mt-3">
        <div className="h-3 overflow-hidden rounded-full bg-slate-100">
          <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-500 transition-[width]" style={{ width: `${safeProgress}%` }} />
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3 text-[11px] uppercase tracking-[0.14em] text-slate-500">
          <span>Clip actual: {formatSeconds(currentItemDurationSeconds)}</span>
          <span>Loop infinito activo</span>
        </div>
      </div>
    </div>
  );
}
