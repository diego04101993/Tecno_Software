import { SectionCard } from "../SectionCard";
import type { AudioReportEntry, AudioRecentEvent } from "../../types/domain";

export function AudioReportTable({
  title,
  subtitle,
  music,
  spots,
  recentEvents,
}: {
  title: string;
  subtitle: string;
  music: AudioReportEntry[];
  spots: AudioReportEntry[];
  recentEvents: AudioRecentEvent[];
}) {
  return (
    <SectionCard title={title} subtitle={subtitle}>
      <div className="grid gap-5 xl:grid-cols-2">
        <div className="space-y-3">
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Música</p>
          {music.length > 0 ? (
            music.map((item) => (
              <article key={`${item.content_id}-${item.playlist_id}`} className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                <p className="font-semibold text-ink">{item.content_name}</p>
                <p className="mt-1 text-sm text-slate-500">{item.play_count} reproduccion(es)</p>
                <p className="mt-1 text-xs text-slate-500">{item.last_played_at ?? "Sin fecha registrada"}</p>
              </article>
            ))
          ) : (
            <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
              Aún no hay reproducciones de música reportadas.
            </div>
          )}
        </div>
        <div className="space-y-3">
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Spots</p>
          {spots.length > 0 ? (
            spots.map((item) => (
              <article key={`${item.content_id}-${item.playlist_id}`} className="rounded-[22px] border border-slate-200 bg-white px-4 py-4">
                <p className="font-semibold text-ink">{item.content_name}</p>
                <p className="mt-1 text-sm text-slate-500">{item.play_count} reproduccion(es)</p>
                <p className="mt-1 text-xs text-slate-500">{item.last_played_at ?? "Sin fecha registrada"}</p>
              </article>
            ))
          ) : (
            <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
              Aún no hay reproducciones de spots reportadas.
            </div>
          )}
        </div>
      </div>

      <div className="mt-5 space-y-3">
        <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Eventos recientes</p>
        {recentEvents.length > 0 ? (
          recentEvents.map((event) => (
            <article key={event.id} className="rounded-[22px] border border-slate-200 bg-slate-50 px-4 py-4">
              <p className="font-semibold text-ink">{event.content_name}</p>
              <p className="mt-1 text-sm text-slate-600">
                {event.entry_kind === "music" ? "Música" : "Spot"} - {event.playlist_name ?? "Sin playlist"}
              </p>
              <p className="mt-1 text-xs text-slate-500">{event.played_at}</p>
            </article>
          ))
        ) : (
          <div className="rounded-[22px] border border-dashed border-slate-300 bg-slate-50 px-4 py-5 text-sm text-slate-600">
            El reporte quedara poblado cuando el media player comience a enviar eventos de audio.
          </div>
        )}
      </div>
    </SectionCard>
  );
}
