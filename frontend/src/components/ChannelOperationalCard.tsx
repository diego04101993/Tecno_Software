import { Activity, Clock3, PlayCircle, Presentation } from "lucide-react";
import { Link } from "react-router-dom";

import { formatChannelMode, formatOrientation } from "../lib/labels";
import type { Channel } from "../types/domain";
import { StatusBadge } from "./StatusBadge";

type HeartbeatTone = "emerald" | "amber" | "rose";

const heartbeatStyles: Record<HeartbeatTone, string> = {
  emerald: "border-emerald-200 bg-emerald-50 text-emerald-800",
  amber: "border-amber-200 bg-amber-50 text-amber-900",
  rose: "border-rose-200 bg-rose-50 text-rose-800",
};

export function ChannelOperationalCard({
  channel,
  assignedCampaignLabel,
  activeTimelineCount,
  heartbeatLabel,
  heartbeatTone,
  detailTo,
  branchName,
}: {
  channel: Channel;
  assignedCampaignLabel: string;
  activeTimelineCount: number;
  heartbeatLabel: string;
  heartbeatTone: HeartbeatTone;
  detailTo?: string;
  branchName?: string;
}) {
  async function copyChannelCode() {
    try {
      await navigator.clipboard.writeText(channel.channel_code);
    } catch {
      // Ignore clipboard failures in unsupported contexts.
    }
  }

  return (
    <article className="rounded-[26px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate font-display text-2xl text-ink" title={channel.name}>
            {channel.name}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold tracking-[0.18em] text-slate-600">
              {channel.channel_code}
            </span>
            <button
              className="rounded-full border border-slate-200 px-3 py-1 text-xs font-semibold text-slate-600 transition hover:border-cyan-300 hover:text-ink"
              type="button"
              onClick={copyChannelCode}
            >
              Copiar código
            </button>
          </div>
          <p className="mt-1 text-sm text-slate-500">
            {branchName ? `${branchName} · ` : ""}
            {formatChannelMode(channel.mode)} · {formatOrientation(channel.orientation)} · {channel.resolution_width}x{channel.resolution_height}
          </p>
        </div>
        <StatusBadge status={channel.status} />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex items-center gap-2 text-slate-500">
            <PlayCircle className="h-4 w-4" />
            <span className="text-xs uppercase tracking-wide">Playback</span>
          </div>
          <p className="mt-2 line-clamp-2 text-sm font-semibold text-ink">{channel.current_playback ?? "Sin reproducción reportada"}</p>
        </div>
        <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex items-center gap-2 text-slate-500">
            <Presentation className="h-4 w-4" />
            <span className="text-xs uppercase tracking-wide">Campaña</span>
          </div>
          <p className="mt-2 line-clamp-2 text-sm font-semibold text-ink">{assignedCampaignLabel}</p>
        </div>
        <div className="rounded-[20px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex items-center gap-2 text-slate-500">
            <Activity className="h-4 w-4" />
            <span className="text-xs uppercase tracking-wide">Timeline</span>
          </div>
          <p className="mt-2 text-sm font-semibold text-ink">{activeTimelineCount} slot(s) activos</p>
        </div>
        <div className={`rounded-[20px] border px-3 py-3 ${heartbeatStyles[heartbeatTone]}`}>
          <div className="flex items-center gap-2">
            <Clock3 className="h-4 w-4" />
            <span className="text-xs uppercase tracking-wide">Heartbeat</span>
          </div>
          <p className="mt-2 text-sm font-semibold">{heartbeatLabel}</p>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between gap-3">
        <p className="text-xs uppercase tracking-wide text-slate-400">
          {channel.screen_count} salida(s) · Hardware {channel.hardware_identifier ?? "pendiente"}
        </p>
        {detailTo ? (
          <Link
            className="inline-flex rounded-full border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:border-accent hover:text-ink"
            to={detailTo}
          >
            Ver detalle
          </Link>
        ) : null}
      </div>
    </article>
  );
}
