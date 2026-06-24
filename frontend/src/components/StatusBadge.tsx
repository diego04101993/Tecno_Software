import type { ChannelStatus } from "../types/domain";

const styles: Record<ChannelStatus, string> = {
  online: "bg-emerald-100 text-emerald-700 border-emerald-200",
  offline: "bg-rose-100 text-rose-700 border-rose-200",
  unknown: "bg-slate-100 text-slate-600 border-slate-200",
};

const labels: Record<ChannelStatus, string> = {
  online: "En línea",
  offline: "Sin conexión",
  unknown: "Sin datos",
};

export function StatusBadge({ status }: { status: ChannelStatus }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-wide ${styles[status]}`}>
      {labels[status]}
    </span>
  );
}
