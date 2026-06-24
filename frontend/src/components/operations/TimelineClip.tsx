import {
  ArrowLeft,
  ArrowRight,
  Clock3,
  CopyPlus,
  FileCode2,
  Globe,
  GripHorizontal,
  ImageIcon,
  LayoutTemplate,
  Trash2,
  Type,
  Video,
} from "lucide-react";

import { getPreviewEntryDurationModeLabel, isPreviewEntryDurationEditable, resolvePreviewMediaUrl, type PreviewSequenceEntry } from "../../lib/preview";

type TimelineClipProps = {
  entry: PreviewSequenceEntry;
  index: number;
  clipWidthPx: number;
  startSeconds: number;
  isSelected: boolean;
  isActive: boolean;
  canEdit: boolean;
  onSelect: (entryId: string, startSeconds: number) => void;
  onMove: (entryId: string, direction: "left" | "right") => void;
  onDuplicate: (entryId: string) => void;
  onRemove: (entry: PreviewSequenceEntry) => void;
  onUpdateDuration: (entryId: string, durationSeconds: number) => void;
  isFirst: boolean;
  isLast: boolean;
};

function getClipAccentClass(itemCode: string) {
  switch (itemCode) {
    case "IMG":
      return "bg-sky-400";
    case "VID":
      return "bg-emerald-400";
    case "LAY":
      return "bg-violet-400";
    case "URL":
      return "bg-orange-400";
    case "HTML":
      return "bg-amber-300";
    case "STR":
      return "bg-rose-400";
    case "TXT":
      return "bg-slate-300";
    default:
      return "bg-slate-400";
  }
}

function renderFallbackPreview(entry: PreviewSequenceEntry) {
  const contentType = entry.content?.type ?? (entry.item_type === "layout" ? "layout" : "unknown");
  const iconByType =
    contentType === "video"
      ? Video
      : contentType === "url"
        ? Globe
        : contentType === "html"
          ? FileCode2
          : contentType === "text"
            ? Type
            : contentType === "layout"
              ? LayoutTemplate
              : ImageIcon;
  const Icon = iconByType;

  return (
    <div className="grid h-full w-full place-items-center bg-[radial-gradient(circle_at_top,rgba(56,189,248,0.2),transparent_35%),linear-gradient(180deg,#111827_0%,#0f172a_100%)] text-white/85">
      <div className="text-center">
        <span className="mx-auto grid h-8 w-8 place-items-center rounded-xl border border-white/10 bg-white/5">
          <Icon className="h-4 w-4" />
        </span>
        <p className="mt-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-white/70">{entry.itemCode}</p>
      </div>
    </div>
  );
}

function TimelineClipPreview({ entry }: { entry: PreviewSequenceEntry }) {
  const mediaUrl = resolvePreviewMediaUrl(entry.content);
  const contentType = entry.content?.type ?? (entry.item_type === "layout" ? "layout" : "unknown");

  if (contentType === "image" && mediaUrl) {
    return <img alt={entry.itemLabel} className="h-full w-full object-cover" draggable={false} loading="eager" src={mediaUrl} />;
  }
  if (contentType === "video" && mediaUrl) {
    return <video className="h-full w-full object-cover" muted playsInline preload="metadata" src={mediaUrl} />;
  }

  return renderFallbackPreview(entry);
}

