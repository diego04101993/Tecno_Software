import { formatChannelMode } from "../lib/labels";
import type { PreviewPlaylistEntry } from "../lib/preview";

export function PreviewPlaylistPanel({
  entries,
  currentIndex,
  onSelectIndex,
  campaignName,
  scheduleTitle,
  layoutName,
  channelMode,
}: {
  entries: PreviewPlaylistEntry[];
  currentIndex: number;
  onSelectIndex: (index: number) => void;
  campaignName: string | null;
  scheduleTitle: string | null;
  layoutName: string | null;
  channelMode: string;
}) {
  return (
    <aside className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-card">
      <div>
        <p className="text-xs uppercase tracking-[0.24em] text-slate-500">Playlist y contexto</p>
        <p className="mt-3 text-lg font-semibold text-ink">{campaignName ?? "Sin campaña visible"}</p>
        <p className="mt-2 text-sm text-slate-600">
          {scheduleTitle ?? "Sin timeline activo"} - {layoutName ?? "Sin layout"} - {formatChannelMode(channelMode)}
        </p>
      </div>

      <div className="mt-5 space-y-3">
        {entries.length > 0 ? (
          entries.map((entry, index) => (
            <button
              key={entry.id}
              className={[
                "w-full rounded-[22px] border px-4 py-4 text-left transition",
                index === currentIndex
                  ? "border-cyan-300 bg-cyan-50 shadow-[0_0_0_1px_rgba(6,182,212,0.18)]"
                  : "border-slate-200 bg-slate-50 hover:border-slate-300 hover:bg-white",
              ].join(" ")}
              type="button"
              onClick={() => onSelectIndex(index)}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-semibold text-ink" title={entry.content?.name ?? "Contenido"}>
                    {entry.content?.name ?? "Contenido"}
                  </p>
                  <p className="mt-1 truncate text-sm text-slate-500" title={entry.contentLabel}>
                    {entry.contentLabel}
                  </p>
                </div>
                <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-600">
                  {entry.durationLabel}
                </span>
              </div>
              <p className="mt-3 text-xs uppercase tracking-[0.18em] text-slate-500">
                Orden {entry.sort_order} - Zona {entry.zone_key}
              </p>
            </button>
          ))
        ) : (
          <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
            No hay playlist disponible para simular este canal por ahora.
          </div>
        )}
      </div>
    </aside>
  );
}
