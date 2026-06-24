import { Pause, Play, SkipForward } from "lucide-react";

import { formatSeconds } from "../lib/preview";

export function PreviewControls({
  isPlaying,
  onTogglePlay,
  onNext,
  currentIndex,
  playlistLength,
  progressPercent,
  elapsedSeconds,
  itemDurationSeconds,
  totalDurationSeconds,
}: {
  isPlaying: boolean;
  onTogglePlay: () => void;
  onNext: () => void;
  currentIndex: number;
  playlistLength: number;
  progressPercent: number;
  elapsedSeconds: number;
  itemDurationSeconds: number;
  totalDurationSeconds: number;
}) {
  const safeProgress = Math.max(0, Math.min(100, progressPercent));

  return (
    <div className="rounded-[28px] border border-slate-200 bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-3">
        <button
          className="inline-flex items-center gap-2 rounded-full bg-ink px-4 py-3 text-sm font-semibold text-white"
          type="button"
          onClick={onTogglePlay}
          disabled={playlistLength === 0}
        >
          {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {isPlaying ? "Pausar" : "Reproducir"}
        </button>
        <button
          className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700 transition hover:border-accent/40 hover:text-ink"
          type="button"
          onClick={onNext}
          disabled={playlistLength === 0}
        >
          <SkipForward className="h-4 w-4" />
          Siguiente
        </button>
        <div className="ml-auto text-sm text-slate-600">
          Elemento {playlistLength === 0 ? 0 : currentIndex + 1} de {playlistLength}
        </div>
      </div>

      <div className="mt-4">
        <div className="h-3 overflow-hidden rounded-full bg-slate-100">
          <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-500 transition-[width]" style={{ width: `${safeProgress}%` }} />
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-slate-600">
          <span>
            {formatSeconds(elapsedSeconds)} / {formatSeconds(itemDurationSeconds)}
          </span>
          <span>Loop total: {formatSeconds(totalDurationSeconds)}</span>
        </div>
      </div>
    </div>
  );
}
