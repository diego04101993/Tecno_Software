import { ArrowLeft, ArrowRight, ChevronsLeft, ChevronsRight, Plus, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";

import type { PreviewSequenceEntry } from "../../lib/preview";
import { TimelineClip } from "./TimelineClip";
import { TimelinePlayhead } from "./TimelinePlayhead";
import { TimelineRuler } from "./TimelineRuler";

type OperationVideoEditorTimelineProps = {
  entries: PreviewSequenceEntry[];
  selectedItemId: string | null;
  activeItemId: string | null;
  canEdit: boolean;
  totalDurationSeconds: number;
  currentLoopTimeSeconds: number;
  playbackMode: "sequential" | "random";
  onSelectItem: (sequenceItemId: string) => void;
  onSelectAndSeekItem: (sequenceItemId: string, startSeconds: number) => void;
  onMoveItem: (sequenceItemId: string, direction: "left" | "right") => void;
  onDuplicateItem: (sequenceItemId: string) => void;
  onRemoveItem: (entry: PreviewSequenceEntry) => void;
  onUpdateDuration: (sequenceItemId: string, durationSeconds: number) => void;
  onSeek: (seconds: number) => void;
  onRequestAddContent: () => void;
};

type TimelineMetric = {
  entry: PreviewSequenceEntry;
  index: number;
  startSeconds: number;
  durationSeconds: number;
  startPx: number;
  widthPx: number;
  endPx: number;
};

function resolvePixelsPerSecond(totalDurationSeconds: number) {
  if (totalDurationSeconds <= 30) {
    return 34;
  }
  if (totalDurationSeconds <= 60) {
    return 28;
  }
  if (totalDurationSeconds <= 120) {
    return 20;
  }
  return 15;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum);
}

