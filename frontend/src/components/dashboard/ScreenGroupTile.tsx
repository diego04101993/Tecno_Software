import { PencilLine, Trash2 } from "lucide-react";
import type { ReactNode } from "react";

import type { ChannelStatus } from "../../types/domain";
import { StatusBadge } from "../StatusBadge";

export function ScreenGroupTile({
  title,
  subtitle,
  status,
  summary,
  actions,
  children,
}: {
  title: string;
  subtitle: string;
  status: ChannelStatus;
  summary: Array<{ label: string; value: string }>;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <article className="rounded-[26px] border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="font-display text-2xl text-ink">{title}</p>
          <p className="mt-1 text-sm text-slate-600">{subtitle}</p>
        </div>
        <div className="flex items-center gap-3">
          {actions}
          <StatusBadge status={status} />
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {summary.map((item) => (
          <div key={item.label} className="rounded-[18px] border border-slate-200 bg-white px-3 py-3">
            <p className="text-[11px] uppercase tracking-[0.2em] text-slate-400">{item.label}</p>
            <p className="mt-2 text-sm font-semibold text-ink">{item.value}</p>
          </div>
        ))}
      </div>

      <div className="mt-4">{children}</div>
    </article>
  );
}

export function ScreenGroupActions({
  onEdit,
  onDelete,
}: {
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  if (!onEdit && !onDelete) {
    return null;
  }

  return (
    <div className="flex flex-wrap gap-2">
      {onEdit ? (
        <button
          className="inline-flex items-center gap-2 rounded-full border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 transition hover:border-cyan-300 hover:text-ink"
          type="button"
          onClick={onEdit}
        >
          <PencilLine className="h-3.5 w-3.5" />
          Editar
        </button>
      ) : null}
      {onDelete ? (
        <button
          className="inline-flex items-center gap-2 rounded-full border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700 transition hover:border-rose-300 hover:bg-rose-50"
          type="button"
          onClick={onDelete}
        >
          <Trash2 className="h-3.5 w-3.5" />
          Eliminar
        </button>
      ) : null}
    </div>
  );
}
