import { ArrowRightLeft, CalendarClock, Cpu, PencilLine, PlayCircle, Trash2 } from "lucide-react";
import type { ReactNode } from "react";

import { formatChannelMode, formatHeartbeatAge } from "../../lib/labels";
import type { ChannelStatus } from "../../types/domain";
import { StatusBadge } from "../StatusBadge";

function ScreenActionButton({
  danger = false,
  onClick,
  children,
}: {
  danger?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      className={[
        "inline-flex items-center gap-2 rounded-full border px-3 py-2 text-xs font-semibold transition",
        danger
          ? "border-rose-200 text-rose-700 hover:border-rose-300 hover:bg-rose-50"
          : "border-slate-200 text-slate-700 hover:border-cyan-300 hover:text-ink",
      ].join(" ")}
      type="button"
      onClick={onClick}
    >
      {children}
    </button>
  );
}

export function ScreenTile({
  title,
  status,
  mode,
  resolutionLabel,
  channelCode,
  campaignLabel,
  playbackLabel,
  hardwareLabel,
  heartbeatAgeSeconds,
  lastHeartbeatAt,
  eyebrow,
  hasAssignedCampaign,
  scheduleCount,
  scheduleSummary,
  onPublishCampaign,
  onManageSchedules,
  onEdit,
  onDelete,
}: {
  title: string;
  status: ChannelStatus;
  mode: string;
  resolutionLabel: string;
  channelCode: string;
  campaignLabel: string;
  playbackLabel?: string | null;
  hardwareLabel?: string | null;
  heartbeatAgeSeconds?: number | null;
  lastHeartbeatAt?: string | null;
  eyebrow?: string;
  hasAssignedCampaign?: boolean;
  scheduleCount?: number;
  scheduleSummary?: string;
  onPublishCampaign?: () => void;
  onManageSchedules?: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const lastCommunicationLabel =
    heartbeatAgeSeconds !== null && heartbeatAgeSeconds !== undefined
      ? formatHeartbeatAge(heartbeatAgeSeconds)
      : lastHeartbeatAt
        ? formatHeartbeatAge(Math.max(0, Math.floor((Date.now() - new Date(lastHeartbeatAt).getTime()) / 1000)))
        : "Sin registro";

  return (
    <article className="rounded-[24px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          {eyebrow ? <p className="text-[11px] uppercase tracking-[0.22em] text-slate-400">{eyebrow}</p> : null}
          <p className="mt-1 truncate font-semibold text-ink" title={title}>
            {title}
          </p>
          <p className="mt-1 text-sm text-slate-500">
            {formatChannelMode(mode)} · {resolutionLabel}
          </p>
        </div>
        <StatusBadge status={status} />
      </div>

      {onPublishCampaign || onManageSchedules || onEdit || onDelete ? (
        <div className="mt-4 flex flex-wrap gap-2">
          {onPublishCampaign ? (
            <ScreenActionButton onClick={onPublishCampaign}>
              <ArrowRightLeft className="h-3.5 w-3.5" />
              {hasAssignedCampaign ? "Cambiar campaña" : "Publicar campaña"}
            </ScreenActionButton>
          ) : null}
          {onManageSchedules ? (
            <ScreenActionButton onClick={onManageSchedules}>
              <CalendarClock className="h-3.5 w-3.5" />
              Programaciones
            </ScreenActionButton>
          ) : null}
          {onEdit ? (
            <ScreenActionButton onClick={onEdit}>
              <PencilLine className="h-3.5 w-3.5" />
              Editar
            </ScreenActionButton>
          ) : null}
          {onDelete ? (
            <ScreenActionButton danger onClick={onDelete}>
              <Trash2 className="h-3.5 w-3.5" />
              Eliminar
            </ScreenActionButton>
          ) : null}
        </div>
      ) : null}

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Canal / player</p>
          <p className="mt-2 break-all text-sm font-semibold text-ink">{channelCode}</p>
        </div>
        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Campaña visible</p>
          <p className="mt-2 text-sm font-semibold text-ink">{campaignLabel}</p>
          <p className="mt-1 text-xs text-slate-500">{hasAssignedCampaign ? "Campaña activa en pantalla" : "Sin campaña publicada"}</p>
        </div>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex items-center gap-2 text-slate-500">
            <PlayCircle className="h-4 w-4" />
            <span className="text-[11px] uppercase tracking-[0.2em]">Playback</span>
          </div>
          <p className="mt-2 line-clamp-2 text-sm text-slate-700">{playbackLabel ?? "Sin reproducción reportada"}</p>
        </div>
        <div className="rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex items-center gap-2 text-slate-500">
            <Cpu className="h-4 w-4" />
            <span className="text-[11px] uppercase tracking-[0.2em]">Hardware</span>
          </div>
          <p className="mt-2 line-clamp-2 break-all text-sm text-slate-700">{hardwareLabel ?? "Pendiente"}</p>
        </div>
      </div>

      <div className="mt-3 rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
        <div className="flex items-center justify-between gap-2 text-slate-500">
          <div className="flex items-center gap-2">
            <CalendarClock className="h-4 w-4" />
            <span className="text-[11px] uppercase tracking-[0.2em]">Programaciones</span>
          </div>
          <span className="rounded-full bg-white px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-700">
            {scheduleCount ?? 0}
          </span>
        </div>
        <p className="mt-2 text-sm font-semibold text-ink">{scheduleSummary ?? "Sin programaciones"}</p>
        <p className="mt-1 text-xs text-slate-500">
          {(scheduleCount ?? 0) > 0 ? "Puedes editar, desactivar o eliminar reglas desde este dashboard." : "La pantalla depende solo de la campaña visible actual."}
        </p>
      </div>

      <div className="mt-3 rounded-[18px] border border-slate-200 bg-slate-50 px-3 py-3">
        <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">Ultima comunicacion</p>
        <p className="mt-2 text-sm font-semibold text-ink">{lastCommunicationLabel}</p>
        <p className="mt-1 text-xs text-slate-500">
          {status === "online" ? "Heartbeat dentro de los ultimos 90 segundos." : "Sin heartbeat reciente del Player."}
        </p>
      </div>
    </article>
  );
}