export function TimelineClip({
  entry,
  index,
  clipWidthPx,
  startSeconds,
  isSelected,
  isActive,
  canEdit,
  onSelect,
  onMove,
  onDuplicate,
  onRemove,
  onUpdateDuration,
  isFirst,
  isLast,
}: TimelineClipProps) {
  const durationEditable = isPreviewEntryDurationEditable(entry);
  const durationModeLabel = getPreviewEntryDurationModeLabel(entry);

  return (
    <article
      className={[
        "group relative flex h-[220px] shrink-0 cursor-pointer flex-col overflow-hidden border-r border-slate-950/40 px-3 py-3 transition",
        isActive ? "bg-cyan-500/18 shadow-[inset_0_0_0_2px_rgba(34,211,238,0.65)]" : "bg-slate-900/80 hover:bg-slate-900",
        isSelected ? "shadow-[inset_0_0_0_2px_rgba(255,255,255,0.45)]" : "",
      ].join(" ")}
      style={{ width: `${clipWidthPx}px` }}
      data-entry-id={entry.id}
      onClick={() => onSelect(entry.id, startSeconds)}
    >
      <div className="absolute inset-x-0 top-0 h-1 bg-white/10">
        <div className={`h-full ${getClipAccentClass(entry.itemCode)}`} />
      </div>

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <GripHorizontal className="h-3.5 w-3.5 shrink-0 text-white/35" />
            <span className="rounded-full border border-white/15 bg-white/10 px-2 py-1 text-[10px] font-bold uppercase tracking-[0.18em] text-white/90">
              {entry.itemCode}
            </span>
            <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-white/55">#{index + 1}</span>
          </div>
          <p className="mt-2 truncate text-sm font-semibold text-white" title={entry.itemLabel}>
            {entry.itemLabel}
          </p>
        </div>

        {canEdit ? (
          <div className="flex items-center gap-1 opacity-0 transition group-hover:opacity-100" onClick={(event) => event.stopPropagation()}>
            <button
              className="rounded-full border border-white/10 bg-white/10 p-1.5 text-white/80 transition hover:border-white/20 hover:bg-white/15 disabled:cursor-not-allowed disabled:opacity-30"
              disabled={isFirst}
              type="button"
              title="Mover a la izquierda"
              onClick={() => onMove(entry.id, "left")}
            >
              <ArrowLeft className="h-3 w-3" />
            </button>
            <button
              className="rounded-full border border-white/10 bg-white/10 p-1.5 text-white/80 transition hover:border-white/20 hover:bg-white/15 disabled:cursor-not-allowed disabled:opacity-30"
              disabled={isLast}
              type="button"
              title="Mover a la derecha"
              onClick={() => onMove(entry.id, "right")}
            >
              <ArrowRight className="h-3 w-3" />
            </button>
            <button
              className="rounded-full border border-white/10 bg-white/10 p-1.5 text-white/80 transition hover:border-white/20 hover:bg-white/15"
              type="button"
              title="Duplicar clip"
              onClick={() => onDuplicate(entry.id)}
            >
              <CopyPlus className="h-3 w-3" />
            </button>
            <button
              className="rounded-full border border-white/10 bg-white/10 p-1.5 text-white/80 transition hover:border-rose-300/40 hover:bg-rose-500/20 hover:text-white"
              type="button"
              title="Eliminar clip"
              onClick={() => onRemove(entry)}
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        ) : null}
      </div>

      <div className="mt-3 grid min-h-0 flex-1 grid-rows-[104px_auto] gap-2">
        <div className="overflow-hidden rounded-[16px] border border-white/10 bg-black/30">
          <TimelineClipPreview entry={entry} />
        </div>

        <div className="rounded-[16px] border border-white/10 bg-black/20 px-2.5 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-[10px] uppercase tracking-[0.16em] text-white/60">{entry.content?.type ?? entry.item_type}</span>
            <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-white/80">{entry.durationLabel}</span>
          </div>
          <div className="mt-2 flex items-center gap-1.5" onClick={(event) => event.stopPropagation()}>
            <Clock3 className="h-3 w-3 text-white/45" />
            {durationEditable ? (
              <>
                <input
                  key={`${entry.id}:${entry.duration_seconds}`}
                  className="h-auto min-w-0 flex-1 border-none bg-transparent px-0 py-0 text-xs font-semibold text-white shadow-none focus:ring-0"
                  defaultValue={entry.duration_seconds}
                  disabled={!canEdit}
                  min={1}
                  type="number"
                  onBlur={(event) => onUpdateDuration(entry.id, Number(event.target.value))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.currentTarget.blur();
                    }
                  }}
                />
                <span className="text-[10px] uppercase tracking-[0.16em] text-white/45">seg</span>
              </>
            ) : (
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-white/70">
                {entry.itemCode === "STR" ? "Continuo" : entry.itemCode === "VID" ? "Auto" : durationModeLabel}
              </span>
            )}
          </div>
        </div>
      </div>
    </article>
  );
}