export function OperationVideoEditorTimeline({
  entries,
  selectedItemId,
  activeItemId,
  canEdit,
  totalDurationSeconds,
  currentLoopTimeSeconds,
  playbackMode,
  onSelectItem,
  onSelectAndSeekItem,
  onMoveItem,
  onDuplicateItem,
  onRemoveItem,
  onUpdateDuration,
  onSeek,
  onRequestAddContent,
}: OperationVideoEditorTimelineProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pixelsPerSecond = useMemo(() => resolvePixelsPerSecond(totalDurationSeconds), [totalDurationSeconds]);
  const metrics = useMemo<TimelineMetric[]>(() => {
    let secondCursor = 0;
    let pixelCursor = 0;

    return entries.map((entry, index) => {
      const durationSeconds = Math.max(1, entry.duration_seconds);
      const widthPx = Math.max(160, durationSeconds * pixelsPerSecond);
      const metric = {
        entry,
        index,
        startSeconds: secondCursor,
        durationSeconds,
        startPx: pixelCursor,
        widthPx,
        endPx: pixelCursor + widthPx,
      };
      secondCursor += durationSeconds;
      pixelCursor += widthPx;
      return metric;
    });
  }, [entries, pixelsPerSecond]);

  const sequenceWidthPx = Math.max(metrics[metrics.length - 1]?.endPx ?? 0, 640);

  const resolvePixelForSeconds = (seconds: number) => {
    if (metrics.length === 0) {
      return 0;
    }

    const clampedSeconds = clamp(seconds, 0, Math.max(0, totalDurationSeconds));
    for (let index = 0; index < metrics.length; index += 1) {
      const metric = metrics[index];
      const isLast = index === metrics.length - 1;
      const metricEndSeconds = metric.startSeconds + metric.durationSeconds;
      if (clampedSeconds < metricEndSeconds || isLast) {
        const ratio = metric.durationSeconds <= 0 ? 0 : clamp((clampedSeconds - metric.startSeconds) / metric.durationSeconds, 0, 1);
        return metric.startPx + ratio * metric.widthPx;
      }
    }

    return sequenceWidthPx;
  };

  const resolveSecondsForPixel = (pixel: number) => {
    if (metrics.length === 0) {
      return 0;
    }

    const clampedPixel = clamp(pixel, 0, sequenceWidthPx);
    for (let index = 0; index < metrics.length; index += 1) {
      const metric = metrics[index];
      const isLast = index === metrics.length - 1;
      if (clampedPixel < metric.endPx || isLast) {
        const ratio = metric.widthPx <= 0 ? 0 : clamp((clampedPixel - metric.startPx) / metric.widthPx, 0, 1);
        return metric.startSeconds + ratio * metric.durationSeconds;
      }
    }

    return totalDurationSeconds;
  };

  const playheadLeftPx = resolvePixelForSeconds(currentLoopTimeSeconds);

  function scrollToPosition(left: number, behavior: ScrollBehavior = "smooth") {
    const container = scrollRef.current;
    if (!container) {
      return;
    }

    container.scrollTo({
      left: clamp(left, 0, Math.max(0, container.scrollWidth - container.clientWidth)),
      behavior,
    });
  }

  function ensureRangeVisible(startPx: number, endPx: number, behavior: ScrollBehavior) {
    const container = scrollRef.current;
    if (!container) {
      return;
    }

    const padding = 72;
    const visibleLeft = container.scrollLeft;
    const visibleRight = visibleLeft + container.clientWidth;

    if (startPx - padding < visibleLeft) {
      scrollToPosition(startPx - padding, behavior);
      return;
    }
    if (endPx + padding > visibleRight) {
      scrollToPosition(endPx - container.clientWidth + padding, behavior);
    }
  }

  useEffect(() => {
    if (!scrollRef.current || !activeItemId) {
      return;
    }

    const element = scrollRef.current.querySelector<HTMLElement>(`[data-entry-id="${activeItemId}"]`);
    if (!element) {
      return;
    }

    ensureRangeVisible(element.offsetLeft, element.offsetLeft + element.offsetWidth, "smooth");
  }, [activeItemId]);

  useEffect(() => {
    if (!scrollRef.current) {
      return;
    }

    ensureRangeVisible(playheadLeftPx, playheadLeftPx, "auto");
  }, [playheadLeftPx]);

  if (entries.length === 0) {
    return (
      <div className="grid h-full min-h-[280px] place-items-center rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-6 text-center text-sm text-slate-600">
        <div>
          <span className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-white text-accent shadow-sm">
            <Sparkles className="h-5 w-5" />
          </span>
          <p className="mt-4 font-semibold text-ink">Timeline lista para construir el loop</p>
          <p className="mt-2 max-w-2xl">Agrega contenido desde la biblioteca derecha. Cada item se convierte en un clip proporcional dentro del loop infinito.</p>
          <button
            className="mt-5 inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-xs font-semibold uppercase tracking-[0.14em] text-slate-700 transition hover:border-accent/35 hover:text-ink"
            type="button"
            onClick={onRequestAddContent}
          >
            <Plus className="h-3.5 w-3.5" />
            Agregar desde biblioteca
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-[24px] border border-slate-200 bg-slate-950 shadow-inner">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-4 py-3 text-[11px] uppercase tracking-[0.16em] text-slate-300">
        <div className="space-y-1">
          <p>{playbackMode === "random" ? "Ciclo aleatorio actual" : "Timeline en orden secuencial"}</p>
          <p className="text-[10px] text-slate-500">La timeline tiene scroll horizontal interno y representa el loop real del runtime.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 font-semibold text-slate-200 transition hover:border-cyan-400/40 hover:bg-cyan-500/10"
            type="button"
            onClick={() => scrollToPosition(0)}
          >
            <ChevronsLeft className="h-3.5 w-3.5" />
            Inicio
          </button>
          <button
            className="rounded-full border border-white/10 bg-white/5 p-1.5 text-slate-200 transition hover:border-cyan-400/40 hover:bg-cyan-500/10"
            type="button"
            title="Desplazar timeline a la izquierda"
            onClick={() => scrollToPosition((scrollRef.current?.scrollLeft ?? 0) - Math.max(280, (scrollRef.current?.clientWidth ?? 0) * 0.65))}
          >
            <ArrowLeft className="h-3.5 w-3.5" />
          </button>
          <button
            className="rounded-full border border-white/10 bg-white/5 p-1.5 text-slate-200 transition hover:border-cyan-400/40 hover:bg-cyan-500/10"
            type="button"
            title="Desplazar timeline a la derecha"
            onClick={() => scrollToPosition((scrollRef.current?.scrollLeft ?? 0) + Math.max(280, (scrollRef.current?.clientWidth ?? 0) * 0.65))}
          >
            <ArrowRight className="h-3.5 w-3.5" />
          </button>
          <button
            className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 font-semibold text-slate-200 transition hover:border-cyan-400/40 hover:bg-cyan-500/10"
            type="button"
            onClick={() => scrollToPosition(sequenceWidthPx)}
          >
            Final
            <ChevronsRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden"
        onWheel={(event) => {
          if (!scrollRef.current || scrollRef.current.scrollWidth <= scrollRef.current.clientWidth) {
            return;
          }
          if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
            return;
          }
          event.preventDefault();
          scrollRef.current.scrollLeft += event.deltaY;
        }}
      >
        <div className="relative" style={{ width: `${sequenceWidthPx}px`, minWidth: "100%" }}>
          <TimelineRuler
            totalDurationSeconds={Math.max(1, totalDurationSeconds)}
            timelineWidthPx={sequenceWidthPx}
            resolvePixelForSeconds={resolvePixelForSeconds}
            resolveSecondsForPixel={resolveSecondsForPixel}
            onSeek={onSeek}
          />

          <div
            className="relative flex h-[220px] items-stretch border-b border-white/10 bg-[linear-gradient(180deg,rgba(15,23,42,0.96)_0%,rgba(2,6,23,0.96)_100%)]"
            style={{ width: `${sequenceWidthPx}px` }}
          >
            {metrics.map((metric) => (
              <TimelineClip
                key={metric.entry.id}
                entry={metric.entry}
                index={metric.index}
                clipWidthPx={metric.widthPx}
                startSeconds={metric.startSeconds}
                isSelected={metric.entry.id === selectedItemId}
                isActive={metric.entry.id === activeItemId}
                canEdit={canEdit}
                isFirst={metric.index === 0}
                isLast={metric.index === metrics.length - 1}
                onSelect={(entryId, startSeconds) => {
                  onSelectItem(entryId);
                  onSelectAndSeekItem(entryId, startSeconds);
                }}
                onMove={onMoveItem}
                onDuplicate={onDuplicateItem}
                onRemove={onRemoveItem}
                onUpdateDuration={onUpdateDuration}
              />
            ))}

            <TimelinePlayhead heightPx={220} leftPx={playheadLeftPx} />
          </div>

          <div className="flex items-center justify-between gap-3 border-t border-white/10 bg-slate-950/90 px-4 py-3">
            <div className="flex flex-wrap items-center gap-3 text-[11px] uppercase tracking-[0.16em] text-slate-400">
              <span>{entries.length} clip(s) en el loop</span>
              <span>{Math.max(1, totalDurationSeconds)}s totales</span>
            </div>
            <button
              className="inline-flex items-center gap-2 rounded-[14px] border border-dashed border-white/15 bg-white/5 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-300 transition hover:border-cyan-400/40 hover:bg-cyan-500/10 hover:text-white"
              type="button"
              onClick={onRequestAddContent}
            >
              <Plus className="h-3.5 w-3.5" />
              Agregar clip
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
