import { ArrowRightLeft, ChevronDown, ChevronRight, PencilLine, PlusCircle, Trash2 } from "lucide-react";
import type { ReactNode } from "react";

function ActionButton({
  children,
  danger = false,
  onClick,
}: {
  children: ReactNode;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={[
        "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-sm font-semibold transition",
        danger
          ? "border-rose-200 text-rose-700 hover:border-rose-300 hover:bg-rose-50"
          : "border-slate-200 text-slate-700 hover:border-cyan-300 hover:text-ink",
      ].join(" ")}
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      {children}
    </button>
  );
}

export function BranchInfrastructureCard({
  title,
  code,
  address,
  totalScreens,
  onlineScreens,
  offlineScreens,
  visibleCampaigns,
  expanded,
  onToggle,
  onAddScreen,
  onPublishCampaign,
  onEdit,
  onDelete,
  canAddScreen,
  canPublishCampaign,
  canEdit,
  canDelete,
  children,
}: {
  title: string;
  code: string;
  address: string | null;
  totalScreens: number;
  onlineScreens: number;
  offlineScreens: number;
  visibleCampaigns: number;
  expanded: boolean;
  onToggle: () => void;
  onAddScreen?: () => void;
  onPublishCampaign?: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
  canAddScreen?: boolean;
  canPublishCampaign?: boolean;
  canEdit?: boolean;
  canDelete?: boolean;
  children?: ReactNode;
}) {
  return (
    <article className="rounded-[30px] border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <button className="flex min-w-0 flex-1 items-start gap-4 text-left" type="button" onClick={onToggle}>
          <span className="mt-1 rounded-2xl bg-slate-100 p-3 text-slate-700">
            {expanded ? <ChevronDown className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
          </span>
          <div className="min-w-0">
            <p className="truncate font-display text-3xl text-ink" title={title}>
              {title}
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <p className="text-sm text-slate-500">{code}</p>
              <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700">{onlineScreens} online</span>
              <span className="rounded-full bg-rose-50 px-2.5 py-1 text-xs font-semibold text-rose-700">{offlineScreens} offline</span>
            </div>
            <p className="mt-3 text-sm text-slate-700">{address ?? "Dirección pendiente"}</p>
          </div>
        </button>

        <div className="flex shrink-0 flex-wrap gap-3">
          {canAddScreen && onAddScreen ? (
            <ActionButton onClick={onAddScreen}>
              <PlusCircle className="h-4 w-4" />
              Agregar pantalla
            </ActionButton>
          ) : null}
          {canPublishCampaign && onPublishCampaign ? (
            <ActionButton onClick={onPublishCampaign}>
              <ArrowRightLeft className="h-4 w-4" />
              Publicar campaña
            </ActionButton>
          ) : null}
          {canEdit && onEdit ? (
            <ActionButton onClick={onEdit}>
              <PencilLine className="h-4 w-4" />
              Editar
            </ActionButton>
          ) : null}
          {canDelete && onDelete ? (
            <ActionButton danger onClick={onDelete}>
              <Trash2 className="h-4 w-4" />
              Eliminar
            </ActionButton>
          ) : null}
        </div>
      </div>

      <div className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-[20px] bg-slate-50 px-4 py-3 text-sm text-slate-700">
          <p className="text-xs uppercase tracking-wide text-slate-400">Pantallas</p>
          <p className="mt-2 font-semibold text-ink">{totalScreens}</p>
        </div>
        <div className="rounded-[20px] bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          <p className="text-xs uppercase tracking-wide text-emerald-700">Online</p>
          <p className="mt-2 font-semibold text-ink">{onlineScreens}</p>
        </div>
        <div className="rounded-[20px] bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <p className="text-xs uppercase tracking-wide text-rose-700">Offline</p>
          <p className="mt-2 font-semibold text-ink">{offlineScreens}</p>
        </div>
        <div className="rounded-[20px] bg-slate-50 px-4 py-3 text-sm text-slate-700">
          <p className="text-xs uppercase tracking-wide text-slate-400">Campañas visibles</p>
          <p className="mt-2 font-semibold text-ink">{visibleCampaigns}</p>
        </div>
      </div>

      {expanded ? <div className="mt-5 border-t border-slate-200 pt-5">{children}</div> : null}
    </article>
  );
}
