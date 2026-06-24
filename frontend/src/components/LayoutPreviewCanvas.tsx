import type { Channel } from "../types/domain";
import type { PreviewRenderableEntry, PreviewZone } from "../lib/preview";
import { getZonePreviewEntry } from "../lib/preview";
import { PreviewContentSurface } from "./PreviewContentSurface";

type LayoutPreviewCanvasProps = {
  channel: Channel;
  zones: PreviewZone[];
  entries: PreviewRenderableEntry[];
  currentIndex: number;
  compact?: boolean;
};

export function LayoutPreviewCanvas({
  channel,
  zones,
  entries,
  currentIndex,
  compact = false,
}: LayoutPreviewCanvasProps) {
  const canvasWidth = Math.max(1, zones.reduce((max, zone) => Math.max(max, zone.x + zone.width), channel.resolution_width));
  const canvasHeight = Math.max(1, zones.reduce((max, zone) => Math.max(max, zone.y + zone.height), channel.resolution_height));

  return (
    <div
      className={[
        "overflow-hidden border border-slate-200 bg-slate-950 shadow-card",
        compact ? "rounded-[24px] p-3" : "rounded-[28px] p-4",
      ].join(" ")}
    >
      <div className={compact ? "mb-3 flex items-center justify-between gap-3" : "mb-4 flex items-center justify-between"}>
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-[0.24em] text-slate-400">{compact ? "Canvas de layout" : "Preview de layout"}</p>
          <p className={compact ? "mt-1 truncate text-xs text-slate-300" : "mt-2 text-sm text-slate-300"}>
            {compact ? "Las regiones se alinean dentro del canvas activo." : "Las regiones se simulan dentro del canvas configurado para la campana."}
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs font-semibold text-slate-200">
          {canvasWidth}x{canvasHeight}
        </span>
      </div>

      <div
        className="relative overflow-hidden rounded-[20px] border border-white/10 bg-slate-900"
        style={{ aspectRatio: `${canvasWidth} / ${canvasHeight}` }}
      >
        <div className="absolute inset-0 bg-[linear-gradient(135deg,rgba(255,255,255,0.08),transparent_48%),radial-gradient(circle_at_top,rgba(255,255,255,0.08),transparent_36%)]" />
        {zones.map((zone) => {
          const entry = getZonePreviewEntry({
            entries,
            currentIndex,
            zoneKey: zone.key,
          });

          return (
            <div
              key={zone.key}
              className="absolute overflow-hidden rounded-[16px] border border-white/15 bg-white/5 shadow-lg"
              style={{
                left: `${(zone.x / canvasWidth) * 100}%`,
                top: `${(zone.y / canvasHeight) * 100}%`,
                width: `${(zone.width / canvasWidth) * 100}%`,
                height: `${(zone.height / canvasHeight) * 100}%`,
              }}
            >
              <div className="flex items-center justify-between border-b border-white/10 bg-black/25 px-3 py-2 text-[10px] uppercase tracking-[0.18em] text-slate-200">
                <span>{zone.label}</span>
                <span>{zone.key}</span>
              </div>
              <div className={compact ? "h-[calc(100%-33px)] p-2.5" : "h-[calc(100%-37px)] p-3"}>
                <PreviewContentSurface entry={entry} compact title={entry?.content?.name ?? zone.label} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
