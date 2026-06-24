import type { Channel, Layout } from "../types/domain";
import type { PreviewPlaylistEntry, PreviewZone } from "../lib/preview";
import { LayoutPreviewCanvas } from "./LayoutPreviewCanvas";
import { PreviewContentSurface } from "./PreviewContentSurface";

export function PreviewCanvas({
  channel,
  layout,
  zones,
  entries,
  currentIndex,
}: {
  channel: Channel;
  layout: Layout | null;
  zones: PreviewZone[];
  entries: PreviewPlaylistEntry[];
  currentIndex: number;
}) {
  const currentEntry = entries[currentIndex] ?? null;

  if (layout) {
    return <LayoutPreviewCanvas channel={channel} zones={zones} entries={entries} currentIndex={currentIndex} />;
  }

  return (
    <div className="overflow-hidden rounded-[28px] border border-slate-200 bg-slate-950 p-4 shadow-card">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.24em] text-slate-400">Preview del canal</p>
          <p className="mt-2 text-sm text-slate-300">Simulacion visual segun la resolucion, orientacion y modo del canal.</p>
        </div>
        <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs font-semibold text-slate-200">
          {channel.resolution_width}x{channel.resolution_height}
        </span>
      </div>

      <div
        className="relative overflow-hidden rounded-[24px] border border-white/10 bg-slate-900"
        style={{ aspectRatio: `${Math.max(1, channel.resolution_width)} / ${Math.max(1, channel.resolution_height)}` }}
      >
        <div className="absolute inset-0 p-4">
          <PreviewContentSurface entry={currentEntry} title={currentEntry?.content?.name ?? channel.name} />
        </div>

        {channel.mode === "expanded" ? (
          <div
            className="pointer-events-none absolute inset-0 grid"
            style={{ gridTemplateColumns: `repeat(${Math.max(1, channel.screen_count)}, minmax(0, 1fr))` }}
          >
            {Array.from({ length: Math.max(1, channel.screen_count) }, (_, index) => (
              <div key={index} className="relative border-r border-white/20 last:border-r-0">
                <span className="absolute left-3 top-3 rounded-full border border-white/15 bg-black/35 px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-white/90">
                  HDMI {index + 1}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
