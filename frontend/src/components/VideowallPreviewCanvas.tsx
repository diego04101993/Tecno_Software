import type { PreviewPlaylistEntry, PreviewVideowallState } from "../lib/preview";
import { PreviewContentSurface } from "./PreviewContentSurface";

export function VideowallPreviewCanvas({
  videowall,
  currentEntry,
  channelName,
}: {
  videowall: PreviewVideowallState;
  currentEntry: PreviewPlaylistEntry | null;
  channelName: string;
}) {
  if (!videowall.wall || !videowall.segment) {
    return (
      <div className="rounded-[28px] border border-slate-200 bg-slate-50 px-5 py-5 text-sm text-slate-600">
        Este canal no tiene un segmento de videowall asociado para previsualizar.
      </div>
    );
  }

  const totalWidth = Math.max(1, videowall.wall.total_width);
  const totalHeight = Math.max(1, videowall.wall.total_height);
  const segment = videowall.segment;

  return (
    <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
      <div className="overflow-hidden rounded-[28px] border border-slate-200 bg-slate-950 p-4 shadow-card">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-slate-400">Preview completo del videowall</p>
            <p className="mt-2 text-sm text-slate-300">
              El lienzo total muestra donde cae este canal dentro del muro sincronizado.
            </p>
          </div>
          <span className="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs font-semibold text-slate-200">
            {videowall.wall.columns}x{videowall.wall.rows}
          </span>
        </div>

        <div
          className="relative overflow-hidden rounded-[24px] border border-white/10 bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.12),transparent_36%),linear-gradient(160deg,#0f172a,#111827)]"
          style={{ aspectRatio: `${totalWidth} / ${totalHeight}` }}
        >
          <div
            className="absolute inset-0 grid"
            style={{ gridTemplateColumns: `repeat(${videowall.wall.columns}, minmax(0, 1fr))` }}
          >
            {Array.from({ length: videowall.wall.columns * videowall.wall.rows }, (_, index) => {
              const position = index + 1;
              const node = videowall.nodes.find((item) => item.position_index === position) ?? null;
              const isCurrent = node?.channel_id === segment.channel_id && position === segment.position_index;

              return (
                <div
                  key={position}
                  className={[
                    "relative border border-white/10",
                    isCurrent ? "bg-cyan-400/18" : node ? "bg-white/8" : "bg-black/15",
                  ].join(" ")}
                >
                  <span className="absolute left-3 top-3 rounded-full border border-white/10 bg-black/35 px-3 py-1 text-[10px] uppercase tracking-[0.18em] text-white/80">
                    Monitor {position}
                  </span>
                  {isCurrent ? (
                    <div className="absolute inset-4 overflow-hidden rounded-[18px] border border-cyan-200/40 bg-white/10 p-2">
                      <PreviewContentSurface entry={currentEntry} compact title={channelName} />
                    </div>
                  ) : node ? (
                    <div className="absolute inset-x-3 bottom-3 rounded-[16px] border border-white/10 bg-black/25 px-3 py-2 text-[11px] text-slate-200">
                      Segmento ocupado
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-[28px] border border-slate-200 bg-white p-4 shadow-card">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Preview por monitor</p>
            <p className="mt-2 text-sm text-slate-600">Segmento individual que veria este canal dentro del videowall.</p>
          </div>
          <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-600">
            Posicion {segment.position_index}
          </span>
        </div>

        <div className="space-y-4">
          <div
            className="overflow-hidden rounded-[24px] border border-slate-200 bg-slate-950 p-3"
            style={{ aspectRatio: `${Math.max(1, segment.width)} / ${Math.max(1, segment.height)}` }}
          >
            <PreviewContentSurface entry={currentEntry} title={channelName} />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
              x:{segment.x} - y:{segment.y}
            </div>
            <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
              {segment.width}x{segment.height}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
