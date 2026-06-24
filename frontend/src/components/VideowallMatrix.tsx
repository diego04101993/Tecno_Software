import type { Channel, Videowall, VideowallNode } from "../types/domain";

export function VideowallMatrix({
  videowall,
  nodes,
  channels,
}: {
  videowall: Videowall | null;
  nodes: VideowallNode[];
  channels: Channel[];
}) {
  if (!videowall) {
    return (
      <div className="rounded-[28px] border border-dashed border-slate-300 bg-slate-50 p-8 text-center text-sm text-slate-500">
        Selecciona o crea un videowall para ver el preview.
      </div>
    );
  }

  return (
    <div className="rounded-[32px] border border-slate-200 bg-gradient-to-br from-slate-950 via-slate-900 to-slate-800 p-5 text-white shadow-card">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <p className="font-display text-2xl">{videowall.name}</p>
          <p className="text-sm text-slate-300">
            {videowall.columns}x{videowall.rows} · {videowall.total_width}x{videowall.total_height}px
          </p>
        </div>
        <span className="rounded-full bg-white/10 px-4 py-2 text-xs uppercase tracking-[0.2em] text-slate-200">
          {videowall.sync_mode}
        </span>
      </div>
      <div
        className="grid gap-3 rounded-[24px] border border-white/10 bg-black/20 p-3"
        style={{ gridTemplateColumns: `repeat(${videowall.columns}, minmax(0, 1fr))` }}
      >
        {Array.from({ length: videowall.columns * videowall.rows }, (_, index) => {
          const node = nodes.find((item) => item.position_index === index + 1);
          const channel = channels.find((item) => item.id === node?.channel_id);
          return (
            <div
              key={index}
              className="flex min-h-32 flex-col justify-between rounded-[22px] border border-white/10 bg-white/10 p-4"
            >
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-300">Posición {index + 1}</p>
                <p className="mt-3 font-semibold text-white">{channel?.name ?? "Sin asignar"}</p>
              </div>
              <p className="text-xs text-slate-300">
                {node ? `${node.width}x${node.height} · x:${node.x} y:${node.y}` : "Esperando canal"}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
