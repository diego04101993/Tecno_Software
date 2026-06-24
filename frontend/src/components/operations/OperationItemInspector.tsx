import { Clock3, Clapperboard, Repeat, Shuffle, Trash2 } from "lucide-react";

import { formatSeconds, getPreviewEntryDurationModeLabel, isPreviewEntryDurationEditable, type PreviewSequenceEntry } from "../../lib/preview";
import type { Campaign } from "../../types/domain";
import { OperationPlaybackModeBar } from "./OperationPlaybackModeBar";

type OperationItemInspectorProps = {
  campaign: Campaign | null;
  selectedEntry: PreviewSequenceEntry | null;
  playbackMode: "sequential" | "random";
  totalItems: number;
  totalDurationSeconds: number;
  canEdit: boolean;
  onChangePlaybackMode: (mode: "sequential" | "random") => void;
  onUpdateDuration: (sequenceItemId: string, durationSeconds: number) => void;
  onRemoveSelectedItem: (entry: PreviewSequenceEntry) => void;
};

function getItemTypeLabel(entry: PreviewSequenceEntry | null) {
  switch (entry?.itemCode) {
    case "LAY":
      return "Layout";
    case "IMG":
      return "Imagen";
    case "VID":
      return "Video";
    case "URL":
      return "URL";
    case "HTML":
      return "HTML";
    case "STR":
      return "Stream";
    case "TXT":
      return "Texto";
    default:
      return "Sin clip seleccionado";
  }
}

export function OperationItemInspector({
  campaign,
  selectedEntry,
  playbackMode,
  totalItems,
  totalDurationSeconds,
  canEdit,
  onChangePlaybackMode,
  onUpdateDuration,
  onRemoveSelectedItem,
}: OperationItemInspectorProps) {
  const durationEditable = isPreviewEntryDurationEditable(selectedEntry);
  const durationModeLabel = getPreviewEntryDurationModeLabel(selectedEntry);

  return (
    <div className="grid min-h-0 min-w-0 gap-3 px-4 py-3 xl:grid-cols-[minmax(0,1.08fr)_minmax(320px,370px)]">
      <section className="rounded-[20px] border border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Configuración de campaña</p>
            <p className="mt-1 truncate text-base font-semibold text-ink" title={campaign?.name ?? "Sin campaña activa"}>
              {campaign?.name ?? "Sin campaña activa"}
            </p>
            <p className="mt-1 text-[11px] leading-4 text-slate-600">Control rápido del modo de reproducción y resumen del loop actual.</p>
          </div>
          <span className="inline-flex items-center gap-2 rounded-full bg-emerald-50 px-3 py-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-700">
            <Repeat className="h-3.5 w-3.5" />
            Loop infinito
          </span>
        </div>

        <div className="mt-3">
          <OperationPlaybackModeBar mode={playbackMode} canEdit={canEdit} onChange={onChangePlaybackMode} />
        </div>

        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-2.5">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Modo</p>
            <p className="mt-2 inline-flex items-center gap-2 text-xs font-semibold text-ink">
              {playbackMode === "random" ? <Shuffle className="h-4 w-4 text-slate-500" /> : <Clapperboard className="h-4 w-4 text-slate-500" />}
              {playbackMode === "random" ? "Aleatorio" : "Secuencia"}
            </p>
          </div>
          <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-2.5">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Clips</p>
            <p className="mt-2 text-xs font-semibold text-ink">{totalItems}</p>
          </div>
          <div className="rounded-[16px] border border-slate-200 bg-white px-3 py-2.5">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Duración total</p>
            <p className="mt-2 text-xs font-semibold text-ink">{formatSeconds(totalDurationSeconds)}</p>
          </div>
        </div>
      </section>

      <section className="rounded-[20px] border border-slate-200 bg-white px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.22em] text-slate-500">Clip seleccionado</p>
            <p className="mt-1 truncate text-base font-semibold text-ink" title={selectedEntry?.itemLabel ?? "Sin clip seleccionado"}>
              {selectedEntry?.itemLabel ?? "Sin clip seleccionado"}
            </p>
            <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-slate-600">{selectedEntry?.contentLabel ?? "Selecciona un clip del timeline para editarlo."}</p>
          </div>
          {selectedEntry && canEdit ? (
            <button
              className="inline-flex items-center gap-2 rounded-full border border-rose-200 px-2.5 py-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-rose-600 transition hover:bg-rose-50"
              type="button"
              onClick={() => onRemoveSelectedItem(selectedEntry)}
            >
              <Trash2 className="h-3.5 w-3.5" />
              Eliminar clip
            </button>
          ) : null}
        </div>

        <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(88px,0.9fr)_minmax(98px,112px)_minmax(0,1fr)]">
          <div className="rounded-[16px] border border-slate-200 bg-slate-50 px-2.5 py-2.5">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Tipo</p>
            <p className="mt-2 text-[11px] font-semibold text-ink">{getItemTypeLabel(selectedEntry)}</p>
          </div>
          <div className="rounded-[16px] border border-slate-200 bg-slate-50 px-2.5 py-2.5">
            <p className="text-[10px] uppercase tracking-[0.16em] text-slate-500">Duracion</p>
            {selectedEntry ? (
              <div className="mt-2 flex min-w-0 items-center gap-1.5">
                <Clock3 className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                {durationEditable ? (
                  <input
                    key={`${selectedEntry.id}:${selectedEntry.duration_seconds}`}
                    className="h-8 w-[62px] min-w-0 rounded-full border border-slate-200 bg-white px-2 text-center text-[12px] font-semibold text-ink outline-none transition focus:border-cyan-300 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                    defaultValue={selectedEntry.duration_seconds}
                    disabled={!canEdit}
                    min={1}
                    type="number"
                    onBlur={(event) => onUpdateDuration(selectedEntry.id, Number(event.target.value))}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.currentTarget.blur();
                      }
                    }}
                  />
                ) : (
                  <span className="inline-flex h-8 min-w-[72px] items-center justify-center rounded-full border border-slate-200 bg-white px-2 text-[11px] font-semibold text-slate-600">
                    {selectedEntry.itemCode === "STR" ? "Continuo" : selectedEntry.duration_seconds}s
                  </span>
                )}
              </div>
            ) : (
              <p className="mt-2 text-[11px] leading-4 text-slate-500">Selecciona un clip para editar su duración.</p>
            )}
            {selectedEntry ? <p className="mt-2 text-[10px] leading-4 text-slate-500">{durationModeLabel}</p> : null}
          </div>
          <div className="rounded-[16px] border border-slate-200 bg-slate-50 px-2.5 py-2.5">
            <p className="text-[11px] uppercase tracking-[0.16em] text-slate-500">Estado</p>
            <p className="mt-2 text-[10px] leading-4 font-semibold text-ink">{selectedEntry ? "Listo para loop y preview." : "Sin seleccion activa."}</p>
          </div>
        </div>
      </section>
    </div>
  );
}
